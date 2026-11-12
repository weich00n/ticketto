"""Migrations against a real MySQL: schema shape, model parity, and reversibility."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import MySQLInfo, alembic_config

pytestmark = pytest.mark.integration


async def _columns(engine: AsyncEngine, table: str) -> dict[str, tuple[str, str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, column_type, is_nullable, column_default"
                " FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        )
        return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def test_queue_configs_schema(db_engine: AsyncEngine) -> None:
    cols = await _columns(db_engine, "queue_configs")
    assert cols["event_id"][:2] == ("char(36)", "NO")
    assert cols["status"][0] == "enum('SCHEDULED','OPEN','ADMITTING','CLOSED','CANCELLED')"
    for name in ("queue_opens_at", "release_at", "created_at", "updated_at"):
        assert cols[name][:2] == ("datetime(3)", "NO")
    assert cols["closed_at"][:2] == ("datetime(3)", "YES")
    assert cols["sale_ends_at"][:2] == ("datetime(3)", "YES")  # migration 0002

    async with db_engine.connect() as conn:
        index = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.statistics"
                " WHERE table_schema = DATABASE() AND table_name = 'queue_configs'"
                " AND index_name = 'idx_status_release' ORDER BY seq_in_index"
            )
        )
        assert [r[0] for r in index] == ["status", "release_at"]
        index = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.statistics"
                " WHERE table_schema = DATABASE() AND table_name = 'queue_configs'"
                " AND index_name = 'idx_status_opens' ORDER BY seq_in_index"
            )
        )
        assert [r[0] for r in index] == ["status", "queue_opens_at"]  # migration 0005


async def test_only_queue_tables_exist(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        tables = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        )
        # processed_messages was dropped by migration 0004.
        assert {r[0] for r in tables} == {"alembic_version", "queue_configs"}


def test_models_match_migrations(migrated_mysql: MySQLInfo) -> None:
    """alembic check fails if the ORM models drift from the migrated schema."""
    command.check(alembic_config(migrated_mysql.async_url))


async def test_upgrade_downgrade_round_trip(mysql_info: MySQLInfo) -> None:
    """Run in a scratch database so the shared test schema is untouched."""
    admin = create_async_engine(mysql_info.url_for("mysql", root=True))
    async with admin.begin() as conn:
        await conn.execute(text("DROP DATABASE IF EXISTS migration_round_trip"))
        await conn.execute(text("CREATE DATABASE migration_round_trip"))
    await admin.dispose()

    url = mysql_info.url_for("migration_round_trip", root=True)
    cfg = alembic_config(url)
    # Alembic's env.py uses asyncio.run, so run it outside this test's event loop.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        tables = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        )
        assert {r[0] for r in tables} == {"alembic_version"}
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, cfg, "head")
