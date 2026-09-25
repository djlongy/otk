"""High side: verify a pack, merge it into the persistent OCI store, push to the registry, reconcile.

The store keeps every blob that ever crossed, so a pack carrying only new layers still yields
complete images. Packs are applied in sequence order; a gap is reported, never guessed around."""

import json
import os
import re
import shutil
import tarfile
import time
from pathlib import Path

from otk import oci, reconcile

NAME = re.compile(r"^otk-(\d{6})\.tar$")


class PackError(RuntimeError):
    pass


def pending(inbox):
    """(seq, tar, sha) for every pack whose tar and checksum have both arrived, oldest first."""
    out = []
    for p in Path(inbox).glob("otk-*.tar"):
        m = NAME.match(p.name)
        sha = p.with_name(p.name + ".sha256")
        if m and sha.exists():
            out.append((int(m.group(1)), p, sha))
    return sorted(out)


class Store:
    def __init__(self, work_dir):
        self.work = Path(work_dir)
        self.layout = oci.init(self.work / "store")
        self.state_path = self.work / "state.json"
        self.state = (json.loads(self.state_path.read_text()) if self.state_path.exists()
                      else {"applied_seq": 0, "seen": [], "desired": {}})

    def save(self):
        tmp = self.state_path.with_name(".state.json.tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        os.replace(tmp, self.state_path)

    def gaps(self):
        seen = set(self.state["seen"])
        return [s for s in range(1, max(seen, default=0)) if s not in seen]


def verify(tar, sha):
    want = Path(sha).read_text().split()[0]
    got = oci.sha256_file(tar).split(":", 1)[1]
    if want != got:
        raise PackError(f"{Path(tar).name}: checksum mismatch (sidecar {want[:12]}, file {got[:12]})")


def _extract(tar, dest):
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")
    return json.loads((dest / "pack.json").read_text())


def current_tags(registry, high, repo):
    return {t: d for t in registry.list_tags(high, repo) if (d := registry.digest_or_none(high, repo, t))}


def _push(registry, high, store, digest, repo, tag):
    registry.from_layout(high, store.layout, oci.ref_name(digest), repo, tag)
    got = registry.digest_or_none(high, repo, tag)
    if got != digest:
        raise PackError(f"{repo}:{tag} landed as {got}, expected {digest}")


def import_pack(cfg, registry, tar, sha, log=print):
    high = cfg.high
    store = Store(high.work_dir)
    verify(tar, sha)
    incoming = store.work / "incoming" / Path(tar).stem
    pack = _extract(tar, incoming)
    seq = pack["seq"]

    added = 0
    for digest in pack["carried"]:
        added += oci.link_blob(incoming, store.layout, digest, verify=True)

    complete, incomplete = [], []
    for img in pack["images"]:
        lacking = oci.missing(store.layout, img["blobs"])
        (incomplete if lacking else complete).append((img, lacking))
    oci.upsert(store.layout, [oci.descriptor(store.layout, i["digest"], oci.ref_name(i["digest"]))
                              for i, _ in complete])
    log(f"merge  {Path(tar).name}: {added} blob(s) added, {len(complete)} image(s) complete, "
        f"{len(incomplete)} incomplete")
    for img, lacking in incomplete:
        log(f"  incomplete {img['target']}@{img['digest'][:19]}: {len(lacking)} blob(s) missing, "
            f"first {lacking[0][:19]}")

    pushed = 0
    for img, _ in complete:
        for tag in img["tags"]:
            _push(registry, high, store, img["digest"], img["target"], tag)
            pushed += 1

    report = {"seq": seq, "added": added, "pushed": pushed, "incomplete": [i["digest"] for i, _ in incomplete],
              "deleted": [], "refused": [], "stale": seq <= store.state["applied_seq"]}
    if report["stale"]:
        log(f"state  pack {seq} is older than applied pack {store.state['applied_seq']}: content merged, "
            "tags not reconciled")
    else:
        _reconcile(cfg, registry, store, pack, report, log)
        store.state["applied_seq"] = seq
        store.state["desired"] = pack["state"]
        if report["refused"]:
            log("prune  skipped: a repository refused its desired state, the store keeps everything")
        else:
            _prune(store, pack, log)

    store.state["seen"] = sorted(set(store.state["seen"]) | {seq})
    store.save()
    reports = store.work / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"otk-{seq:06d}.json").write_text(json.dumps(report, indent=1) + "\n")
    shutil.rmtree(incoming, ignore_errors=True)
    if store.gaps():
        log(f"gap    packs never received: {', '.join(map(str, store.gaps()))} "
            "(on the low side: otk ledger rewind <first missing>)")
    return report


def _reconcile(cfg, registry, store, pack, report, log):
    for target, desired in pack["state"].items():
        current = current_tags(registry, cfg.high, target)
        push, delete, refused = reconcile.plan(current, desired, cfg.max_delete_pct)
        if refused:
            report["refused"].append(target)
            log(f"refuse {target}: would delete {len(current) - len(desired)} of {len(current)} tag(s), "
                f"over max_delete_pct={cfg.max_delete_pct}")
            continue
        for tag in delete:
            registry.delete_tag(cfg.high, target, tag)
            report["deleted"].append(f"{target}:{tag}")
        # A registry that deletes by digest takes sibling tags with it; re-check after deleting.
        current = current_tags(registry, cfg.high, target) if delete else current
        for tag in sorted(t for t, d in desired.items() if current.get(t) != d):
            digest = desired[tag]
            if oci.missing(store.layout, [digest]):
                log(f"skip   {target}:{tag}: manifest {digest[:19]} not in the store yet")
                continue
            _push(registry, cfg.high, store, digest, target, tag)
            report["pushed"] += 1
        if delete:
            log(f"delete {target}: {', '.join(delete)}")


def _prune(store, pack, log):
    wanted = {d for tags in pack["state"].values() for d in tags.values()}
    keep = [m for m in oci.read_index(store.layout) if m["digest"] in wanted]
    oci.write_index(store.layout, keep)
    if not pack["prune"]:
        return
    reachable = set()
    for m in keep:
        reachable.update(oci.graph(store.layout, m["digest"]))
    gone = 0
    for digest in pack["prune"]:
        path = oci.blob_path(store.layout, digest)
        if digest not in reachable and path.exists():
            path.unlink()
            gone += 1
    log(f"prune  {gone} blob(s) removed, {len(pack['prune']) - gone} kept or absent")


def run(cfg, registry, log=print):
    inbox = Path(cfg.inbox)
    done, failed = inbox / "done", inbox / "failed"
    results = []
    for seq, tar, sha in pending(inbox):
        try:
            results.append(import_pack(cfg, registry, tar, sha, log))
            dest = done
        except Exception as e:  # noqa: BLE001 - one bad pack must not block the next
            log(f"FAILED {tar.name}: {e}")
            results.append({"seq": seq, "error": str(e)})
            dest = failed
        dest.mkdir(parents=True, exist_ok=True)
        for p in (tar, sha):
            os.replace(p, dest / p.name)
    return results


def watch(cfg, registry, interval, log=print):
    while True:
        run(cfg, registry, log)
        time.sleep(interval)
