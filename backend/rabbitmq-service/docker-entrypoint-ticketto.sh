#!/usr/bin/env bash
# Ticketto entrypoint: prepares the broker's users and Erlang cookie, then hands over to the
# official image's docker-entrypoint.sh unchanged.
#
# Users: when RabbitMQ imports definitions at boot it does not create the default `guest`
# user, so the users are written to 20-users.json here, from environment variables:
#   - the admin user: RABBITMQ_ADMIN_USER / RABBITMQ_ADMIN_PASS (required)
#   - one least-privilege user per service, listed with its permissions in
#     /etc/rabbitmq/ticketto-service-users.tsv (generated from topology/messages.yaml).
#     Its password is RABBITMQ_PASS_<SERVICE> (event-service -> RABBITMQ_PASS_EVENT_SERVICE),
#     or RABBITMQ_SERVICE_PASS_DEFAULT if that is set (local development only). A service
#     with neither is skipped, with a warning.
# Passwords never go into git or the image. The file is rewritten on every start, so a
# rotated password takes effect on the next restart.
#
# Cookie: CLI tools (health checks, `docker exec rabbitmqctl ...`) run as root create
# /var/lib/rabbitmq/.erlang.cookie if it is missing. If one does that before the server
# has created it, the server can't read the root-owned file and fails to start. Creating
# it here, before the server starts, removes that race.
set -euo pipefail

DEFINITIONS_DIR=/etc/rabbitmq/definitions
USERS_FILE="$DEFINITIONS_DIR/20-users.json"
SERVICE_USERS=/etc/rabbitmq/ticketto-service-users.tsv
COOKIE_FILE=/var/lib/rabbitmq/.erlang.cookie

# Salted SHA-256, RabbitMQ's default (rabbit_password_hashing_sha256):
#   base64(salt ++ sha256(salt ++ password)), with a random 4-byte salt.
# The password is read from the environment and never appears in a process's arguments.
hash_password() {
  local work
  work=$(mktemp -d)
  head -c 4 /dev/urandom >"$work/salt"
  printf '%s' "$1" >"$work/password"
  cat "$work/salt" "$work/password" | openssl dgst -sha256 -binary >"$work/digest"
  cat "$work/salt" "$work/digest" | base64 -w 0
  rm -rf "$work"
}

# The environment variable holding a service's password: event-service -> RABBITMQ_PASS_EVENT_SERVICE
password_variable() {
  local name=${1^^}
  echo "RABBITMQ_PASS_${name//-/_}"
}

write_users_file() {
  : "${RABBITMQ_ADMIN_USER:?RABBITMQ_ADMIN_USER must be set (the broker has no guest user)}"
  : "${RABBITMQ_ADMIN_PASS:?RABBITMQ_ADMIN_PASS must be set}"
  # The name goes into JSON unescaped, so only allow characters that need no escaping.
  if [[ ! "$RABBITMQ_ADMIN_USER" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "RABBITMQ_ADMIN_USER may only contain letters, digits, '.', '_' and '-'" >&2
    exit 1
  fi
  if (( ${#RABBITMQ_ADMIN_PASS} < 8 )); then
    echo "RABBITMQ_ADMIN_PASS must be at least 8 characters" >&2
    exit 1
  fi

  local users permissions topic_permissions
  users=$(printf '    {"name": "%s", "password_hash": "%s", "hashing_algorithm": "rabbit_password_hashing_sha256", "tags": ["administrator"], "limits": {}}' \
    "$RABBITMQ_ADMIN_USER" "$(hash_password "$RABBITMQ_ADMIN_PASS")")
  permissions=$(printf '    {"user": "%s", "vhost": "/", "configure": ".*", "write": ".*", "read": ".*"}' \
    "$RABBITMQ_ADMIN_USER")
  topic_permissions=""

  # One user per service. The TSV's regexes are already JSON-escaped; names are [a-z.-].
  local user configure write read topic_exchange topic_write variable password
  while IFS=$'\t' read -r user configure write read topic_exchange topic_write; do
    [[ -z "$user" || "$user" == \#* ]] && continue
    variable=$(password_variable "$user")
    password=${!variable:-${RABBITMQ_SERVICE_PASS_DEFAULT:-}}
    if [[ -z "$password" ]]; then
      echo "ticketto: no password for service user $user (set $variable); not created" >&2
      continue
    fi
    if (( ${#password} < 8 )); then
      echo "$variable must be at least 8 characters" >&2
      exit 1
    fi
    # No tags: service users can't use the management UI or HTTP API.
    users+=$(printf ',\n    {"name": "%s", "password_hash": "%s", "hashing_algorithm": "rabbit_password_hashing_sha256", "tags": [], "limits": {}}' \
      "$user" "$(hash_password "$password")")
    permissions+=$(printf ',\n    {"user": "%s", "vhost": "/", "configure": "%s", "write": "%s", "read": "%s"}' \
      "$user" "$configure" "$write" "$read")
    if [[ "$topic_exchange" != - ]]; then
      topic_permissions+=$(printf '%s    {"user": "%s", "vhost": "/", "exchange": "%s", "write": "%s", "read": ".*"}' \
        "${topic_permissions:+,$'\n'}" "$user" "$topic_exchange" "$topic_write")
    fi
  done <"$SERVICE_USERS"

  (
    umask 077
    printf '{\n  "users": [\n%s\n  ],\n  "permissions": [\n%s\n  ],\n  "topic_permissions": [\n%s\n  ]\n}\n' \
      "$users" "$permissions" "$topic_permissions" >"$USERS_FILE"
  )
}

ensure_cookie() {
  if [[ ! -f "$COOKIE_FILE" ]]; then
    # Owner-only (umask 077), as Erlang requires.
    (umask 077 && head -c 32 /dev/urandom | base64 -w 0 | tr -dc 'A-Za-z0-9' >"$COOKIE_FILE")
  fi
}

if [[ "${1:-}" == rabbitmq-server ]]; then
  write_users_file
  ensure_cookie
  # Started as root (the default): the official entrypoint drops to the rabbitmq user, which
  # must be able to read both files. Started with --user, they already belong to that user.
  if [[ "$(id -u)" == 0 ]]; then
    chown rabbitmq:rabbitmq "$USERS_FILE" "$COOKIE_FILE"
  fi
  # The server only needs the hashes; keep the plain passwords out of its environment.
  unset RABBITMQ_ADMIN_PASS RABBITMQ_SERVICE_PASS_DEFAULT
  while read -r variable; do unset "$variable"; done < <(compgen -e | grep '^RABBITMQ_PASS_' || true)
fi

exec docker-entrypoint.sh "$@"
