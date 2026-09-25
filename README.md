# otk

Offline transfer kit: moves container images from a low-side registry across a one-way link to a
high-side registry with manifest digests, platforms and tags unchanged. Start with [QUICKSTART.md](QUICKSTART.md).

## Contents

- [`otk/`](otk/README.md): the `otk` command. Packs images on the low side, imports and reconciles on the high side.
- [`nifi/`](nifi/README.md): NiFi flows that carry packs to and from the one-way link, one per side.
- [`ci/gitlab/`](ci/gitlab/README.md): GitLab CI template that plans on merge requests and packs on the default branch.
- [`lab/`](lab/README.md): compose lab with both registries, both NiFis and a folder standing in for the link, plus the end-to-end test.
- [`Dockerfile`](Dockerfile): the `otk` image, skopeo and Python on a pinned base, used by CI, the lab and any container runtime.

## Requirements

- Python 3.11 or later and skopeo 1.14 or later on each side, or the image built from `Dockerfile`.
- A low-side registry that serves the source images, typically through a pull-through cache.
- A high-side registry that accepts pushes to the target namespaces.
- A transport that moves files one way unchanged: the NiFi flows here, or any diode file drop.

## Usage

Low side, from a host or CI job with the low registry reachable:

```sh
otk pack -c otk.toml
```

High side, as a service beside the inbox the transport fills:

```sh
otk import -c otk.toml --watch
```

## Expected result

Every tag in the low-side config exists on the high side with the same manifest digest, and
`otk status -c otk.toml` on the high side reports no gaps.

```sh
skopeo inspect --raw docker://<high-registry>/<repo>:<tag> | sha256sum    # equals the low side
```
