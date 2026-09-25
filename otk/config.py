"""Load and validate otk.toml. Every path and endpoint comes from here, nothing is hard-coded."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class Side:
    registry: str
    tls_verify: bool = True
    authfile: str = ""
    work_dir: Path = Path(".")


@dataclass
class Image:
    source: str                  # repository on the low registry, e.g. proxy-hub/library/redis
    target: str                  # repository on the high registry, e.g. hub/library/redis
    tags: list[str] = field(default_factory=list)
    semver_keep: int = 0         # newest N semver tags, 0 = off
    semver_prefix: str = ""      # only tags starting with this, e.g. "1.27."
    tags_from: str = ""          # repository to list tags from, e.g. docker.io/library/redis


@dataclass
class Config:
    low: Side | None
    high: Side | None
    outbox: Path | None
    inbox: Path | None
    images: list[Image]
    prune: bool = False
    max_delete_pct: int = 50


def _side(raw, name):
    if name not in raw:
        return None
    s = raw[name]
    if not s.get("registry"):
        raise ConfigError(f"[{name}] registry is required")
    return Side(registry=s["registry"].rstrip("/"), tls_verify=bool(s.get("tls_verify", True)),
                authfile=s.get("authfile", ""), work_dir=Path(s.get("work_dir", f"./otk-{name}")))


def _image(i, raw):
    for key in ("source", "target"):
        if not raw.get(key):
            raise ConfigError(f"images[{i}].{key} is required")
    img = Image(source=raw["source"].strip("/"), target=raw["target"].strip("/"), tags=list(raw.get("tags", [])),
                semver_keep=int(raw.get("semver", {}).get("keep", 0)),
                semver_prefix=raw.get("semver", {}).get("prefix", ""), tags_from=raw.get("tags_from", ""))
    if not img.tags and not img.semver_keep:
        raise ConfigError(f"images[{i}] ({img.source}) needs tags or semver.keep")
    return img


def load(path):
    raw = tomllib.loads(Path(path).read_text())
    images = [_image(i, r) for i, r in enumerate(raw.get("images", []))]
    targets = [i.target for i in images]
    dup = {t for t in targets if targets.count(t) > 1}
    if dup:
        raise ConfigError(f"target repositories listed twice: {', '.join(sorted(dup))}")
    return Config(low=_side(raw, "low"), high=_side(raw, "high"),
                  outbox=Path(raw["low"]["outbox"]) if "outbox" in raw.get("low", {}) else None,
                  inbox=Path(raw["high"]["inbox"]) if "inbox" in raw.get("high", {}) else None,
                  images=images, prune=bool(raw.get("low", {}).get("prune", False)),
                  max_delete_pct=int(raw.get("high", {}).get("max_delete_pct", 50)))


def need(cfg, side):
    value = getattr(cfg, side)
    if value is None:
        raise ConfigError(f"this command needs a [{side}] section")
    return value
