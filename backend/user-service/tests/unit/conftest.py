import os

os.environ.setdefault("COGNITO_USER_POOL_ID", "test-pool")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cognito import CognitoUnavailable, CognitoUserNotFound, get_cognito
from app.db import Base, get_db
from app.main import app
from app.models import User

BUYER_SUB = "11111111-1111-4111-8111-111111111111"
BUYER_USERNAME = BUYER_SUB  # with email sign-in, Cognito's username is the sub
OTHER_SUB = "22222222-2222-4222-8222-222222222222"


def auth_headers(sub: str = BUYER_SUB, username: str | None = None, groups: str | None = None):
    headers = {"X-User-Sub": sub, "X-User-Username": username or sub}
    if groups is not None:
        headers["X-User-Groups"] = groups
    return headers


class FakeCognito:
    """In-memory stand-in for app.cognito.CognitoClient."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {
            BUYER_USERNAME: {"sub": BUYER_SUB, "email": "alice@example.com", "name": "Alice Tan"},
        }
        self.groups: dict[str, set[str]] = {}
        self.fail = False
        self.before_get_user = None  # hook for race-condition tests

    def _check(self, username: str) -> None:
        if self.fail:
            raise CognitoUnavailable("simulated outage")
        if username not in self.users:
            raise CognitoUserNotFound()

    def get_user_attributes(self, username: str) -> dict[str, str]:
        self._check(username)
        if self.before_get_user:
            self.before_get_user()
        return dict(self.users[username])

    def add_to_organiser_group(self, username: str) -> None:
        self._check(username)
        self.groups.setdefault(username, set()).add("organiser")

    def update_name(self, username: str, name: str) -> None:
        self._check(username)
        self.users[username]["name"] = name


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def cognito():
    return FakeCognito()


@pytest.fixture
def client(session_factory, cognito):
    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_cognito] = lambda: cognito
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(session_factory):
    def _make(sub: str = BUYER_SUB, role: str = "buyer", **fields) -> User:
        user = User(
            user_id=fields.pop("user_id", "8f14e45f-ceea-4b6a-9a3b-1f2d3c4e5f60"),
            cognito_sub=sub,
            email=fields.pop("email", "alice@example.com"),
            name=fields.pop("name", "Alice Tan"),
            role=role,
            **fields,  # e.g. notify_new_events, purchased_event_ids
        )
        with session_factory() as session:
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

    return _make


def get_user_row(session_factory, sub: str = BUYER_SUB) -> User | None:
    with session_factory() as session:  # type: Session
        return session.query(User).filter_by(cognito_sub=sub).one_or_none()
