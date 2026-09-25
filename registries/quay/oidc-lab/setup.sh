#!/usr/bin/env bash
# Start Keycloak, create realm "otk" with client "quay" and two users (alice = a person and Quay superuser,
# svc-otk = the service account), then start a Quay that authenticates against it (AUTHENTICATION_TYPE: OIDC).
set -euo pipefail
cd "$(dirname "$0")"
kcpass=${KC_ADMIN_PASSWORD:-otk-lab-kc-admin}
mkdir -p .state/quay .state/tls
if [ ! -s .state/tls/kc.crt ]; then   # throwaway CA + Keycloak certificate; Quay refuses a plain-HTTP OIDC server
  ( cd .state/tls
    printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n' > ca.ext
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:keycloak\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n' > kc.ext
    openssl req -newkey rsa:2048 -nodes -subj "/CN=otk lab CA" -keyout ca.key -out ca.csr 2>/dev/null
    openssl x509 -req -in ca.csr -signkey ca.key -days 30 -extfile ca.ext -out ca.crt 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj "/CN=keycloak" -keyout kc.key -out kc.csr 2>/dev/null
    openssl x509 -req -in kc.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 -extfile kc.ext -out kc.crt 2>/dev/null
    chmod 644 ca.crt kc.crt kc.key )
fi
docker compose up -d keycloak db redis tester
kc() { docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"; }
for _ in $(seq 1 60); do kc config credentials --server http://localhost:8080 --realm master --user kcadmin --password "$kcpass" >/dev/null 2>&1 && break; sleep 5; done
kc create realms -s realm=otk -s enabled=true >/dev/null 2>&1 || true
if [ ! -s .state/client-secret ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > .state/client-secret
  kc create clients -r otk -s clientId=quay -s enabled=true -s publicClient=false -s "secret=$(cat .state/client-secret)" \
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=true -s 'redirectUris=["http://quay:8080/*"]' >/dev/null
fi
for u in alice svc-otk; do
  kc create users -r otk -s username=$u -s enabled=true -s emailVerified=true -s email=$u@example.invalid \
    -s firstName=$u -s lastName=lab >/dev/null 2>&1 || true
  kc set-password -r otk --username $u --new-password "otk-lab-$u-pass" >/dev/null
done
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
cat > .state/quay/config.yaml <<YAML
SERVER_HOSTNAME: "quay:8080"
PREFERRED_URL_SCHEME: http
SETUP_COMPLETE: true
AUTHENTICATION_TYPE: OIDC
KEYCLOAK_LOGIN_CONFIG:
  CLIENT_ID: quay
  CLIENT_SECRET: "$(cat .state/client-secret)"
  OIDC_SERVER: https://keycloak:8443/realms/otk/
  SERVICE_NAME: Keycloak
  LOGIN_SCOPES: [openid, email, profile]
  PREFERRED_USERNAME_CLAIM_NAME: preferred_username
FEATURE_USER_CREATION: true
FEATURE_USERNAME_CONFIRMATION: false
FEATURE_MAILING: false
FEATURE_SECURITY_SCANNER: false
FEATURE_PROXY_CACHE: true
FEATURE_EXTENDED_REPOSITORY_NAMES: true
SUPER_USERS: [alice]
SECRET_KEY: "$(rand)"
DATABASE_SECRET_KEY: "$(rand)"
DB_URI: "postgresql://quay:quaypass@db:5432/quay"
DB_CONNECTION_ARGS: {autorollback: true, threadlocals: true}
BUILDLOGS_REDIS: {host: redis, port: 6379}
USER_EVENTS_REDIS: {host: redis, port: 6379}
DISTRIBUTED_STORAGE_CONFIG:
  default: [LocalStorage, {storage_path: /datastorage/registry}]
DISTRIBUTED_STORAGE_PREFERENCE: [default]
DISTRIBUTED_STORAGE_DEFAULT_LOCATIONS: []
LOGS_MODEL: database
YAML
chmod 644 .state/quay/config.yaml
mkdir -p .state/quay/extra_ca_certs && cp .state/tls/ca.crt .state/quay/extra_ca_certs/otk-lab-ca.crt   # Quay trusts the IdP's CA
docker compose up -d quay
for _ in $(seq 1 90); do
  docker compose exec -T tester python3 -c 'import urllib.request,sys
try: urllib.request.urlopen("http://quay:8080/v2/", timeout=5)
except urllib.error.HTTPError as e: sys.exit(0 if e.code == 401 else 1)
except Exception: sys.exit(1)' 2>/dev/null && break
  sleep 5
done
echo "keycloak realm otk ready, quay (OIDC) up"
