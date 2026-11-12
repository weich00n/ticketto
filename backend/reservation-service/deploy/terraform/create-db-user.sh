#!/usr/bin/env bash
# Creates reservation-service's MySQL user and schema on the shared RDS instance, once, after
# the first `tofu apply`. Logs in as the RDS master (its Secrets Manager login) and gives the
# user the password tofu stored in reservation-service/db. Safe to re-run: it resets the
# password to the stored one.
#
# Needs: the mysql client, AWS credentials, jq, and RDS reachable from where you run it.
# Run from deploy/terraform:  ./create-db-user.sh
set -euo pipefail
cd "$(dirname "$0")"

host=$(tofu output -raw rds_endpoint)
master_arn=$(tofu output -raw rds_master_secret_arn)
db_arn=$(tofu output -raw db_secret_arn)

master=$(aws secretsmanager get-secret-value --secret-id "$master_arn" --query SecretString --output text)
service=$(aws secretsmanager get-secret-value --secret-id "$db_arn" --query SecretString --output text)
user=$(jq -r .username <<<"$service")
password=$(jq -r .password <<<"$service")

# The master password goes in through the environment, so it never shows in `ps`.
MYSQL_PWD=$(jq -r .password <<<"$master") mysql --host="$host" --user="$(jq -r .username <<<"$master")" \
  --ssl-mode=REQUIRED <<SQL
CREATE DATABASE IF NOT EXISTS db_reservation CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$user'@'%' IDENTIFIED BY '$password';
ALTER USER '$user'@'%' IDENTIFIED BY '$password';
GRANT ALL PRIVILEGES ON db_reservation.* TO '$user'@'%';
SQL
echo "MySQL user $user can use db_reservation on $host"
