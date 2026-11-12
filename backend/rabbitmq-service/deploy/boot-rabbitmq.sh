#!/usr/bin/env bash
# First-boot setup of the dedicated broker instance (deploy/terraform), run by its user data:
#   1. install Docker and jq
#   2. mount the data volume at /data, formatting it only if it is brand new
#   3. once the broker's secret is readable, start the last deployed image (or :latest)
#
# Settings come from /opt/ticketto/rabbitmq.env, written by the user data. Safe to re-run.
# If there is no image in ECR yet (the very first apply), it starts nothing and exits 0:
# the pipeline's first deploy-broker job starts the broker.
set -euo pipefail # no -x: run-rabbitmq.sh handles the password

CONFIG=/opt/ticketto/rabbitmq.env
# shellcheck source=/dev/null
source "$CONFIG"
: "${AWS_REGION:?set in $CONFIG}" "${ECR_REPOSITORY_URI:?set in $CONFIG}"
: "${RABBITMQ_SECRET_ID:?set in $CONFIG}" "${DATA_VOLUME_ID:?set in $CONFIG}"

log() { echo "[boot-rabbitmq] $*"; }

# ---- 1. packages (the AWS CLI is already on Amazon Linux 2023) ----
dnf install -y docker jq
systemctl enable --now docker

# ---- 2. data volume (attached after boot, so wait for it) ----
device="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOLUME_ID//-/}"
for _ in $(seq 1 60); do
  [[ -e "$device" ]] && break
  sleep 5
done
[[ -e "$device" ]] || { log "data volume $DATA_VOLUME_ID never appeared"; exit 1; }
if ! blkid "$device" >/dev/null; then
  log "formatting the new data volume"
  mkfs -t xfs "$device"
fi
mkdir -p /data
grep -q "$device" /etc/fstab || echo "$device /data xfs defaults,nofail 0 2" >>/etc/fstab
mount -a
mkdir -p /data/rabbitmq

# ---- 3. the broker ----
# OpenTofu writes the secret after the instance exists (it holds its private IP), so wait.
for _ in $(seq 1 60); do
  aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$RABBITMQ_SECRET_ID" \
    --query Name --output text >/dev/null 2>&1 && break
  sleep 10
done

# The last deployed tag is on the data volume, so a replacement instance runs the same image.
tag=$(cat /data/rabbitmq-service.tag 2>/dev/null || echo latest)
if /opt/ticketto/run-rabbitmq.sh "$tag"; then
  log "broker running $tag"
else
  log "rabbitmq-service:$tag did not start (normal before the first deploy-broker job)"
fi
