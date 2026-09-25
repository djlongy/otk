## What this is

The procedure for a Quay that signs users in through Keycloak (`AUTHENTICATION_TYPE: OIDC`): one Keycloak user, `svc-otk`, becomes a Quay account that pulls through `dockerhub` and pushes into your own organisation (`local` below), with one app token for GitLab CI. It gives no superuser rights. `registries/quay/oidc-lab/` reproduces every step and check.

## How to use it

1. In Keycloak, create user `svc-otk` in the realm Quay uses, set its email, and set a password with Temporary off.
2. Sign in to the Quay web UI once as `svc-otk` through Keycloak. This creates the Quay user.
3. As an admin of `dockerhub`: Teams, create team `otk-pull` with role Member, add `svc-otk`.
4. As an admin of `local`: create team `otk-push` with role Creator, add `svc-otk`, and add a Default Permission of Write for team `otk-push`.
5. Signed in as `svc-otk`: Account Settings, Docker CLI and other Application Tokens, create token `otk-ci`, and copy it.
6. Write the auth file: `skopeo login --authfile otk-auth.json -u '$app' quay.example.internal` and paste the token as the password.
7. Store `otk-auth.json` as the GitLab CI file variable `OTK_AUTHFILE`, masked and protected.

You know it works when `skopeo copy --all --preserve-digests --authfile otk-auth.json docker://quay.example.internal/dockerhub/library/alpine:3.21.3 docker://quay.example.internal/local/library/alpine:3.21.3` succeeds.

If it fails:
- `unauthorized` on a pull-through: `svc-otk` is not in a `dockerhub` team; repeat step 3.
- Login refused: the username must be the literal `$app`, not `svc-otk`.
