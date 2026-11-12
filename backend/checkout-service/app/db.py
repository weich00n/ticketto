from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def create_db_engine(settings: Settings, **kwargs) -> Engine:
    return create_engine(
        settings.database_url,
        connect_args=settings.database_connect_args(),
        pool_pre_ping=True,
        pool_recycle=3600,
        **kwargs,
    )


def create_session_factory(settings: Settings) -> "sessionmaker[Session]":
    return sessionmaker(bind=create_db_engine(settings), expire_on_commit=False)
