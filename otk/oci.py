"""OCI image layout helpers: digests, the blob graph of a manifest, merging, completeness."""

import hashlib
import json
import os
import shutil
from pathlib import Path

INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
REF = "org.opencontainers.image.ref.name"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def blob_path(layout, digest):
    algo, hexd = digest.split(":", 1)
    return Path(layout) / "blobs" / algo / hexd


def init(layout):
    layout = Path(layout)
    (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    if not (layout / "oci-layout").exists():
        (layout / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}\n')
    if not (layout / "index.json").exists():
        write_index(layout, [])
    return layout


def read_index(layout):
    return json.loads((Path(layout) / "index.json").read_text()).get("manifests", [])


def write_index(layout, manifests):
    body = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": manifests}
    tmp = Path(layout) / ".index.json.tmp"
    tmp.write_text(json.dumps(body, indent=1) + "\n")
    os.replace(tmp, Path(layout) / "index.json")


def graph(layout, digest):
    """Every blob digest a manifest needs, the manifest itself first. Raises KeyError if a manifest is absent."""
    out, todo = [], [digest]
    while todo:
        d = todo.pop()
        if d in out:
            continue
        out.append(d)
        p = blob_path(layout, d)
        if not p.exists():
            raise KeyError(d)
        doc = json.loads(p.read_bytes())
        if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
            todo.extend(m["digest"] for m in doc.get("manifests", []))
        else:
            if "config" in doc:
                out.append(doc["config"]["digest"])
            out.extend(layer["digest"] for layer in doc.get("layers", []))
            if doc.get("subject"):
                todo.append(doc["subject"]["digest"])
    return list(dict.fromkeys(out))


def missing(layout, digests):
    return [d for d in digests if not blob_path(layout, d).exists()]


def descriptor(layout, digest, ref_name):
    p = blob_path(layout, digest)
    doc = json.loads(p.read_bytes())
    media = doc.get("mediaType") or ("application/vnd.oci.image.index.v1+json" if "manifests" in doc
                                     else "application/vnd.oci.image.manifest.v1+json")
    return {"mediaType": media, "digest": digest, "size": p.stat().st_size, "annotations": {REF: ref_name}}


def ref_name(digest):
    """OCI ref names allow [A-Za-z0-9._-]; one entry per manifest digest keeps the store tag-free."""
    return digest.replace(":", "-")


def upsert(layout, descriptors):
    by_ref = {m.get("annotations", {}).get(REF): m for m in read_index(layout)}
    for d in descriptors:
        by_ref[d["annotations"][REF]] = d
    write_index(layout, sorted(by_ref.values(), key=lambda m: m["annotations"][REF]))


def link_blob(src_layout, dst_layout, digest, verify=True):
    """Place a blob in dst (hard link when possible). Returns True if it was added."""
    src, dst = blob_path(src_layout, digest), blob_path(dst_layout, digest)
    if dst.exists():
        return False
    if verify and sha256_file(src) != digest:
        raise ValueError(f"corrupt blob {digest}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".tmp")
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return True
