"""Decide which tags go: explicit tags plus the newest N semver tags per image."""

import re

SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def semver_key(tag):
    m = SEMVER.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def newest(tags, keep, prefix=""):
    """Newest `keep` plain semver tags (no pre-release), optionally limited to a prefix like '1.27.'."""
    candidates = [t for t in tags if semver_key(t) and t.lstrip("v").startswith(prefix.lstrip("v"))]
    return sorted(candidates, key=semver_key, reverse=True)[:keep]


def resolve(cfg, registry):
    """Desired state: {target_repo: {tag: digest}}, plus which low repo serves each target.

    Digests come from the low registry, so the pull-through cache fetches each manifest once here."""
    desired, sources = {}, {}
    for img in cfg.images:
        tags = list(img.tags)
        if img.semver_keep:
            listed = (registry.list_tags(None, img.tags_from) if img.tags_from
                      else registry.list_tags(cfg.low, img.source))
            tags += [t for t in newest(listed, img.semver_keep, img.semver_prefix) if t not in tags]
        state = {}
        for tag in tags:
            digest = registry.digest_or_none(cfg.low, img.source, tag)
            if digest is None:
                raise SystemExit(f"{img.source}:{tag} not found on {cfg.low.registry}")
            state[tag] = digest
        desired[img.target], sources[img.target] = state, img.source
    return desired, sources
