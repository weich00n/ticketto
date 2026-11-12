from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from app.db import Base
from app.main import create_app


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def app(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'event-service.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    app = create_app(Settings(database_url=database_url, rabbitmq_url="amqp://unused"))
    app.config.update(TESTING=True)
    yield app
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def session_factory(app):
    return app.extensions["session_factory"]


@pytest.fixture()
def organiser_headers():
    return {
        "X-User-Sub": "11111111-1111-1111-1111-111111111111",
        "X-User-Username": "organiser@example.com",
        "X-User-Groups": "[organiser]",
    }


@pytest.fixture()
def release_at():
    return datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)


@pytest.fixture()
def event_payload(release_at):
    return {
        "title": "The Midnight Symphony",
        "description": "An evening of orchestral music.",
        "venue": "Victoria Theatre & Concert Hall",
        "starts_at": iso(release_at + timedelta(days=30)),
        "ticket_release_at": iso(release_at),
        "ticket_categories": [
            {"name": "CAT 1", "price_amount": 8800, "currency": "SGD", "total_quantity": 500}
        ],
    }
