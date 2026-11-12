"""Migrations, and the database timeouts they must not get. Test 8 of rollout-plan.md, Part 3."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import Settings
from app.migrations import alembic_config

REPO = Path(__file__).resolve().parents[2]
MYSQL = "mysql+pymysql://u:p@db.example/db_notification"


def _settings(url: str, **kwargs) -> Settings:
    return Settings(
        database_url=url, rabbitmq_url="", user_service_url="", require_runtime=False, **kwargs
    )


def test_the_alembic_command_needs_only_the_database(tmp_path):
    # What the one-off ECS task runs: no broker or user-service settings are set.
    url = f"sqlite:///{tmp_path / 'm.db'}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RABBITMQ", "USER_SERVICE"))}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**env, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"notifications", "events", "processed_messages", "alembic_version"} <= tables


def test_migrations_downgrade_and_upgrade_again(tmp_path):
    url = f"sqlite:///{tmp_path / 'd.db'}"
    config: Config = alembic_config(_settings(url))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert set(inspect(create_engine(url)).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    assert "notifications" in inspect(create_engine(url)).get_table_names()


def test_migration_0002_adds_the_inbox_columns_and_removes_them_again(tmp_path):
    url = f"sqlite:///{tmp_path / 'inbox.db'}"
    config: Config = alembic_config(_settings(url))

    def columns() -> set[str]:
        return {c["name"] for c in inspect(create_engine(url)).get_columns("notifications")}

    def indexes() -> set[str]:
        return {i["name"] for i in inspect(create_engine(url)).get_indexes("notifications")}

    command.upgrade(config, "head")
    assert {"cognito_sub", "read_at"} <= columns()
    assert "ix_notifications_inbox" in indexes()
    command.downgrade(config, "0001")
    assert not {"cognito_sub", "read_at"} & columns()
    assert "ix_notifications_inbox" not in indexes()


# ---------- database timeouts ----------


def test_mysql_connections_get_timeouts():
    # Without them a stalled database would hang the worker forever: nothing else stops a run.
    args = _settings(MYSQL).database_connect_args()
    assert args["connect_timeout"] == 10
    assert (args["read_timeout"], args["write_timeout"]) == (60, 60)


def test_the_read_timeout_is_longer_than_mysqls_lock_wait():
    # innodb_lock_wait_timeout defaults to 50 s: a query waiting its turn isn't stuck.
    assert _settings(MYSQL).database_connect_args()["read_timeout"] > 50


def test_mysql_connections_can_leave_the_timeouts_out():
    args = _settings(MYSQL).database_connect_args(timeouts=False)
    assert not {"connect_timeout", "read_timeout", "write_timeout"} & set(args)
    assert args["init_command"]  # everything else stays


def test_migrations_run_without_the_timeouts(tmp_path, monkeypatch):
    # A long ALTER TABLE cut off at 60 s could leave MySQL half-migrated.
    seen = []
    real = Settings.database_connect_args

    def spy(self, *, timeouts: bool = True) -> dict:
        seen.append(timeouts)
        return real(self, timeouts=timeouts)

    monkeypatch.setattr(Settings, "database_connect_args", spy)
    command.upgrade(alembic_config(_settings(f"sqlite:///{tmp_path / 't.db'}")), "head")
    assert seen == [False]


@pytest.mark.parametrize("ca", [None, "/etc/ssl/rds/global-bundle.pem"])
def test_tls_is_unchanged_by_the_timeouts(ca):
    args = _settings(MYSQL, db_ssl_ca=ca).database_connect_args()
    assert args.get("ssl") == ({"ca": ca} if ca else None)
