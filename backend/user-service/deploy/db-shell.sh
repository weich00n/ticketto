#!/usr/bin/env bash
# Open a MySQL shell on user-service's database in RDS, as the app's own user, from your laptop.
#
#   ./deploy/db-shell.sh                          interactive shell on db_user
#   ./deploy/db-shell.sh -e 'SELECT * FROM users' run one query and exit
#   ADMIN=1 ./deploy/db-shell.sh                  as the RDS master user (sees every service's DB)
#
# Passwords come from Secrets Manager and are never printed. Needs the AWS CLI, jq and the
# mysql client, and RDS must be running and reachable on 3306 from your IP.
set -euo pipefail

RDS_ID=${RDS_ID:-db-ticketing}
APP_SECRET_ID=${APP_SECRET_ID:-user-service/db}

APP=$(aws secretsmanager get-secret-value --secret-id "$APP_SECRET_ID" --query SecretString --output text)
if [[ -n "${ADMIN:-}" ]]; then
  SECRET_ARN=$(aws rds describe-db-instances --db-instance-identifier "$RDS_ID" \
    --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text)
  CREDS=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text)
else
  CREDS=$APP
fi

# The password goes in a temporary option file readable only by you, not on the command line.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -sSfo "$TMP/rds-ca.pem" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
(umask 077; printf '[client]\nhost=%s\nport=%s\nuser=%s\npassword=%s\nssl-ca=%s\nssl-mode=VERIFY_IDENTITY\n' \
  "$(jq -r .host <<<"$APP")" "$(jq -r .port <<<"$APP")" \
  "$(jq -r .username <<<"$CREDS")" "$(jq -r .password <<<"$CREDS")" "$TMP/rds-ca.pem" > "$TMP/my.cnf")

mysql --defaults-extra-file="$TMP/my.cnf" --database="$(jq -r .dbname <<<"$APP")" "$@"
