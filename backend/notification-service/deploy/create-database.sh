#!/usr/bin/env bash
# One-off: create notification-service's database and MySQL user on the shared RDS instance.
# Run it from your laptop after `tofu apply` (which creates the notification-service/db secret),
# while the RDS instance is running. It needs the AWS CLI, jq and the mysql client.
#
# Passwords come from Secrets Manager and are never printed. Safe to re-run: an existing
# database is kept, and the user's password is reset to the one in the secret.
set -euo pipefail

RDS_ID=${RDS_ID:-db-ticketing}
APP_SECRET_ID=${APP_SECRET_ID:-notification-service/db}

read -r STATUS MASTER_SECRET_ARN <<<"$(aws rds describe-db-instances --db-instance-identifier "$RDS_ID" \
  --query 'DBInstances[0].[DBInstanceStatus, MasterUserSecret.SecretArn]' --output text)"
if [[ "$STATUS" != "available" ]]; then
  echo "$RDS_ID is '$STATUS'. Start it first: aws rds start-db-instance --db-instance-identifier $RDS_ID" >&2
  exit 1
fi

APP=$(aws secretsmanager get-secret-value --secret-id "$APP_SECRET_ID" --query SecretString --output text)
MASTER=$(aws secretsmanager get-secret-value --secret-id "$MASTER_SECRET_ARN" --query SecretString --output text)
HOST=$(jq -r .host <<<"$APP")
PORT=$(jq -r .port <<<"$APP")
DB_NAME=$(jq -r .dbname <<<"$APP")
APP_USER=$(jq -r .username <<<"$APP")
APP_PASSWORD=$(jq -r .password <<<"$APP")

# Credentials go in temporary option files (readable only by you), not on the command line.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -sSfo "$TMP/rds-ca.pem" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
write_cnf() { # file user password
  (umask 077; printf '[client]\nhost=%s\nport=%s\nuser=%s\npassword=%s\nssl-ca=%s\nssl-mode=VERIFY_IDENTITY\n' \
    "$HOST" "$PORT" "$2" "$3" "$TMP/rds-ca.pem" > "$1")
}
write_cnf "$TMP/admin.cnf" "$(jq -r .username <<<"$MASTER")" "$(jq -r .password <<<"$MASTER")"
write_cnf "$TMP/app.cnf" "$APP_USER" "$APP_PASSWORD"

echo "Creating database $DB_NAME and user $APP_USER on $HOST..."
mysql --defaults-extra-file="$TMP/admin.cnf" <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '$APP_USER'@'%' IDENTIFIED BY '$APP_PASSWORD' REQUIRE SSL;
ALTER USER '$APP_USER'@'%' IDENTIFIED BY '$APP_PASSWORD' REQUIRE SSL;
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$APP_USER'@'%';
SQL

echo "Checking that $APP_USER can sign in..."
mysql --defaults-extra-file="$TMP/app.cnf" --database="$DB_NAME" \
  -e 'SELECT CURRENT_USER() AS signed_in_as, DATABASE() AS current_database;'
echo "Done. The tables are created by the pipeline's migrate job (alembic upgrade head), not here."
