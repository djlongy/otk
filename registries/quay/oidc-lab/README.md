# Quay OIDC service-account lab

Keycloak with a TLS certificate from a throwaway CA, a Quay using `AUTHENTICATION_TYPE: OIDC`, and `test.py`, which signs in through Keycloak like a browser and checks every claim in `registries/quay/README.md`.

Needs Docker with compose and the `otk:dev` image (`docker build -t otk:dev .` at the repo root). `setup.sh` issues a throwaway CA and the Keycloak certificate under `.state/tls` (openssl on the host).

```sh
registries/quay/oidc-lab/setup.sh
docker compose -f registries/quay/oidc-lab/compose.yaml exec -T tester python3 /test.py
docker compose -f registries/quay/oidc-lab/compose.yaml down -v
```

Each check prints PASS or FAIL. The Keycloak-password rows print SEEN or NOTE: on Quay 3.15.7 a user created by a web login cannot also use the password grant ("Email has already been used").
