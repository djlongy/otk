#!/usr/bin/env python3
"""Build the otk NiFi flow for one side of the link through the REST API, and start it. Stdlib only.

usage: flow.py --side low|high [--url https://nifi:8443] [--user admin] [--password ...]
               [--param otk.source=/dir] [--param otk.dest=/dir] [--reset] [--export FILE]

Both sides are the same three processors; only the directories differ, and those live in a
parameter context so the flow moves between environments without editing processors:

  ListFile #{otk.source}  (otk-NNNNNN.tar and its .sha256, hidden temp files ignored)
    -> FetchFile           (moves the original to #{otk.source}/sent)
    -> PutFile #{otk.dest} (conflict = fail, so a re-sent pack never overwrites silently)

low:  source = otk outbox,        dest = the diode's ingress directory
high: source = the diode's egress, dest = otk inbox
Nothing unpacks or merges a pack in NiFi; the pack crosses byte for byte and otk verifies it.
Failures loop back with NiFi's penalty so a stuck file stays visible in the queue instead of vanishing.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

FILTER = r"otk-[0-9]{6}\.tar(\.sha256)?"
DEFAULTS = {"low": {"otk.source": "/otk/outbox", "otk.dest": "/otk/diode"},
            "high": {"otk.source": "/otk/diode", "otk.dest": "/otk/inbox"}}


class Nifi:
    def __init__(self, url, user, password, verify_tls):
        self.api = url.rstrip("/") + "/nifi-api"
        self.user, self.password, self.token, self.types = user, password, None, {}
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    def login(self):
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.api + "/access/token", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            self.token = r.read().decode()

    def call(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def bundle(self, type_name):
        if not self.types:
            self.types = {t["type"]: t["bundle"] for t in self.call("GET", "/flow/processor-types")["processorTypes"]}
        full = next(t for t in self.types if t.endswith("." + type_name))
        return full, self.types[full]

    def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None):
        t, b = self.bundle(type_name)
        cfg = {"properties": props, "autoTerminatedRelationships": list(terminate)}
        if schedule:
            cfg["schedulingPeriod"] = schedule
        return self.call("POST", f"/process-groups/{pg}/processors",
                         {"revision": {"version": 0}, "component": {"type": t, "bundle": b, "name": name,
                                                                    "position": {"x": 0, "y": y * 180}, "config": cfg}})["id"]

    def connect(self, pg, src, dst, rels):
        self.call("POST", f"/process-groups/{pg}/connections",
                  {"revision": {"version": 0},
                   "component": {"source": {"id": src, "groupId": pg, "type": "PROCESSOR"},
                                 "destination": {"id": dst, "groupId": pg, "type": "PROCESSOR"},
                                 "selectedRelationships": list(rels)}})


def find_group(n, root, name):
    return next((g for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]
                 if g["component"]["name"] == name), None)


def remove_group(n, g):
    pg = g["id"]
    n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "STOPPED"})
    time.sleep(2)
    n.call("POST", f"/process-groups/{pg}/empty-all-connections-requests")
    time.sleep(1)
    g = n.call("GET", f"/process-groups/{pg}")
    n.call("DELETE", f"/process-groups/{pg}?version={g['revision']['version']}&clientId=otk")


def parameter_context(n, name, params):
    ctxs = n.call("GET", "/flow/parameter-contexts")["parameterContexts"]
    for c in ctxs:
        if c["component"]["name"] == name:
            n.call("DELETE", f"/parameter-contexts/{c['id']}?version={c['revision']['version']}&clientId=otk")
    body = {"revision": {"version": 0}, "component": {
        "name": name, "description": "otk transfer directories",
        "parameters": [{"parameter": {"name": k, "value": v, "sensitive": False}} for k, v in params.items()]}}
    return n.call("POST", "/parameter-contexts", body)["id"]


def build(n, side, params):
    root = n.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]
    name = f"otk {side} side"
    existing = find_group(n, root, name)
    if existing:
        remove_group(n, existing)
    ctx = parameter_context(n, f"otk-{side}", params)
    pg = n.call("POST", f"/process-groups/{root}/process-groups",
                {"revision": {"version": 0},
                 "component": {"name": name, "position": {"x": 0, "y": 0}, "parameterContext": {"id": ctx}}})["id"]
    ls = n.processor(pg, "ListFile", "list packs", 0,
                     {"Input Directory": "#{otk.source}", "File Filter": FILTER, "Recurse Subdirectories": "false",
                      "Minimum File Age": "5 sec", "Ignore Hidden Files": "true"}, schedule="10 sec")
    fetch = n.processor(pg, "FetchFile", "take pack", 1,
                        {"Completion Strategy": "Move File", "Move Destination Directory": "#{otk.source}/sent",
                         "Move Conflict Strategy": "Replace File"},
                        terminate=("not.found",))
    put = n.processor(pg, "PutFile", "hand over", 2,
                      {"Directory": "#{otk.dest}", "Conflict Resolution Strategy": "fail",
                       "Create Missing Directories": "true"}, terminate=("success",))
    n.connect(pg, ls, fetch, ["success"])
    n.connect(pg, fetch, put, ["success"])
    n.connect(pg, fetch, fetch, ["failure", "permission.denied"])
    n.connect(pg, put, put, ["failure"])
    n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})
    return pg


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--side", choices=("low", "high"), required=True)
    ap.add_argument("--url", default=os.environ.get("NIFI_URL", "https://localhost:8443"))
    ap.add_argument("--user", default=os.environ.get("NIFI_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("NIFI_PASSWORD"))
    ap.add_argument("--param", action="append", default=[], help="otk.source=DIR or otk.dest=DIR")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed lab NiFi)")
    ap.add_argument("--export", help="also write the group's flow definition to this file")
    args = ap.parse_args()
    if not args.password:
        raise SystemExit("set --password or NIFI_PASSWORD")
    params = dict(DEFAULTS[args.side], **dict(p.split("=", 1) for p in args.param))
    n = Nifi(args.url, args.user, args.password, not args.insecure)
    for _ in range(60):
        try:
            n.login()
            break
        except Exception:  # noqa: BLE001 - NiFi still starting
            time.sleep(5)
    else:
        raise SystemExit(f"NiFi at {args.url} not reachable")
    pg = build(n, args.side, params)
    print(f"otk {args.side} side running in process group {pg}: {params}")
    if args.export:
        req = urllib.request.Request(f"{n.api}/process-groups/{pg}/download",
                                     headers={"Authorization": f"Bearer {n.token}"})
        with urllib.request.urlopen(req, timeout=60, context=n.ctx) as r:
            open(args.export, "wb").write(r.read())
        print(f"flow definition written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
