from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def create_db_engine(settings: Settings, **kwargs) -> Engine:
    backend = make_url(settings.database_url).get_backend_name()
    options: dict = {}
    if backend == "mysql":
        # Every read after taking the per-event lock must see what other transactions
        # committed before the lock was granted, not a snapshot taken earlier.
        options["isolation_level"] = "READ COMMITTED"
    connect_args = settings.database_connect_args()
    if backend == "sqlite":
        connect_args = {**connect_args, "timeout": 30, "check_same_thread": False}
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=3600,
        **options,
        **kwargs,
    )
    if backend == "sqlite":
        _use_real_sqlite_transactions(engine)
    return engine


def _use_real_sqlite_transactions(engine: Engine) -> None:
    """SQLite is only used by the unit tests. pysqlite's own transaction handling breaks
    SAVEPOINT; this is SQLAlchemy's documented fix. IMMEDIATE takes the write lock up front,
    so concurrent test transactions queue instead of failing with "database is locked"."""

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_session_factory(settings: Settings) -> "sessionmaker[Session]":
    return sessionmaker(bind=create_db_engine(settings), expire_on_commit=False)
