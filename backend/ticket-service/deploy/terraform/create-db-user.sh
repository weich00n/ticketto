#!/usr/bin/env bash
# Creates ticket-service's MySQL user and schema on the shared RDS instance, once, after the
# first `tofu apply`. Logs in as the RDS master (its Secrets Manager login) and gives the user
# the password tofu stored in ticket-service/db. Safe to re-run: it resets the password to the
# stored one. The pipeline's migrate job then creates the tables.
#
# Needs: a mysql or mariadb client, AWS credentials, jq, and RDS reachable from where you run it.
# Run from deploy/terraform:  ./create-db-user.sh
set -euo pipefail
cd "$(dirname "$0")"

host=$(tofu output -raw rds_endpoint)
db=$(tofu output -raw db_name)
master_arn=$(tofu output -raw rds_master_secret_arn)
db_arn=$(tofu output -raw db_secret_arn)

master=$(aws secretsmanager get-secret-value --secret-id "$master_arn" --query SecretString --output text)
service=$(aws secretsmanager get-secret-value --secret-id "$db_arn" --query SecretString --output text)
user=$(jq -r .username <<<"$service")
password=$(jq -r .password <<<"$service")

# MySQL's client spells "require TLS" differently from MariaDB's.
if mysql --version 2>/dev/null | grep -qi mariadb; then tls=(--ssl); else tls=(--ssl-mode=REQUIRED); fi

# The master password goes in through the environment, so it never shows in `ps`.
MYSQL_PWD=$(jq -r .password <<<"$master") mysql --host="$host" --user="$(jq -r .username <<<"$master")" \
  "${tls[@]}" <<SQL
CREATE DATABASE IF NOT EXISTS \`$db\` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$user'@'%' IDENTIFIED BY '$password';
ALTER USER '$user'@'%' IDENTIFIED BY '$password';
GRANT ALL PRIVILEGES ON \`$db\`.* TO '$user'@'%';
SQL
echo "MySQL user $user can use $db on $host"
