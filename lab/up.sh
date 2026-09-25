#!/usr/bin/env bash
# Bring up the otk lab: two Quays, two NiFis, the folder that stands in for the diode, and the importer.
# Idempotent. Needs docker with compose and python3 on the host.
#   DOCKER_CONTEXT=<ctx> lab/up.sh
set -euo pipefail
cd "$(dirname "$0")"
user=${QUAY_USER:-admin}; pass=${QUAY_PASSWORD:-otk-lab-quay-admin}
export NIFI_PASSWORD=${NIFI_PASSWORD:-otk-lab-nifi-admin}
LOW_PORT=${LOW_PORT:-28081}; HIGH_PORT=${HIGH_PORT:-28082}

mkdir -p data/outbox data/diode data/inbox .state/low .state/high .state/auth
chmod 777 data/* .state/low .state/high .state/auth     # NiFi and otk run as uid 1000

quay_config() {   # side -> quay/<side>/config.yaml; hostname = compose service name so token realms resolve in-network
  local side=$1 dir=quay/$1
  [ -s "$dir/config.yaml" ] && return
  mkdir -p "$dir"
  rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
  cat > "$dir/config.yaml" <<YAML
SERVER_HOSTNAME: "$side-quay:8080"
PREFERRED_URL_SCHEME: http
SETUP_COMPLETE: true
AUTHENTICATION_TYPE: Database
FEATURE_USER_INITIALIZE: true
FEATURE_USER_CREATION: false
FEATURE_DIRECT_LOGIN: true
FEATURE_MAILING: false
FEATURE_SECURITY_SCANNER: false
FEATURE_PROXY_CACHE: true
FEATURE_EXTENDED_REPOSITORY_NAMES: true
CREATE_PRIVATE_REPO_ON_PUSH: true
CREATE_NAMESPACE_ON_PUSH: false
SUPER_USERS: [$user]
SECRET_KEY: "$(rand)"
DATABASE_SECRET_KEY: "$(rand)"
DB_URI: "postgresql://quay:quaypass@$side-db:5432/quay"
DB_CONNECTION_ARGS: {autorollback: true, threadlocals: true}
BUILDLOGS_REDIS: {host: $side-redis, port: 6379}
USER_EVENTS_REDIS: {host: $side-redis, port: 6379}
DISTRIBUTED_STORAGE_CONFIG:
  default: [LocalStorage, {storage_path: /datastorage/registry}]
DISTRIBUTED_STORAGE_PREFERENCE: [default]
DISTRIBUTED_STORAGE_DEFAULT_LOCATIONS: []
LOGS_MODEL: database
YAML
  chmod 644 "$dir/config.yaml"
}
quay_config low; quay_config high

docker compose up -d --build low-db low-redis low-quay high-db high-redis high-quay nifi-low nifi-high

quay_init() {   # side url org [proxy-upstream]
  local side=$1 url=$2 org=$3 upstream=${4:-} tok=.state/$1.token code=000
  for _ in $(seq 1 90); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$url/v2/")" = 401 ] && break; sleep 5; done
  if [ ! -s "$tok" ]; then
    for _ in $(seq 1 30); do
      code=$(curl -s -o .state/init.json -w '%{http_code}' -X POST "$url/api/v1/user/initialize" -H 'Content-Type: application/json' \
        -d "{\"username\":\"$user\",\"password\":\"$pass\",\"email\":\"$user@example.com\",\"access_token\":true}")
      [ "$code" = 200 ] && break; sleep 5
    done
    [ "$code" = 200 ] || { echo "$side: initialize failed ($code)" >&2; exit 1; }
    (umask 077; python3 -c 'import json;print(json.load(open(".state/init.json"))["access_token"])' > "$tok")
    : > .state/init.json
  fi
  local auth="Authorization: Bearer $(cat "$tok")"
  curl -fs -H "$auth" "$url/api/v1/organization/$org" >/dev/null 2>&1 || \
    curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' "$url/api/v1/organization/" \
      -d "{\"name\":\"$org\",\"email\":\"$org@example.com\"}" >/dev/null
  if [ -n "$upstream" ]; then
    curl -fs -H "$auth" "$url/api/v1/organization/$org/proxycache" | grep -q "$upstream" || \
      curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' "$url/api/v1/organization/$org/proxycache" \
        -d "{\"org_name\":\"$org\",\"upstream_registry\":\"$upstream\",\"expiration_s\":86400,\"insecure\":false}" >/dev/null
  fi
  echo "$side: $url ready, organisation $org${upstream:+ (pull-through cache of $upstream)}"
}
quay_init low  "http://localhost:$LOW_PORT"  proxy-hub docker.io
quay_init high "http://localhost:$HIGH_PORT" hub

for side in low high; do    # registry credentials for otk, written by skopeo login inside the otk image
  [ -s ".state/auth/$side.json" ] && continue
  docker compose run --rm --no-deps -v "$PWD/.state/auth:/authw" --entrypoint skopeo otk-high \
    login --tls-verify=false --authfile "/authw/$side.json" -u "$user" -p "$pass" "$side-quay:8080" >/dev/null
  echo "$side: registry credentials in .state/auth/$side.json"
done

docker compose up -d otk-high
python3 ../nifi/flow.py --side low  --url "https://localhost:${NIFI_LOW_PORT:-28090}"  --insecure --export ../nifi/otk-low.json
python3 ../nifi/flow.py --side high --url "https://localhost:${NIFI_HIGH_PORT:-28091}" --insecure --export ../nifi/otk-high.json
echo "lab up: pack with 'docker compose -f lab/compose.yaml run --rm otk-low pack'"
