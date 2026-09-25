# Quay service account for otk

One non-person Quay account that pulls through a proxy-cache organisation and pushes into another organisation with a single credential. Keycloak/OIDC steps: [QUICKSTART.md](QUICKSTART.md).

## Requirements

- Quay 3.x with `FEATURE_PROXY_CACHE` and `FEATURE_APP_SPECIFIC_TOKENS` (default on).
- Python 3.11 or later for `service-account.py`.
- An admin OAuth token (`QUAY_ADMIN_TOKEN`): a superuser, or an admin of both organisations with `--teams-only`.

## Flags

Full set: `python3 service-account.py --help`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `--url` | none | Quay base URL |
| Required | `--pull-org` | none | Proxy-cache organisation, e.g. `dockerhub` |
| Required | `--push-org` | none | Organisation images are copied into |
| Optional | `--user` | `svc-otk` | Service account name |
| Optional | `--authfile` | `auth.json` | Containers auth file to write (Database auth only) |
| When OIDC | `--teams-only` | off | Grant only; the user exists from its first login and mints its token in the UI |

## Usage

```sh
QUAY_ADMIN_TOKEN=<token> python3 service-account.py --url https://quay.example.internal \
  --pull-org dockerhub --push-org local --teams-only
```

## Behaviour

What the registry checks, each proven with and without the grant:

| Action | Grant it needs |
|---|---|
| Pull through the cache, including images never pulled before | Membership of any team in the proxy-cache organisation (role `member`) |
| Create a repository and push in the other organisation | Team role `creator` there |
| Push to a repository someone else created | Default permission `write` for that team |
| Anything in a third organisation, deleting in the cache, the superuser API | Nothing granted, all refused |

- Registry credentials are per host, so one auth entry covers both organisations. Log in with username `$app` and the app token.
- Robot accounts belong to one organisation, so a robot pair means one credential per organisation.
- Disabling the user in the identity provider does not stop an app token. Offboard by revoking the token in Quay as well.
- Rotation: mint a new token, update the stored auth file, revoke the old token.

## Out of scope

- Creating the organisations and the proxy-cache configuration.
- Storing the auth file: a GitLab file variable (`OTK_AUTHFILE`) or your secret store.

## Expected result

```sh
skopeo copy --all --preserve-digests --authfile auth.json \
  docker://quay.example.internal/dockerhub/library/alpine:3.21.3 docker://quay.example.internal/local/library/alpine:3.21.3
```

succeeds, and the manifest digest is the same in both organisations.
