"""An in-memory registry with the same surface as otk.registry, backed by OCI layouts on disk."""

import hashlib
import json
import tempfile
from pathlib import Path

from otk import oci
from otk.registry import RegistryError


def put_blob(layout, data):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    p = oci.blob_path(layout, digest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return digest, len(data)


def make_image(layout, layers, arch="amd64"):
    """A single-platform OCI image whose layers are the given byte strings. Returns the manifest digest."""
    cfg_d, cfg_s = put_blob(layout, json.dumps({"architecture": arch, "os": "linux"}).encode())
    descs = []
    for data in layers:
        d, s = put_blob(layout, data)
        descs.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": d, "size": s})
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": cfg_d, "size": cfg_s},
                "layers": descs, "annotations": {"org.opencontainers.image.version": "x"}}
    return put_blob(layout, json.dumps(manifest).encode())[0]


def make_index(layout, manifests):
    body = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": d,
                           "size": oci.blob_path(layout, d).stat().st_size,
                           "platform": {"architecture": a, "os": "linux"}} for d, a in manifests]}
    return put_blob(layout, json.dumps(body).encode())[0]


class FakeRegistry:
    RegistryError = RegistryError

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="otk-fake-"))
        self.tags = {}          # (registry, repo) -> {tag: digest}
        self.pulls = 0

    def layout(self, registry):
        return oci.init(self.root / registry.replace(":", "_"))

    def seed(self, registry, repo, tag, digest):
        self.tags.setdefault((registry, repo), {})[tag] = digest

    def list_tags(self, side, repo, **_):
        if side is None:
            registry, repo = repo.split("/", 1)
        else:
            registry = side.registry
        return sorted(self.tags.get((registry, repo), {}))

    def digest_or_none(self, side, repo, tag):
        return self.tags.get((side.registry, repo), {}).get(tag)

    def to_layout(self, side, repo, digest, layout, name):
        self.pulls += 1
        src = self.layout(side.registry)
        for d in oci.graph(src, digest):
            oci.link_blob(src, layout, d, verify=False)
        oci.upsert(layout, [oci.descriptor(layout, digest, name)])

    def from_layout(self, side, layout, name, repo, tag):
        entry = next(m for m in oci.read_index(layout) if m["annotations"][oci.REF] == name)
        blobs = oci.graph(layout, entry["digest"])
        if oci.missing(layout, blobs):
            raise RegistryError(f"blob missing in source layout for {repo}:{tag}")
        dst = self.layout(side.registry)
        for d in blobs:
            oci.link_blob(layout, dst, d, verify=False)
        self.seed(side.registry, repo, tag, entry["digest"])

    def delete_tag(self, side, repo, tag):
        tags = self.tags[(side.registry, repo)]
        digest = tags[tag]
        for t in [t for t, d in tags.items() if d == digest]:   # delete-by-digest, like most registries
            del tags[t]
