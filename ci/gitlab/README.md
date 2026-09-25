# otk GitLab CI template

Runs `otk plan` on merge requests and `otk pack` on the default branch, so a merged change to `otk.toml` becomes the next pack.

## Requirements

- GitLab 17.0 or later (`spec:inputs`).
- A Docker executor runner, tagged as `runner-tag`, that mounts the persistent `work_dir` and `outbox` from `otk.toml`.
- The `otk` image from the repo `Dockerfile`, pushed to a registry the runner can pull.

## Inputs

Full set: `spec:inputs` in `otk.yml`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `image` | none | `otk` image pinned by digest |
| Optional | `config` | `otk.toml` | Path of the config in the consuming project |
| Optional | `runner-tag` | `otk` | Runner with the persistent mounts |
| Optional | `stage` | `deploy` | Stage both jobs run in |

## Secrets

| Variable | When | Purpose |
|---|---|---|
| `OTK_AUTHFILE` | Always | File variable: containers auth file with pull rights on the low registry |

## Minimum configuration

`.gitlab-ci.yml`:

```yaml
include:
  - remote: https://raw.githubusercontent.com/djlongy/otk/v0.1.0/ci/gitlab/otk.yml
    inputs:
      image: registry.example.internal/tools/otk@sha256:<digest>
```

## Preconditions

- The runner's volume mounts are the same paths as `work_dir` and `outbox` in `otk.toml`; without them the ledger is lost between jobs and every pack carries everything.
- NiFi (or the transport) watches the `outbox` path on that host.

## Behaviour

- `otk-pack` runs in resource group `otk-pack`, so two pipelines never write the ledger at once.
- A pipeline with no change to the desired state writes no pack.

## Expected result

A new `otk-NNNNNN.tar` and `.sha256` in the outbox after a merge, named in the `otk-pack` job log.
