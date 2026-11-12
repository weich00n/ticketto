#!/usr/bin/env python3
"""Read-only API probes; optional disposable Cognito identity, deleted in finally.

No events, profiles, orders, holds or payments are created. Never prints tokens,
passwords, client secrets or API response records.
"""
import argparse
import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = "https://x0vxfeenrl.execute-api.us-east-1.amazonaws.com"
POOL = "us-east-1_2g8EYCEa1"
CLIENT = "2jr04pk573rr5pe95jm0chv47o"
MISSING = "00000000-0000-4000-8000-000000000000"


def aws(*args):
    result = subprocess.run(["aws", *args, "--output", "json"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        # AWS errors can echo inputs: don't expose them in the test log.
        raise RuntimeError(f"AWS operation failed: {args[0]} {args[1]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def probe(path, expected, token=None, forged=False):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if forged:
        headers.update({"X-User-Groups": "organiser", "X-User-Sub": MISSING,
                        "X-User-Username": "forged", "X-User-Id": MISSING})
    request = urllib.request.Request(BASE + path, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=35)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read()
    print(f"{'PASS' if response.status in expected else 'FAIL'} {path}: HTTP {response.status}")
    if response.status not in expected:
        try:
            payload = json.loads(body)
            print("Error code:", payload.get("error", {}).get("code", "unknown"))
        except (ValueError, AttributeError):
            pass
        raise AssertionError(f"{path}: expected {expected}, got {response.status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authenticated", action="store_true",
                        help="Create a suppressed-email Cognito test user and delete it afterward")
    args = parser.parse_args()
    probe("/api/v1/events", {200})
    probe(f"/api/v1/events/{MISSING}", {404})
    probe(f"/api/v1/events/{MISSING}/queue", {404})
    probe(f"/api/v1/events/{MISSING}/availability", {404})
    for path in ["/api/v1/organizer/events", "/api/v1/tickets", "/api/v1/orders",
                 "/api/v1/users/me", f"/api/v1/reservations/{MISSING}",
                 f"/api/v1/events/{MISSING}/queue/me"]:
        probe(path, {401}, forged=True)
    probe("/internal/v1/events/anything", {404})
    if not args.authenticated:
        return
    username = "gateway-smoke-" + uuid.uuid4().hex + "@example.com"
    password = secrets.token_urlsafe(30) + "aA1!"
    created = False
    try:
        secret = json.loads(aws("secretsmanager", "get-secret-value",
                               "--secret-id", "frontend/cognito")["SecretString"])["client_secret"]
        secret_hash = base64.b64encode(hmac.new(secret.encode(),
            (username + CLIENT).encode(), hashlib.sha256).digest()).decode()
        aws("cognito-idp", "admin-create-user", "--user-pool-id", POOL,
            "--username", username, "--message-action", "SUPPRESS", "--user-attributes",
            json.dumps([{"Name": "email", "Value": username},
                        {"Name": "email_verified", "Value": "true"},
                        {"Name": "name", "Value": "Disposable gateway smoke test"}]))
        created = True
        aws("cognito-idp", "admin-set-user-password", "--user-pool-id", POOL,
            "--username", username, "--password", password, "--permanent")

        def token():
            return aws("cognito-idp", "initiate-auth", "--client-id", CLIENT,
                       "--auth-flow", "USER_PASSWORD_AUTH", "--auth-parameters",
                       json.dumps({"USERNAME": username, "PASSWORD": password,
                                   "SECRET_HASH": secret_hash}))["AuthenticationResult"]["AccessToken"]

        buyer = token()
        probe("/api/v1/organizer/events", {403}, buyer, forged=True)
        probe("/api/v1/tickets", {200}, buyer)
        probe("/api/v1/orders", {200}, buyer)
        probe("/api/v1/users/me", {404}, buyer)  # no profile created by this test
        probe(f"/api/v1/reservations/{MISSING}", {404}, buyer)
        probe(f"/api/v1/events/{MISSING}/queue/me", {404}, buyer)
        aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", POOL,
            "--username", username, "--group-name", "organiser")
        probe("/api/v1/organizer/events", {200}, token())
    finally:
        if created:
            aws("cognito-idp", "admin-delete-user", "--user-pool-id", POOL,
                "--username", username)
            print("Disposable Cognito user deleted.")


if __name__ == "__main__":
    main()
