# otk

`otk` builds numbered transfer packs from a low-side registry and imports them into a high-side registry, keeping every digest.

## Requirements

- Python 3.11 or later (standard library only) and skopeo 1.14 or later on `PATH`.
- Registry credentials in a containers auth file (`skopeo login --authfile`).

## Flags

Full set: `otk --help`, and the loader in `otk/config.py`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `[low] registry` | none | Registry the source images are pulled from |
| Required | `[low] outbox` | none | Directory packs are written to, the transport's pickup point |
| Required | `[low] work_dir` / `[high] work_dir` | `./otk-<side>` | Persistent state: ledger and cache (low), OCI store and applied state (high) |
| Required | `[[images]] source`, `target`, `tags` or `semver.keep` | none | Source repository, target repository, which tags go |
| Required | `[high] registry`, `inbox` | none | Registry to push to, directory the transport delivers into |
| Optional | `tls_verify`, `authfile` | `true`, skopeo default | Per side |
| Optional | `[low] prune` | `false` | Send deletions of blobs no longer wanted |
| Optional | `[high] max_delete_pct` | `50` | Refuse a state that deletes more than this share of a repository's tags |
| Optional | `[[images]] tags_from` | `source` | Where `semver` lists tags, e.g. `docker.io/library/redis` |

## Minimum configuration

`otk.toml`:

```toml
[low]
registry = "registry-low.example.internal"
work_dir = "/var/lib/otk"
outbox = "/srv/otk/outbox"

[high]
registry = "registry-high.example.internal"
work_dir = "/var/lib/otk"
inbox = "/srv/otk/inbox"

[[images]]
source = "proxy-hub/library/redis"
target = "hub/library/redis"
semver = { keep = 3, prefix = "7." }
tags_from = "docker.io/library/redis"
```

## Usage

`python3 -m otk pack -c otk.toml` on the low side; `python3 -m otk import -c otk.toml --watch` on the high side.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | A pack failed verification or a registry call failed |
| 2 | Configuration error |
| 3 | Imported, but an image is incomplete, a repository was refused, or a pack is missing |

## Preconditions

- Both `work_dir`s persist between runs, and the target namespace exists on the high registry.
- The transport delivers `otk-NNNNNN.tar` and its `.sha256` unchanged and ignores hidden temp files.

## Behaviour

- `pack` pulls every platform of each wanted manifest, sends only blobs the ledger has not recorded plus the full tag-to-digest state. No change writes no pack.
- A blob counts as sent when its pack is written. After a lost pack `n`, run `otk ledger rewind n` on the low side; the next pack carries it again.
- `import` verifies checksums, merges into the store, pushes complete images, then adds, retargets and deletes tags to match the state. An older pack adds content only.

## Out of scope

- Signing, scanning and approval of what is sent.
- The physical link and file transfer, including splitting for size limits.
- Creating registry organisations, robots or pull-through caches.

## Expected result

One `<high work_dir>/reports/otk-NNNNNN.json` per pack; `otk status -c otk.toml` prints `"gaps": []`.
