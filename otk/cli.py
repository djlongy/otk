"""otk command line. Low side: plan, pack, ledger. High side: import, status. Either: verify."""

import argparse
import json
import sys

from otk import __version__, config, importer, ledger, pack, registry, select

EXIT_OK, EXIT_FAIL, EXIT_INCOMPLETE, EXIT_CONFIG = 0, 1, 3, 2


def _cfg(args):
    return config.load(args.config)


def cmd_plan(args):
    cfg = _cfg(args)
    config.need(cfg, "low")
    desired, sources = select.resolve(cfg, registry)
    for target, state in desired.items():
        for tag, digest in sorted(state.items()):
            print(f"{sources[target]}:{tag} -> {target}:{tag}  {digest}")
    return EXIT_OK


def cmd_pack(args):
    cfg = _cfg(args)
    config.need(cfg, "low")
    if cfg.outbox is None:
        raise config.ConfigError("[low] outbox is required")
    pack.build(cfg, registry, full=args.full, force=args.force)
    return EXIT_OK


def cmd_ledger(args):
    cfg = _cfg(args)
    lg = ledger.Ledger(config.need(cfg, "low").work_dir)
    if args.action == "rewind":
        n = lg.rewind(args.seq)
        lg.save()
        print(f"forgot {n} blob(s) first sent in pack {args.seq} or later; the next pack carries them again")
    else:
        print(f"last pack {lg.last_seq}, {len(lg.blobs)} blob(s) recorded as sent")
    return EXIT_OK


def cmd_verify(args):
    sha = args.sha or args.pack + ".sha256"
    importer.verify(args.pack, sha)
    print(f"{args.pack}: checksum ok")
    return EXIT_OK


def cmd_import(args):
    cfg = _cfg(args)
    config.need(cfg, "high")
    if cfg.inbox is None:
        raise config.ConfigError("[high] inbox is required")
    if args.watch:
        importer.watch(cfg, registry, args.interval)
    results = importer.run(cfg, registry)
    if any("error" in r for r in results):
        return EXIT_FAIL
    return EXIT_INCOMPLETE if any(r.get("incomplete") or r.get("refused") for r in results) else EXIT_OK


def cmd_status(args):
    cfg = _cfg(args)
    store = importer.Store(config.need(cfg, "high").work_dir)
    s = store.state
    out = {"applied_seq": s["applied_seq"], "received": len(s["seen"]), "gaps": store.gaps(),
           "repositories": len(s["desired"]), "tags": sum(len(t) for t in s["desired"].values())}
    print(json.dumps(out, indent=1))
    return EXIT_INCOMPLETE if out["gaps"] else EXIT_OK


def parser():
    ap = argparse.ArgumentParser(prog="otk", description=__doc__)
    ap.add_argument("--version", action="version", version=f"otk {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("plan", cmd_plan, "low: print the desired tag -> digest map"),
                            ("pack", cmd_pack, "low: write the next pack to the outbox"),
                            ("ledger", cmd_ledger, "low: show or rewind the sent-blob ledger"),
                            ("import", cmd_import, "high: import every complete pack in the inbox"),
                            ("status", cmd_status, "high: applied sequence and missing packs")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-c", "--config", default="otk.toml")
        p.set_defaults(fn=fn)
        if name == "pack":
            p.add_argument("--full", action="store_true", help="carry every needed blob, ignoring the ledger")
            p.add_argument("--force", action="store_true", help="write a pack even when nothing changed")
        if name == "ledger":
            p.add_argument("action", choices=("show", "rewind"), nargs="?", default="show")
            p.add_argument("seq", type=int, nargs="?", default=0)
        if name == "import":
            p.add_argument("--watch", action="store_true", help="keep polling the inbox")
            p.add_argument("--interval", type=int, default=30)
    v = sub.add_parser("verify", help="check a pack against its .sha256")
    v.add_argument("pack")
    v.add_argument("--sha")
    v.set_defaults(fn=cmd_verify)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    if args.cmd == "ledger" and args.action == "rewind" and args.seq < 1:
        print("otk ledger rewind needs a pack number", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except (importer.PackError, registry.RegistryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL
