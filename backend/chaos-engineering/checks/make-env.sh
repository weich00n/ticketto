#!/bin/bash
# Builds checks/.env (mode 600) for the checker from the same secrets the services use.
# Prints hostnames only, never passwords. Every URL points at a tunnel from tunnels.sh.
set -euo pipefail
export AWS_REGION=us-east-1
S="$(cd "$(dirname "$0")" && pwd)"
umask 077
aws s3 cp s3://queue-service-staging-config-708677596839/queue-service/staging/app.env "$S/queue.env" --quiet
trap 'rm -f "$S/queue.env"' EXIT
RDB=$(aws secretsmanager get-secret-value --secret-id reservation-service/db --query SecretString --output text)
RRD=$(aws secretsmanager get-secret-value --secret-id reservation-service/redis --query SecretString --output text)
RDB="$RDB" RRD="$RRD" Q="$S/queue.env" OUT="$S/.env" /usr/bin/python3 - <<'EOF'
import json, os, urllib.parse as u
q = dict(l.split("=", 1) for l in open(os.environ["Q"]).read().splitlines() if "=" in l and not l.startswith("#"))
db = json.loads(os.environ["RDB"]); rd = json.loads(os.environ["RRD"])
qm = q["MYSQL_URL"].strip().strip('"').replace("mysql+asyncmy://", "mysql://")
qm = qm.replace(u.urlsplit(qm).netloc.rsplit("@", 1)[1], "127.0.0.1:13306")  # RDS tunnel
if "ssl" not in qm:
    qm += ("&" if "?" in qm else "?") + "ssl=true"
qr = q["REDIS_URL"].strip().strip('"')
qr = qr.replace(u.urlsplit(qr).netloc.rsplit("@", 1)[-1], "127.0.0.1:16380")  # queue Redis tunnel
lines = {
    "RESERVATION_DB_URL": f"mysql://{u.quote(db['username'], safe='')}:{u.quote(db['password'], safe='')}@127.0.0.1:13306/db_reservation?ssl=true",
    "RESERVATION_REDIS_URL": f"redis://:{u.quote(rd['password'], safe='')}@127.0.0.1:16379/0",
    "QUEUE_DB_URL": qm,
    "QUEUE_REDIS_URL": qr,
}
with open(os.environ["OUT"], "w") as f:
    for k, v in lines.items():
        f.write(f"{k}={v}\n")
for k, v in lines.items():
    p = u.urlsplit(v)
    print(k, f"{p.hostname}:{p.port}{p.path}")
EOF
ls -l "$S/.env"
