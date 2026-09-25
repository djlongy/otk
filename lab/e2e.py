#!/usr/bin/env python3
"""End-to-end proof on the lab: every scenario asserts on digests read back from the registries.

usage: DOCKER_CONTEXT=<ctx> python3 lab/e2e.py        (after lab/up.sh)

  1 first pack        every image lands with the upstream manifest digest, all platforms
  2 delta             a new tag crosses carrying only blobs the high side lacks
  3 lost pack         a pack that never arrives shows as a gap and an incomplete image;
                      `otk ledger rewind` on the low side repairs it with the next pack
  4 retarget/delete   a dropped tag is deleted, a moved tag is retargeted, siblings survive
  5 guard             a state that would delete most tags of a repository is refused
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
STATE = LAB / ".state"
DC = ["docker", "compose", "-f", str(LAB / "compose.yaml")]
HEAD = """[low]
registry = "low-quay:8080"
tls_verify = false
authfile = "/otk/auth/low.json"
work_dir = "/otk/work"
outbox = "/otk/outbox"
prune = true
"""


def sh(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        raise SystemExit(f"FAILED: {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p


def images_toml(images):
    out = HEAD
    for repo, tags in images.items():
        out += f'\n[[images]]\nsource = "proxy-hub/library/{repo}"\ntarget = "hub/library/{repo}"\ntags = {json.dumps(tags)}\n'
    return out


def otk_low(images, *args):
    cfg = STATE / "e2e-low.toml"
    cfg.write_text(images_toml(images))
    p = sh(DC + ["run", "--rm", "-v", f"{cfg}:/work/otk.toml:ro", "otk-low", *args])
    return p.stdout


def skopeo_digest(where, ref):
    auth = {"low": ["--authfile", "/otk/auth/low.json"], "high": ["--authfile", "/otk/auth/high.json"], "up": []}[where]
    host = {"low": "low-quay:8080/proxy-hub/library/", "high": "high-quay:8080/hub/library/", "up": "docker.io/library/"}[where]
    p = sh(DC + ["run", "--rm", "--no-deps", "--entrypoint", "skopeo", "otk-high", "inspect", "--raw",
                 "--tls-verify=false", *auth, f"docker://{host}{ref}"], check=False)
    return None if p.returncode else "sha256:" + hashlib.sha256(p.stdout.encode()).hexdigest()


def high_tags(repo):
    p = sh(DC + ["run", "--rm", "--no-deps", "--entrypoint", "skopeo", "otk-high", "list-tags", "--tls-verify=false",
                 "--authfile", "/otk/auth/high.json", f"docker://high-quay:8080/hub/library/{repo}"], check=False)
    return sorted(json.loads(p.stdout)["Tags"]) if p.returncode == 0 else []


def wait_report(seq, timeout=300):
    path = STATE / "high" / "reports" / f"otk-{seq:06d}.json"
    for _ in range(timeout // 5):
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(5)
    raise SystemExit(f"FAILED: pack {seq} was not imported within {timeout}s")


def last_seq():
    return json.loads((STATE / "low" / "ledger.json").read_text())["last_seq"]


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def assert_mirrored(images):
    for repo, tags in images.items():
        for tag in tags:
            up, hi = skopeo_digest("up", f"{repo}:{tag}"), skopeo_digest("high", f"{repo}:{tag}")
            check(f"{repo}:{tag} digest upstream == high", up is not None and up == hi, (hi or "missing")[:19])


def main():
    base = {"alpine": ["3.20.3", "3.21.3"], "redis": ["7.4.1-alpine"]}

    print("-- 1 first pack")
    out = otk_low(base, "pack", "--force")
    seq = last_seq()
    r = wait_report(seq)
    check("pack imported complete", not r["incomplete"] and not r.get("error"), out.strip().splitlines()[-1])
    assert_mirrored(base)

    print("-- 2 delta")
    delta = {"alpine": ["3.20.3", "3.21.3"], "redis": ["7.4.1-alpine", "7.4.2-alpine"]}
    out = otk_low(delta, "pack")
    r = wait_report(last_seq())
    line = out.strip().splitlines()[-1]
    carried, needed = map(int, line.split("image(s), ")[1].split(" blob")[0].split("/"))
    check("delta carries fewer blobs than the images need", carried < needed, line)
    assert_mirrored(delta)

    print("-- 3 lost pack")
    sh(DC + ["stop", "nifi-low"])
    lost_state = {"alpine": ["3.20.3", "3.21.3", "3.22.1"], "redis": ["7.4.1-alpine", "7.4.2-alpine"]}
    otk_low(lost_state, "pack")
    lost = last_seq()
    for p in (LAB / "data" / "outbox").glob(f"otk-{lost:06d}.tar*"):
        p.unlink()                                          # the pack never reaches the diode
    moved = {"alpine": ["3.20.3", "3.21.3", "3.22.1", "latest"], "redis": ["7.4.1-alpine", "7.4.2-alpine"]}
    otk_low(moved, "pack")
    sh(DC + ["start", "nifi-low"])
    r = wait_report(last_seq(), timeout=600)
    status = json.loads(sh(DC + ["exec", "-T", "otk-high", "otk", "status"], check=False).stdout)
    check("gap reported for the lost pack", status["gaps"] == [lost], str(status["gaps"]))
    check("image from the lost pack reported incomplete", bool(r["incomplete"]), f"{len(r['incomplete'])} incomplete")
    print(otk_low(moved, "ledger", "rewind", str(lost)).strip())
    otk_low(moved, "pack", "--force")
    r = wait_report(last_seq())
    check("rewound pack completes the image", not r["incomplete"])
    assert_mirrored(moved)

    print("-- 4 retarget and delete")
    trimmed = {"alpine": ["3.21.3", "3.22.1", "latest"], "redis": ["7.4.1-alpine", "7.4.2-alpine"]}
    otk_low(trimmed, "pack")
    r = wait_report(last_seq())
    check("dropped tag deleted", "hub/library/alpine:3.20.3" in r["deleted"], str(r["deleted"]))
    check("high tags equal desired", high_tags("alpine") == sorted(trimmed["alpine"]), str(high_tags("alpine")))
    assert_mirrored(trimmed)

    print("-- 5 guard")
    gutted = {"alpine": ["latest"], "redis": ["7.4.1-alpine", "7.4.2-alpine"]}
    otk_low(gutted, "pack")
    r = wait_report(last_seq())
    check("mass delete refused", "hub/library/alpine" in r["refused"], str(r["refused"]))
    check("nothing deleted", high_tags("alpine") == sorted(trimmed["alpine"]), str(high_tags("alpine")))
    print("all scenarios passed")


if __name__ == "__main__":
    main()
