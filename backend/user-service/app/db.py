from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url(),
        connect_args=settings.database_connect_args(),
        pool_pre_ping=True,  # drop connections RDS has closed
        pool_recycle=3600,
    )


@lru_cache
def get_sessionmaker() -> "sessionmaker[Session]":
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session
