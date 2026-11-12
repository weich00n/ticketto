#!/usr/bin/env bash
# CI integration-test job: MySQL and RabbitMQ run as GitLab CI services. This script
# sets RabbitMQ up like the team broker (Ticket's queues and its own user), migrates the DB,
# starts the API and the worker in the background and runs the integration suite against them.
# Expects the variables set on the job in .gitlab-ci.yml.
set -euo pipefail

wait_for() {  # wait_for <description> <python expression that raises until ready>
  local what=$1 check=$2
  for _ in $(seq 1 60); do
    if python -c "$check" >/dev/null 2>&1; then echo "$what is ready"; return 0; fi
    sleep 2
  done
  echo "Timed out waiting for $what" >&2
  return 1
}

wait_for "MySQL" "import pymysql; pymysql.connect(host='$DB_HOST', port=int('$DB_PORT'), user='$DB_USER', password='$DB_PASSWORD', database='$DB_NAME')"
python scripts/broker/setup_broker.py --management-url http://rabbitmq:15672
wait_for "RabbitMQ" "import pika; pika.BlockingConnection(pika.URLParameters('$RABBITMQ_URL')).close()"

alembic upgrade head

gunicorn --bind 127.0.0.1:3006 --workers 2 'app.main:create_app()' > gunicorn.log 2>&1 &
python -m app.worker > worker.log 2>&1 &
wait_for "ticket-service" "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3006/ready', timeout=2)" \
  || { cat gunicorn.log worker.log; exit 1; }

python -m pytest tests/integration -m integration -p no:cacheprovider --junitxml=integration-report.xml
