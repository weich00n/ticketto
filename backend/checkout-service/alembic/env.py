# pylint: disable=no-member
"""Alembic environment: builds the metadata from the app's models and the DB URL from
the app's settings (so migrations and the running service always agree on the schema).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, engine_from_config, pool

from app.config import Settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = Settings.from_environment()


def _sqlalchemy_url(url: str | URL) -> str:
    # `str(URL)` is `render_as_string(hide_password=True)`, which redacts the
    # password to the literal "***". Using it as `sqlalchemy.url` would make
    # `alembic upgrade head` authenticate with "***" (MySQL 1045) instead of the
    # real password. Render with the password visible; a plain str already
    # carries it, so use it as-is.
    return url if isinstance(url, str) else url.render_as_string(hide_password=False)


# Allow `alembic upgrade head` to read DATABASE_URL or the parts from the environment.
config.set_main_option("sqlalchemy.url", _sqlalchemy_url(settings.database_url))


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=Settings.from_environment().database_connect_args(),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
