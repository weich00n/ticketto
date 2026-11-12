#!/usr/bin/env bash
# CI integration-test job: MySQL and cognito-local run as GitLab CI services; this script
# creates a Cognito pool, migrates the DB, starts the service in the background and runs
# the integration suite against it. Expects the variables set on the job in .gitlab-ci.yml.
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
# cognito-local answers its root URL with an error status, so check that the port accepts connections.
wait_for "cognito-local" "import socket, urllib.parse as u; p = u.urlparse('$COGNITO_ENDPOINT'); socket.create_connection((p.hostname, p.port), 2)"

# A fresh pool per pipeline; its random ID is passed to both the service and the tests.
COGNITO_USER_POOL_ID=$(python - <<'PY'
import os, boto3
client = boto3.client(
    "cognito-idp",
    endpoint_url=os.environ["COGNITO_ENDPOINT"],
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
pool = client.create_user_pool(
    PoolName="ci", UsernameAttributes=["email"], AutoVerifiedAttributes=["email"]
)
print(pool["UserPool"]["Id"])
PY
)
export COGNITO_USER_POOL_ID IT_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
echo "Created Cognito pool $COGNITO_USER_POOL_ID"

alembic upgrade head

uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log > uvicorn.log 2>&1 &
wait_for "user-service" "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" \
  || { cat uvicorn.log; exit 1; }

python -m pytest tests/integration -m integration -p no:cacheprovider --junitxml=integration-report.xml
