# otk NiFi flows

One process group per side that moves otk packs byte for byte: list, take, hand over.

## Requirements

- Apache NiFi 2.x.
- Python 3.11 or later for `flow.py`, or the NiFi UI to upload a flow definition.

## Config

Parameters of the `otk-low` and `otk-high` parameter contexts:

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `otk.source` | `/otk/outbox` (low), `/otk/diode` (high) | Directory listed for `otk-NNNNNN.tar` and `.sha256` |
| Required | `otk.dest` | `/otk/diode` (low), `/otk/inbox` (high) | Directory the files are written to |

## Usage

```sh
NIFI_PASSWORD=<password> python3 flow.py --side low --url https://nifi-low.example.internal:8443 \
  --param otk.source=/srv/otk/outbox --param otk.dest=/srv/diode/in
```

Or upload `otk-low.json` / `otk-high.json` as a process group in the UI and set the two parameters.

## Behaviour

- Taken files move to `<otk.source>/sent`; a failed write stays queued and retries, it is never dropped.
- A name that already exists at `otk.dest` fails instead of overwriting.
- `flow.py` replaces an existing `otk <side> side` group and its parameter context, then starts the new one.

## Expected result

A running group `otk low side` or `otk high side`, and each pack appears in `<otk.dest>` within about 20 seconds.
