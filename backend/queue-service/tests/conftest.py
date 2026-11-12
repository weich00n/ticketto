"""Shared pytest fixtures.

Infrastructure (Redis, MySQL, RabbitMQ) is always real. Integration tests connect
to the services in docker-compose.test.yml, which you start yourself:

    make test-up            # docker compose -f docker-compose.test.yml up -d --wait

Unit tests never request these fixtures, so they run without any services.

Addresses default to that compose file's ports and can be overridden with
TEST_{MYSQL,REDIS,RABBITMQ}_{HOST,PORT} (CI points them at its job services).
If a service is unreachable its tests are skipped; set REQUIRE_TEST_SERVICES=1
(as CI does) to make that a failure instead.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import QueueConfig, create_engine, create_session_factory
from app.core.redis import create_redis

pytest_plugins = ["tests.fixtures.keys"]

REPO_ROOT = Path(__file__).resolve().parent.parent

# Credentials and default ports from docker-compose.test.yml.
MYSQL_USER = MYSQL_PASSWORD = "queue"
MYSQL_ROOT_PASSWORD = "root"
MYSQL_DATABASE = "queue_service"
RABBITMQ_USER = RABBITMQ_PASSWORD = "queue"
_DEFAULT_PORTS = {"MYSQL": 13306, "REDIS": 16379, "RABBITMQ": 15673}


def _address(service: str) -> tuple[str, int]:
    host = os.environ.get(f"TEST_{service}_HOST", "127.0.0.1")
    port = int(os.environ.get(f"TEST_{service}_PORT", _DEFAULT_PORTS[service]))
    return host, port


def _require_reachable(service: str) -> tuple[str, int]:
    """Return the service address, or skip/fail if nothing is listening there."""
    host, port = _address(service)
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        message = (
            f"{service.lower()} not reachable at {host}:{port}; "
            "start the test services with `make test-up`"
        )
        if os.environ.get("REQUIRE_TEST_SERVICES") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return host, port


@dataclass(frozen=True)
class MySQLInfo:
    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def async_url(self) -> str:
        return self.url_for(self.database)

    def url_for(self, database: str, *, root: bool = False) -> str:
        user, password = ("root", MYSQL_ROOT_PASSWORD) if root else (self.user, self.password)
        return f"mysql+asyncmy://{user}:{password}@{self.host}:{self.port}/{database}"


def alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


@pytest.fixture(scope="session")
def redis_url() -> str:
    host, port = _require_reachable("REDIS")
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def mysql_info() -> MySQLInfo:
    host, port = _require_reachable("MYSQL")
    return MySQLInfo(host, port, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE)


@pytest.fixture(scope="session")
def rabbitmq_url() -> str:
    host, port = _require_reachable("RABBITMQ")
    return f"amqp://{RABBITMQ_USER}:{RABBITMQ_PASSWORD}@{host}:{port}/%2F"


# --- MySQL -------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_mysql(mysql_info: MySQLInfo) -> MySQLInfo:
    """The test database with all migrations applied (once per session)."""
    command.upgrade(alembic_config(mysql_info.async_url), "head")
    return mysql_info


@pytest.fixture(scope="session")
async def db_engine(migrated_mysql: MySQLInfo) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_mysql.async_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on an emptied database."""
    factory = create_session_factory(db_engine)
    async with factory.begin() as session:
        await session.execute(delete(QueueConfig))
    return factory


# --- Redis -------------------------------------------------------------------


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    """Client on an emptied Redis database."""
    client = create_redis(redis_url)
    await client.flushdb()
    yield client
    await client.aclose()
