"""Fixtures for the integration suite.

These tests talk to the running Docker Compose stack:
  - the service over HTTP          (IT_SERVICE_URL,     default http://localhost:8000)
  - cognito-local through boto3    (COGNITO_ENDPOINT,   default http://localhost:9229)
  - MySQL directly, to check rows  (DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME)

Values are read from the project's .env (whose DB_HOST/DB_PORT are the host-side values,
127.0.0.1:3307). Any of them can be overridden with an IT_-prefixed env var, e.g. IT_DB_PORT.

The pool ID and DB credentials are read from the project's .env file, not from os.environ,
because the unit-test conftest sets a fake COGNITO_USER_POOL_ID in the environment.
"""

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
import httpx
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
PASSWORD = "Passw0rd!"


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.split("#", 1)[0].strip()
    return values


def _setting(name: str, default: str | None = None) -> str:
    # IT_<NAME> in the environment wins, then .env, then the default.
    value = os.environ.get(f"IT_{name}") or _read_env_file().get(name) or default
    if value is None:
        pytest.fail(f"Missing {name}: set it in .env or as IT_{name}")
    return value


def decode_jwt_claims(token: str) -> dict:
    """Read a JWT's payload without verifying it (the gateway's job, not the test's)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@dataclass
class StackUser:
    email: str
    name: str
    sub: str  # also the Cognito username, because users sign in with email

    def headers(self, groups: str | None = None) -> dict[str, str]:
        """The headers API Gateway would set after verifying this user's token."""
        h = {"X-User-Sub": self.sub, "X-User-Username": self.sub}
        if groups is not None:
            h["X-User-Groups"] = groups
        return h


# ---------- stack connections (one per test session) ----------


@pytest.fixture(scope="session")
def api():
    base_url = _setting("SERVICE_URL", "http://localhost:8000")
    with httpx.Client(base_url=base_url, timeout=10) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(
                f"user-service is not reachable at {base_url} ({exc}). "
                "Start the stack first: docker compose up -d --build"
            )
        yield client


@pytest.fixture(scope="session")
def pool_id() -> str:
    return _setting("COGNITO_USER_POOL_ID")


@pytest.fixture(scope="session")
def cognito(pool_id):
    client = boto3.client(
        "cognito-idp",
        region_name="us-east-1",
        endpoint_url=_setting("COGNITO_ENDPOINT", "http://localhost:9229"),
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    try:
        client.describe_user_pool(UserPoolId=pool_id)
    except (ClientError, BotoCoreError) as exc:
        pytest.fail(
            f"Cognito pool {pool_id} not found in cognito-local ({exc}). "
            "Run ./scripts/seed-cognito-local.sh and update COGNITO_USER_POOL_ID in .env"
        )
    try:
        client.create_group(UserPoolId=pool_id, GroupName="organiser")
    except client.exceptions.GroupExistsException:
        pass
    return client


@pytest.fixture(scope="session")
def app_client_id(cognito, pool_id) -> str:
    """An app client allowing USER_PASSWORD_AUTH, so tests can sign in for real tokens."""
    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
    for c in clients:
        if c["ClientName"] == "integration-tests":
            return c["ClientId"]
    return cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="integration-tests",
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]


@pytest.fixture(scope="session")
def db_engine():
    url = URL.create(
        "mysql+pymysql",
        username=_setting("DB_USER", "user_service"),
        password=_setting("DB_PASSWORD", "user_service_dev"),
        host=_setting("DB_HOST", "127.0.0.1"),
        port=int(_setting("DB_PORT", "3307")),
        database=_setting("DB_NAME", "db_user"),
    )
    engine = create_engine(url, connect_args={"init_command": "SET time_zone = '+00:00'"})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.fail(
            f"Cannot connect to MySQL at {url.host}:{url.port} ({exc}). Is the mysql container up?"
        )
    yield engine
    engine.dispose()


# ---------- per-test helpers ----------


@pytest.fixture
def db_row(db_engine):
    """db_row(sub) -> the user's row in MySQL as a dict, or None."""

    def _get(sub: str) -> dict | None:
        with db_engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM users WHERE cognito_sub = :sub"), {"sub": sub})
                .mappings()
                .first()
            )
            return dict(row) if row else None

    return _get


@pytest.fixture
def make_cognito_user(cognito, pool_id, db_engine):
    """Create fresh Cognito users with unique emails; delete them (and their DB rows) afterwards."""
    created: list[StackUser] = []

    def _make(name: str = "Integration Tester") -> StackUser:
        email = f"it-{uuid.uuid4().hex[:12]}@test.com"
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": name},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=PASSWORD, Permanent=True
        )
        sub = cognito.admin_get_user(UserPoolId=pool_id, Username=email)["Username"]
        user = StackUser(email=email, name=name, sub=sub)
        created.append(user)
        return user

    yield _make

    for user in created:
        try:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=user.email)
        except cognito.exceptions.UserNotFoundException:
            pass
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE cognito_sub = :sub"), {"sub": user.sub})


@pytest.fixture
def user(make_cognito_user) -> StackUser:
    return make_cognito_user()


@pytest.fixture
def sign_in(cognito, app_client_id):
    """sign_in(user) -> the access token's claims, from a real cognito-local sign-in."""

    def _sign_in(u: StackUser) -> dict:
        result = cognito.initiate_auth(
            ClientId=app_client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": u.email, "PASSWORD": PASSWORD},
        )
        return decode_jwt_claims(result["AuthenticationResult"]["AccessToken"])

    return _sign_in


@pytest.fixture
def cognito_name(cognito, pool_id):
    def _get(u: StackUser) -> str | None:
        attrs = cognito.admin_get_user(UserPoolId=pool_id, Username=u.email)["UserAttributes"]
        return next((a["Value"] for a in attrs if a["Name"] == "name"), None)

    return _get
