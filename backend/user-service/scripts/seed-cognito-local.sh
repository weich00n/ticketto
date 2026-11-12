#!/usr/bin/env bash
# Creates the local Cognito pool, app client, organiser group and two test users in
# cognito-local (cognito-settings.md §8.2-8.3). Safe to re-run: reuses an existing pool.
#
# Usage: docker compose up -d cognito-local && ./scripts/seed-cognito-local.sh
set -euo pipefail

export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_REGION=us-east-1 AWS_PAGER=""
ENDPOINT="${COGNITO_LOCAL_URL:-http://localhost:9229}"
cog() { aws cognito-idp --endpoint-url "$ENDPOINT" "$@"; }

POOL_NAME=ticketto-local
PASSWORD='Passw0rd!'

POOL_ID=$(cog list-user-pools --max-results 60 \
  --query "UserPools[?Name=='$POOL_NAME'].Id | [0]" --output text)
if [[ -z "$POOL_ID" || "$POOL_ID" == "None" ]]; then
  POOL_ID=$(cog create-user-pool --pool-name "$POOL_NAME" \
    --username-attributes email --auto-verified-attributes email \
    --query 'UserPool.Id' --output text)
  echo "Created user pool $POOL_ID"
else
  echo "Reusing user pool $POOL_ID"
fi

CLIENT_ID=$(cog list-user-pool-clients --user-pool-id "$POOL_ID" \
  --query "UserPoolClients[?ClientName=='ticketto-local-web'].ClientId | [0]" --output text)
if [[ -z "$CLIENT_ID" || "$CLIENT_ID" == "None" ]]; then
  CLIENT_ID=$(cog create-user-pool-client --user-pool-id "$POOL_ID" \
    --client-name ticketto-local-web \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --query 'UserPoolClient.ClientId' --output text)
fi

cog create-group --user-pool-id "$POOL_ID" --group-name organiser >/dev/null 2>&1 || true

create_user() {
  local email=$1 name=$2
  cog admin-create-user --user-pool-id "$POOL_ID" --username "$email" \
    --user-attributes Name=email,Value="$email" Name=name,Value="$name" Name=email_verified,Value=true \
    --message-action SUPPRESS >/dev/null 2>&1 || true
  cog admin-set-user-password --user-pool-id "$POOL_ID" --username "$email" \
    --password "$PASSWORD" --permanent
}

create_user buyer@test.com "Test Buyer"
create_user organiser@test.com "Test Organiser"
cog admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username organiser@test.com --group-name organiser

ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^COGNITO_USER_POOL_ID=' "$ENV_FILE"; then
    sed -i.bak "s|^COGNITO_USER_POOL_ID=.*|COGNITO_USER_POOL_ID=$POOL_ID|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    echo "COGNITO_USER_POOL_ID=$POOL_ID" >> "$ENV_FILE"
  fi
  ENV_NOTE="Updated COGNITO_USER_POOL_ID in .env."
else
  ENV_NOTE="No .env found: copy .env.example to .env and set COGNITO_USER_POOL_ID=$POOL_ID."
fi

cat <<OUT

Done. Test users (password: $PASSWORD): buyer@test.com, organiser@test.com
$ENV_NOTE
  COGNITO_USER_POOL_ID=$POOL_ID
  COGNITO_CLIENT_ID=$CLIENT_ID   # for the Next.js app

If user-service is already running, recreate it so it picks up the new pool ID:
  docker compose up -d user-service
OUT
