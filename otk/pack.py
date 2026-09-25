"""Low side: build the next pack. One tar per run holding an OCI layout of the new blobs, the index of
every wanted manifest, and pack.json with the complete desired state."""

import hashlib
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

from otk import __version__, oci, select
from otk.ledger import Ledger


def _state_hash(desired):
    return hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()


def build(cfg, registry, full=False, force=False, log=print):
    low = cfg.low
    work = Path(low.work_dir)
    cache = oci.init(work / "cache")
    ledger = Ledger(work)
    desired, sources = select.resolve(cfg, registry)

    images, needed = [], []
    for target, state in desired.items():
        by_digest = {}
        for tag, digest in state.items():
            by_digest.setdefault(digest, []).append(tag)
        for digest, tags in by_digest.items():
            name = oci.ref_name(digest)
            try:
                blobs = oci.graph(cache, digest)
                if oci.missing(cache, blobs):
                    raise KeyError(digest)
            except KeyError:
                log(f"pull   {sources[target]}@{digest[:19]} ({', '.join(tags)})")
                registry.to_layout(low, sources[target], digest, cache, name)
                blobs = oci.graph(cache, digest)
            images.append({"target": target, "source": sources[target], "digest": digest,
                           "tags": sorted(tags), "blobs": blobs})
            needed.extend(blobs)
    needed = list(dict.fromkeys(needed))

    carry = needed if full else [b for b in needed if b not in ledger]
    prune = sorted(d for d in ledger.blobs if d not in set(needed)) if cfg.prune else []
    state_hash = _state_hash(desired)
    last = work / "last-state"
    if not carry and not prune and not force and last.exists() and last.read_text() == state_hash:
        log("nothing to send: no new blobs and the desired state is unchanged")
        return None

    seq = ledger.last_seq + 1
    name = f"otk-{seq:06d}"
    staging = work / "staging" / name
    shutil.rmtree(staging, ignore_errors=True)
    oci.init(staging)
    for b in carry:
        oci.link_blob(cache, staging, b, verify=False)
    oci.upsert(staging, [oci.descriptor(cache, i["digest"], oci.ref_name(i["digest"])) for i in images])
    manifest = {"otk": __version__, "seq": seq, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "images": images, "state": desired, "carried": carry, "prune": prune}
    (staging / "pack.json").write_text(json.dumps(manifest, indent=1) + "\n")

    outbox = Path(cfg.outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    tmp = outbox / f".{name}.tar.tmp"
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(staging)), recursive=False)
    digest = oci.sha256_file(tmp).split(":", 1)[1]
    # The ledger is written before the pack is published: a crash after this point re-sends
    # nothing twice, and a pack that is later lost is repaired with `otk ledger rewind`.
    ledger.forget(prune)
    ledger.record(seq, carry)
    ledger.save()
    last.write_text(state_hash)
    os.replace(tmp, outbox / f"{name}.tar")
    sha_tmp = outbox / f".{name}.tar.sha256.tmp"
    sha_tmp.write_text(f"{digest}  {name}.tar\n")
    os.replace(sha_tmp, outbox / f"{name}.tar.sha256")
    shutil.rmtree(staging, ignore_errors=True)
    size = (outbox / f"{name}.tar").stat().st_size
    log(f"pack   {name}.tar  {len(images)} image(s), {len(carry)}/{len(needed)} blob(s) carried, "
        f"{len(prune)} pruned, {size / 1e6:.1f} MB")
    return outbox / f"{name}.tar"
