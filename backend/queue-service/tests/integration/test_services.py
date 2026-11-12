"""Smoke tests: the test services (docker-compose.test.yml) accept connections."""

from __future__ import annotations

import pika
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import MySQLInfo

pytestmark = pytest.mark.integration


async def test_redis_reachable(redis_url: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        assert await client.ping()
        seconds, _micros = await client.time()
        assert seconds > 0
    finally:
        await client.aclose()


async def test_mysql_reachable_in_utc(mysql_info: MySQLInfo) -> None:
    engine = create_async_engine(mysql_info.async_url)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
            tz = (await conn.execute(text("SELECT @@global.time_zone"))).scalar_one()
            assert tz == "+00:00"
    finally:
        await engine.dispose()


def test_rabbitmq_reachable_with_quorum_queues(rabbitmq_url: str) -> None:
    connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    try:
        channel = connection.channel()
        channel.queue_declare("smoke-test", durable=True, arguments={"x-queue-type": "quorum"})
        channel.queue_delete("smoke-test")
    finally:
        connection.close()
