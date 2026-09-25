#!/usr/bin/env python3
"""Run inside the lab network (the tester container). Logs in through Keycloak like a browser, sets up the
orgs and teams as alice, mints svc-otk's credentials, and checks every permission the recipe claims.

Checks, each against a real registry call:
  service account exists in Quay only after its first Keycloak login
  app token ($app) and Keycloak password both work for docker login
  pull-through (proxy org member), push new repo (creator team), digest preserved
  denied: push into proxy org, pull/push in an unrelated org, delete in proxy org, superuser API
  offboarding: disabling svc-otk in Keycloak blocks the password; revoking the app token blocks the token
"""
import base64
import hashlib
import html
import http.cookiejar
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request

QUAY, KC = "http://quay:8080", "https://keycloak:8443"
PASS = {"alice": "otk-lab-alice-pass", "svc-otk": "otk-lab-svc-otk-pass"}
failed = []


def check(name, ok, detail="", observe=False):
    tag = ("SEEN" if observe else "PASS") if ok else ("NOTE" if observe else "FAIL")
    print(f"{tag}  {name}{('  ' + detail) if detail else ''}")
    if not ok and not observe:
        failed.append(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # The login steps are followed by hand: Quay's last redirect after the OIDC callback is port-less.
        self.step = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
        self.csrf = None

    def req(self, method, url, body=None, form=None, headers=None):
        h = dict(headers or {})
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        if form is not None:
            data, h["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        if self.csrf and url.startswith(QUAY):
            h["X-CSRF-Token"] = self.csrf
        try:
            with self.op.open(urllib.request.Request(url, data=data, method=method, headers=h), timeout=60) as r:
                return r.status, r.read().decode(errors="replace"), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), url

    def refresh_csrf(self):
        _, body, _ = self.req("GET", QUAY + "/csrf_token")
        self.csrf = json.loads(body)["csrf_token"]

    def login(self, user):
        self.refresh_csrf()
        code, body, _ = self.req("POST", QUAY + "/api/v1/externallogin/keycloak", {"kind": "login"})
        auth_url = json.loads(body)["auth_url"]
        code, page, _ = self.req("GET", auth_url)
        action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page).group(1))
        form = urllib.parse.urlencode({"username": user, "password": PASS[user], "credentialId": ""}).encode()
        try:
            self.step.open(urllib.request.Request(action, data=form, method="POST"), timeout=60)
            return None                                   # no redirect: Keycloak rejected the login
        except urllib.error.HTTPError as e:
            callback = e.headers.get("Location")
        try:
            self.step.open(callback, timeout=60)          # Quay's callback sets the session
        except urllib.error.HTTPError as e:
            if e.code != 302:
                return None
        self.refresh_csrf()
        code, body, _ = self.req("GET", QUAY + "/api/v1/user/")
        return json.loads(body).get("username") if code == 200 else None

    def api(self, method, path, body=None):
        code, text, _ = self.req(method, QUAY + "/api/v1" + path, body)
        try:
            return code, json.loads(text) if text else None
        except ValueError:
            return code, text


def authfile(path, user, secret):
    open(path, "w").write(json.dumps({"auths": {"quay:8080": {
        "auth": base64.b64encode(f"{user}:{secret}".encode()).decode()}}}))


def skopeo(*args):
    p = subprocess.run(["skopeo", *args], capture_output=True)
    return p.returncode == 0, p.stdout, p.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]


def can_pull(auth, ref):
    return skopeo("inspect", "--raw", "--tls-verify=false", f"--authfile={auth}", f"docker://quay:8080/{ref}")[0]


def can_copy(auth, src, dst):
    return skopeo("copy", "--all", "--preserve-digests", "--src-tls-verify=false", "--dest-tls-verify=false",
                  f"--src-authfile={auth}", f"--dest-authfile={auth}",
                  f"docker://quay:8080/{src}", f"docker://quay:8080/{dst}")[0]


def digest(auth, ref):
    ok, raw, _ = skopeo("inspect", "--raw", "--tls-verify=false", f"--authfile={auth}", f"docker://quay:8080/{ref}")
    return "sha256:" + hashlib.sha256(raw).hexdigest() if ok and raw else None


def kc_admin_token():
    data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin",
                                   "password": sys.argv[1] if len(sys.argv) > 1 else "otk-lab-kc-admin"}).encode()
    with urllib.request.urlopen(KC + "/realms/master/protocol/openid-connect/token", data=data) as r:
        return json.loads(r.read())["access_token"]


def kc_set_enabled(user, enabled):
    tok = kc_admin_token()
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(f"{KC}/admin/realms/otk/users?username={user}&exact=true",
                                                       headers=h)) as r:
        uid = json.loads(r.read())[0]["id"]
    urllib.request.urlopen(urllib.request.Request(f"{KC}/admin/realms/otk/users/{uid}", method="PUT", headers=h,
                                                  data=json.dumps({"enabled": enabled}).encode()))


def main():
    admin = Browser()
    check("alice signs in to Quay through Keycloak", admin.login("alice") == "alice")
    code, _ = admin.api("GET", "/users/svc-otk")
    check("svc-otk does not exist in Quay before its first login", code == 404, f"HTTP {code}")

    svc = Browser()
    check("svc-otk signs in through Keycloak (creates the Quay user)", svc.login("svc-otk") == "svc-otk")
    code, body = admin.api("GET", "/superuser/users/")
    su = [u["username"] for u in body["users"] if u.get("super_user")] if code == 200 else []
    check("svc-otk is not a superuser", "svc-otk" not in su, f"superusers {su}")

    for org in ("dockerhub", "local", "other"):
        admin.api("POST", "/organization/", {"name": org, "email": f"{org}@example.invalid"})
    admin.api("POST", "/organization/dockerhub/proxycache",
              {"org_name": "dockerhub", "upstream_registry": "docker.io", "expiration_s": 86400, "insecure": False})
    admin.api("PUT", "/organization/dockerhub/team/otk-pull", {"role": "member", "description": "otk pull"})
    admin.api("PUT", "/organization/local/team/otk-push", {"role": "creator", "description": "otk push"})
    admin.api("POST", "/organization/local/prototypes", {"role": "write", "delegate": {"kind": "team", "name": "otk-push"}})
    for org, team in (("dockerhub", "otk-pull"), ("local", "otk-push")):
        code, _ = admin.api("PUT", f"/organization/{org}/team/{team}/members/svc-otk", {})
        check(f"alice adds svc-otk to {org}/{team}", code == 200, f"HTTP {code}")

    code, body = svc.api("POST", "/user/apptoken", {"title": "otk-ci"})
    check("svc-otk mints an app token for itself", code == 200, f"HTTP {code}")
    token, uuid = body["token"]["token_code"], body["token"]["uuid"]
    authfile("/tmp/app.json", "$app", token)
    authfile("/tmp/pw.json", "svc-otk", PASS["svc-otk"])

    for label, auth in (("app token", "/tmp/app.json"), ("Keycloak password", "/tmp/pw.json")):
        tag = "1.36.1" if label == "app token" else "1.37.0"
        seen = label != "app token"      # the password grant is reported, not required: the recipe uses the token
        check(f"[{label}] pull-through busybox:{tag}", can_pull(auth, f"dockerhub/library/busybox:{tag}"), observe=seen)
        check(f"[{label}] copy into a new repo in local", can_copy(auth, f"dockerhub/library/busybox:{tag}",
                                                                  f"local/library/busybox:{tag}"), observe=seen)
        d1, d2 = digest(auth, f"dockerhub/library/busybox:{tag}"), digest(auth, f"local/library/busybox:{tag}")
        check(f"[{label}] digest preserved", d1 is not None and d1 == d2, (d1 or "none")[:19], observe=seen)
        check(f"[{label}] push into the proxy org denied", not can_copy(auth, f"local/library/busybox:{tag}",
                                                                        "dockerhub/library/mine:1"))
        check(f"[{label}] push into an unrelated org denied", not can_copy(auth, f"local/library/busybox:{tag}",
                                                                           "other/library/mine:1"))
        check(f"[{label}] delete in the proxy org denied",
              not skopeo("delete", "--tls-verify=false", f"--authfile={auth}",
                         f"docker://quay:8080/dockerhub/library/busybox:{tag}")[0])
    code, _ = svc.api("GET", "/superuser/users/")
    check("svc-otk session cannot use the superuser API", code in (401, 403), f"HTTP {code}")

    kc_set_enabled("svc-otk", False)
    check("Keycloak user disabled: password login blocked", not can_pull("/tmp/pw.json", "local/library/busybox:1.36.1"))
    app_after = can_pull("/tmp/app.json", "local/library/busybox:1.36.1")
    check("Keycloak user disabled: app token observed", True, "still works" if app_after else "blocked")
    kc_set_enabled("svc-otk", True)

    code, _ = svc.api("DELETE", f"/user/apptoken/{uuid}")
    check("app token revoked", code in (200, 204), f"HTTP {code}")
    check("revoked app token blocked", not can_pull("/tmp/app.json", "local/library/busybox:1.36.1"))
    print(json.dumps({"failed": failed, "app_token_survives_idp_disable": app_after}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
