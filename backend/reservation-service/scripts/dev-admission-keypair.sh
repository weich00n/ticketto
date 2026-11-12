#!/usr/bin/env bash
# Generates a throwaway RSA keypair for running the service locally, and prints the two
# environment variables it needs.
#
# In deployment, Queue signs admission tokens with its own private key and gives us only the
# public half. This script exists so a laptop can stand in for Queue without waiting for that
# key, and so no key material is ever committed.
#
#   eval "$(scripts/dev-admission-keypair.sh)"   # exports both, for this shell
#   ./mvnw spring-boot:run
#
# The private key is printed too, so a local test client can mint tokens with it. Never use
# either of these anywhere real.
set -euo pipefail

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$workdir/private.pem" 2>/dev/null
openssl rsa -in "$workdir/private.pem" -pubout -out "$workdir/public.pem" 2>/dev/null

echo "export ADMISSION_TOKEN_ALGORITHM=RS256"
echo "export ADMISSION_TOKEN_PUBLIC_KEY='$(cat "$workdir/public.pem")'"
echo "export DEV_ADMISSION_PRIVATE_KEY='$(cat "$workdir/private.pem")'"
