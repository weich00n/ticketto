import os
from dataclasses import dataclass

from sqlalchemy import URL, make_url


def _database_url_from_parts() -> URL:
    return URL.create(
        "mysql+pymysql",
        username=os.environ.get("DB_USER", "ticketto"),
        password=os.environ.get("DB_PASSWORD", ""),
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        database=os.environ.get("DB_NAME", "event_service"),
        query={"charset": "utf8mb4"},
    )


@dataclass(frozen=True)
class Settings:
    database_url: str | URL
    rabbitmq_url: str
    log_level: str = "INFO"
    db_ssl_ca: str | None = None  # RDS CA bundle; turns on TLS

    @classmethod
    def from_environment(cls) -> "Settings":
        url = os.environ.get("DATABASE_URL")
        return cls(
            database_url=make_url(url) if url else _database_url_from_parts(),
            rabbitmq_url=os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            db_ssl_ca=os.environ.get("DB_SSL_CA") or None,
        )

    def database_connect_args(self) -> dict:
        if make_url(self.database_url).get_backend_name() != "mysql":
            return {}
        args: dict = {"init_command": "SET time_zone = '+00:00'"}
        if self.db_ssl_ca:
            args["ssl"] = {"ca": self.db_ssl_ca}
        return args
