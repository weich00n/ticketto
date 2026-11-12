# pylint: disable=no-member
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app import models  # noqa: F401  # pylint: disable=unused-import
from app.config import Settings
from app.db import Base, create_db_engine

config = context.config
# Tests pass their own Settings; otherwise read the environment. Migrations never sign
# tickets, so QR_SIGNING_KEYS is not needed here.
settings = config.attributes.get("settings") or Settings.from_environment(require_qr_keys=False)

if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_db_engine(settings, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
