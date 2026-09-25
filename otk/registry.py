"""The only code that talks to a registry, through skopeo. Swap this module to use another client."""

import hashlib
import json
import subprocess


class RegistryError(RuntimeError):
    pass


def _run(args, capture=True):
    p = subprocess.run(["skopeo", *args], capture_output=capture, text=False)
    if p.returncode:
        raise RegistryError(f"skopeo {' '.join(args[:2])} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def _opts(side, prefix=""):
    out = [f"--{prefix}tls-verify={'true' if side.tls_verify else 'false'}"]
    if side.authfile:
        out.append(f"--{prefix}authfile={side.authfile}")
    return out


def ref(side, repo, tag_or_digest):
    sep = "@" if tag_or_digest.startswith("sha256:") else ":"
    return f"docker://{side.registry}/{repo}{sep}{tag_or_digest}"


def raw_manifest(side, repo, tag):
    """Exact manifest bytes and their digest. The digest is what must survive the transfer."""
    raw = _run(["inspect", "--raw", *_opts(side), ref(side, repo, tag)])
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def digest_or_none(side, repo, tag):
    try:
        return raw_manifest(side, repo, tag)[1]
    except RegistryError as e:
        msg = str(e).lower()
        # Quay lists a just-deleted tag briefly and then answers "Tag x was deleted or has expired".
        if any(k in msg for k in ("manifest unknown", "name unknown", "not found", "404", "was deleted", "has expired")):
            return None
        raise


def list_tags(side_or_none, repo, tls_verify=True, authfile=""):
    """Tags of a repository. repo may be a full upstream path (docker.io/library/redis) when side is None."""
    if side_or_none is None:
        opts = [f"--tls-verify={'true' if tls_verify else 'false'}"] + ([f"--authfile={authfile}"] if authfile else [])
        target = f"docker://{repo}"
    else:
        opts, target = _opts(side_or_none), f"docker://{side_or_none.registry}/{repo}"
    try:
        return json.loads(_run(["list-tags", *opts, target]))["Tags"] or []
    except RegistryError as e:
        if "unknown" in str(e).lower() or "not found" in str(e).lower():
            return []
        raise


def to_layout(side, repo, digest, layout, name):
    """Pull every platform of one manifest into an OCI layout, bytes untouched."""
    _run(["copy", "--all", "--preserve-digests", "--retry-times=3", *_opts(side, "src-"),
          ref(side, repo, digest), f"oci:{layout}:{name}"], capture=True)


def from_layout(side, layout, name, repo, tag):
    """Push a manifest from an OCI layout to repo:tag, refusing any rewrite of its digest."""
    _run(["copy", "--all", "--preserve-digests", "--retry-times=3", *_opts(side, "dest-"),
          f"oci:{layout}:{name}", ref(side, repo, tag)], capture=True)


def delete_tag(side, repo, tag):
    """Deletes the manifest the tag points at. Registries that delete by digest drop sibling tags too;
    the importer re-pushes every desired tag afterwards, so a shared digest comes straight back."""
    _run(["delete", *_opts(side), ref(side, repo, tag)])
