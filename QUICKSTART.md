## What this is

`otk` moves container images from a low-side registry across a one-way link to a high-side registry with the same digests and tags. `otk/` holds the command, `nifi/` the flows for each side of the link, `ci/gitlab/otk.yml` the low-side pipeline and `lab/` a full emulation with its end-to-end test. It does not sign, scan or physically transfer anything.

## How to use it

1. Build the image and push it where both sides can pull it: `docker build -t registry.example.internal/tools/otk:0.1.0 .`
2. Write `otk.toml` on the low side from the example in `otk/README.md`, one `[[images]]` block per repository.
3. Store low registry credentials for otk: `skopeo login --authfile /var/lib/otk/auth.json registry-low.example.internal`
4. Load the low-side NiFi flow: `NIFI_PASSWORD=<password> python3 nifi/flow.py --side low --url https://nifi-low.example.internal:8443 --param otk.source=/srv/otk/outbox --param otk.dest=/srv/diode/in`
5. On the high side, load the matching flow and start the importer: `otk import -c otk.toml --watch`
6. Send the first pack: `otk pack -c otk.toml`
7. Check the high side: `otk status -c otk.toml`

You know it works when `otk status` prints `"gaps": []` and `skopeo inspect --raw` of a tag returns the same sha256 on both registries.

If it fails:
- `status` lists a gap `n`: on the low side run `otk ledger rewind n`, then `otk pack -c otk.toml`.
- A repository is `refused`: check its tags in `otk.toml`, or raise `max_delete_pct` for an intended mass delete.
