#!/bin/bash
# EC2 user data for reservation-service's Redis (Amazon Linux 2023).
# Paste the whole file into Advanced details -> User data. `#!/bin/bash` must be the
# very first characters: no blank line or space before it, LF line endings only.
# The Redis password comes from Secrets Manager (reservation-service/redis, key `password`), so
# none is written here. The instance profile needs secretsmanager:GetSecretValue on that secret
# (deploy/terraform adds it to the instance's role).
set -euo pipefail

# Install and start Docker
dnf install -y docker
usermod -aG docker ec2-user
systemctl enable --now docker

# Read the Redis password (the AWS CLI and python3 ship with Amazon Linux 2023)
REDIS_PASSWORD=$(aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id reservation-service/redis --query SecretString --output text |
  python3 -c 'import sys, json; print(json.load(sys.stdin)["password"])')
# An empty --requirepass turns authentication off, so never start Redis without one.
if [ -z "$REDIS_PASSWORD" ]; then
  echo "reservation-service/redis has no password; not starting Redis" >&2
  exit 1
fi

# Create the Docker network and volumes if they do not already exist
docker network inspect redisnet >/dev/null 2>&1 ||
  docker network create redisnet

docker volume inspect redis_data >/dev/null 2>&1 ||
  docker volume create redis_data

docker volume inspect redisinsight_data >/dev/null 2>&1 ||
  docker volume create redisinsight_data

# Create Redis if it does not already exist
docker container inspect redis >/dev/null 2>&1 ||
  docker run -d \
    --restart unless-stopped \
    --name redis \
    --network redisnet \
    -p 6379:6379 \
    -v redis_data:/data \
    redis:7.4 \
    redis-server \
      --appendonly yes \
      --maxmemory-policy noeviction \
      --requirepass "$REDIS_PASSWORD"

# Create RedisInsight if it does not already exist (UI on port 5540; restrict it to My IP in the security group)
docker container inspect redisinsight >/dev/null 2>&1 ||
  docker run -d \
    --restart unless-stopped \
    --name redisinsight \
    --network redisnet \
    -p 5540:5540 \
    -v redisinsight_data:/data \
    redis/redisinsight:latest

# Attach an older Redis container (created without the network) to it
docker network connect redisnet redis 2>/dev/null || true

# Start any existing containers that are currently stopped
docker start redis redisinsight 2>/dev/null || true
