#!/usr/bin/env bash
# CI integration tests: MySQL, RabbitMQ and Mailpit run as GitLab services. This declares
# the broker topology, migrates, starts the user-service stub, the worker and the API, then
# runs tests/integration against them. Logs are kept as job artifacts.
set -euo pipefail

python scripts/ci/declare_test_topology.py
alembic upgrade head

PORT=8000 python tests/stubs/user_service.py > stub.log 2>&1 &
python -m app.worker > runner.log 2>&1 &
RUNNER=$!
uvicorn app.main:app --port 8080 --no-access-log > api.log 2>&1 &
API=$!
trap 'kill $RUNNER $API 2>/dev/null || true' EXIT

for url in http://127.0.0.1:8000/health http://127.0.0.1:8080/health; do
  for _ in $(seq 30); do
    python -c "import urllib.request; urllib.request.urlopen('$url', timeout=1)" 2>/dev/null && break
    sleep 1
  done
done

python -m pytest tests/integration -m integration -p no:cacheprovider --junitxml=integration-report.xml
