#!/usr/bin/env bash
# CI integration-test job: MySQL and RabbitMQ run as GitLab CI services. Because the
# group uses per-publisher exchanges owned by the broker (rabbitmq-service topology),
# this script first imports checkout's scoped topology into the stock broker via its
# management API, then migrates the DB, starts the API and the worker, and runs the
# in-process integration suite (tests/integration/test_flows.py) against the live stack.
#
# test_broker.py is the live-broker / real-Stripe suite; it is deliberately NOT part of
# the green CI job (it needs Stripe test-mode keys and a reachable reservation-service).
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
wait_for "RabbitMQ (AMQP)" "import pika; pika.BlockingConnection(pika.URLParameters('$RABBITMQ_URL')).close()"
# The management HTTP port: a TCP connect is enough (no auth needed to probe it).
wait_for "RabbitMQ (management)" "import socket; socket.create_connection(('rabbitmq', 15672), timeout=2).close()"

# Import the checkout-scoped topology (queues, exchanges, bindings, policies) into the
# stock broker. The file is a subset of the shared definitions.json so it only creates
# checkout's queues - not queue-service's - which is what keeps the tests hermetic.
echo "Importing checkout topology into RabbitMQ..."
# Python, not curl: the python:3.12-slim job image has no curl. urlopen raises on a
# non-2xx answer, which fails the job (set -e).
python - <<'PY'
import base64, os, urllib.request

auth = base64.b64encode(
    f"{os.environ['RABBITMQ_ADMIN_USER']}:{os.environ['RABBITMQ_ADMIN_PASS']}".encode()
).decode()
with open("tests/integration/checkout-topology.json", "rb") as f:
    body = f.read()
req = urllib.request.Request(
    "http://rabbitmq:15672/api/definitions",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
)
urllib.request.urlopen(req, timeout=30).close()
PY
echo "Topology imported."

alembic upgrade head

gunicorn --bind 127.0.0.1:3002 --workers 2 'app.main:create_app()' > gunicorn.log 2>&1 &
python -m app.worker > worker.log 2>&1 &
wait_for "checkout-service" "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/ready', timeout=2)" \
  || { cat gunicorn.log worker.log; exit 1; }

python -m pytest tests/integration/test_flows.py tests/integration/test_reservation_contract.py -m integration -p no:cacheprovider --junitxml=integration-report.xml
