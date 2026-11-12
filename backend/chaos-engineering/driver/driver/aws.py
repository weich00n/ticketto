"""Cognito test accounts and the Stripe test key, through boto3 with the caller's AWS credentials.

Accounts are made with the admin API (no sign-up email), the way the frontend's Playwright
helpers do, and sign in with USER_PASSWORD_AUTH like the frontend's login route.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from functools import cached_property

import boto3


@dataclass(frozen=True)
class Account:
    email: str
    password: str
    name: str

    @staticmethod
    def new(role: str) -> "Account":
        suffix = secrets.token_hex(4)
        return Account(
            email=f"chaos-{role}-{suffix}@example.com",
            # The pool's password rules: length, upper, lower, digit and symbol.
            password=f"Aa1!{secrets.token_hex(8)}",
            name=f"Chaos {role} {suffix}",
        )


class Aws:
    def __init__(self) -> None:
        self.region = os.environ.get("AWS_REGION", "us-east-1")
        self.pool_id = os.environ["COGNITO_USER_POOL_ID"]
        self.client_id = os.environ["COGNITO_CLIENT_ID"]
        self.stripe_secret_id = os.environ.get("STRIPE_SECRET_ID", "checkout-service/stripe")
        self.cognito = boto3.client("cognito-idp", region_name=self.region)

    @cached_property
    def _client_secret(self) -> str | None:
        client = self.cognito.describe_user_pool_client(UserPoolId=self.pool_id, ClientId=self.client_id)
        return client["UserPoolClient"].get("ClientSecret")

    @cached_property
    def stripe_key(self) -> str:
        """checkout-service's own key, so Stripe sends the webhook to the checkout we test."""
        sm = boto3.client("secretsmanager", region_name=self.region)
        key = json.loads(sm.get_secret_value(SecretId=self.stripe_secret_id)["SecretString"])["STRIPE_API_KEY"]
        if not key.startswith(("sk_test_", "rk_test_")):
            raise SystemExit("Refusing to run: the Stripe key is not a test-mode key.")
        return key

    def create(self, account: Account) -> None:
        self.cognito.admin_create_user(
            UserPoolId=self.pool_id,
            Username=account.email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": account.email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": account.name},
            ],
        )
        self.cognito.admin_set_user_password(
            UserPoolId=self.pool_id, Username=account.email, Password=account.password, Permanent=True
        )

    def delete(self, email: str) -> None:
        try:
            self.cognito.admin_delete_user(UserPoolId=self.pool_id, Username=email)
        except self.cognito.exceptions.UserNotFoundException:
            pass

    def sign_in(self, account: Account) -> str:
        """A fresh access token (valid 60 minutes), which carries the `groups` claim."""
        params = {"USERNAME": account.email, "PASSWORD": account.password}
        if self._client_secret:
            digest = hmac.new(self._client_secret.encode(), (account.email + self.client_id).encode(),
                              hashlib.sha256).digest()
            params["SECRET_HASH"] = base64.b64encode(digest).decode()
        result = self.cognito.initiate_auth(ClientId=self.client_id, AuthFlow="USER_PASSWORD_AUTH",
                                            AuthParameters=params)
        return result["AuthenticationResult"]["AccessToken"]
