#!/usr/bin/env python3
"""Give one non-superuser Quay account pull-through on a proxy-cache organisation and push on a second
organisation, and (Database auth) mint its app-specific token into a containers auth file.

usage: service-account.py --url https://quay.example.internal --user svc-otk \
         --pull-org dockerhub --push-org local [--authfile auth.json] [--teams-only] [--insecure]
env:   QUAY_ADMIN_TOKEN  OAuth token of a superuser, or of an admin of both organisations with --teams-only

Idempotent. Grants exactly what the registry checks:
  pull-org  team otk-pull, role member: pull-through needs organisation membership, nothing more
  push-org  team otk-push, role creator, default permission write: create repositories, push to existing ones
With --teams-only the user must already exist (an OIDC Quay creates it at its first web login) and the token
is minted in the UI instead. Registry credentials are per host, so one auth entry covers both organisations.
"""
import argparse
import base64
import http.cookiejar
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request


class Quay:
    def __init__(self, url, token=None, insecure=False):
        self.url = url.rstrip("/")
        self.token = token
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.url + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode(errors="replace")
            if e.code not in ok:
                return e.code, data
            return e.code, data


def must(result, what):
    code, data = result
    if code >= 300:
        raise SystemExit(f"{what}: HTTP {code} {data}")
    return data


def ensure_team(q, org, team, role, user, default_role=None):
    must(q.call("PUT", f"/api/v1/organization/{org}/team/{team}",
                {"role": role, "description": f"otk service account, {role}"}), f"team {org}/{team}")
    code, data = q.call("PUT", f"/api/v1/organization/{org}/team/{team}/members/{user}", {})
    if code >= 300 and "already a member" not in str(data):
        raise SystemExit(f"member {user} in {org}/{team}: HTTP {code} {data}")
    if default_role:
        code, protos = q.call("GET", f"/api/v1/organization/{org}/prototypes")
        if not any(p.get("delegate", {}).get("name") == team for p in (protos or {}).get("prototypes", [])):
            must(q.call("POST", f"/api/v1/organization/{org}/prototypes",
                        {"role": default_role, "delegate": {"kind": "team", "name": team}}), f"default permission {org}")
    print(f"{org}: team {team} role={role}" + (f", default permission {default_role}" if default_role else "")
          + f", member {user}")


def app_token(url, user, password, insecure):
    """Sign in as the service user (session + CSRF) and mint an app-specific token."""
    q = Quay(url, insecure=insecure)
    q.opener.open(url.rstrip("/") + "/csrf_token", timeout=30)
    code, data = q.call("GET", "/csrf_token")
    q.csrf = (data or {}).get("csrf_token")
    must(q.call("POST", "/api/v1/signin", {"username": user, "password": password}), f"sign in as {user}")
    code, data = q.call("GET", "/csrf_token")
    q.csrf = (data or {}).get("csrf_token", q.csrf)
    data = must(q.call("POST", "/api/v1/user/apptoken", {"title": "otk-ci"}), "create app token")
    return data["token"]["token_code"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", default="svc-otk")
    ap.add_argument("--pull-org", required=True)
    ap.add_argument("--push-org", required=True)
    ap.add_argument("--authfile", default="auth.json")
    ap.add_argument("--teams-only", action="store_true", help="grant only; the user exists and mints its own token")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    admin = Quay(args.url, os.environ.get("QUAY_ADMIN_TOKEN"), args.insecure)
    if not admin.token:
        raise SystemExit("set QUAY_ADMIN_TOKEN")

    password = None
    code, _ = admin.call("GET", f"/api/v1/users/{args.user}")
    if args.teams_only:
        if code == 404:
            raise SystemExit(f"{args.user} does not exist yet: sign in to Quay once as {args.user}")
        ensure_team(admin, args.pull_org, "otk-pull", "member", args.user)
        ensure_team(admin, args.push_org, "otk-push", "creator", args.user, "write")
        return 0
    if code == 404:
        data = must(admin.call("POST", "/api/v1/superuser/users/",
                               {"username": args.user, "email": f"{args.user}@example.invalid"}), "create user")
        password = data.get("password")
        print(f"created user {args.user} (not a superuser)")
    ensure_team(admin, args.pull_org, "otk-pull", "member", args.user)
    ensure_team(admin, args.push_org, "otk-push", "creator", args.user, "write")

    if password is None:   # existing user: reset to a random password we can sign in with once
        password = secrets.token_urlsafe(24)
        must(admin.call("PUT", f"/api/v1/superuser/users/{args.user}", {"password": password}), "reset password")
    token = app_token(args.url, args.user, password, args.insecure)
    host = args.url.split("://", 1)[1]
    # Quay app-specific tokens authenticate with the fixed username "$app"; the token identifies the user.
    auth = base64.b64encode(f"$app:{token}".encode()).decode()
    fd = os.open(args.authfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"auths": {host: {"auth": auth}}}, f)
    print(f"app token for {args.user} written to {args.authfile} (one entry for {host}); revoke it in the user's settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
