from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# API Gateway reaches this port through the shared ALB.
PUBLIC_PORT = 8000


class Settings(BaseSettings):
    """Environment-driven configuration. Values come from env vars (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str = "us-east-1"
    # Set to the cognito-local URL for local dev; leave unset in AWS.
    cognito_endpoint: str | None = None
    cognito_user_pool_id: str
    cognito_organiser_group: str = "organiser"
    cognito_timeout_seconds: float = 5.0

    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "db_user"
    db_user: str = "user_service"
    db_password: str = ""
    # Path to the RDS CA bundle. When set, connections to MySQL use TLS.
    db_ssl_ca: str | None = None

    log_level: str = "INFO"

    # A second port that serves only /internal/v1 and /health (app/internal_port.py). Other
    # services are let in on this port alone, so they can't reach the /api/v1 routes, which
    # trust the X-User-* headers. Unset (local dev, tests): PUBLIC_PORT serves everything.
    internal_port: int | None = None

    # The worker (python -m app.worker) only. As user-service, e.g.
    # amqp://user-service:<password>@<broker>:5672/%2F
    rabbitmq_url: str | None = None

    @field_validator("internal_port")
    @classmethod
    def _not_the_public_port(cls, port: int | None) -> int | None:
        if port == PUBLIC_PORT:
            raise ValueError(f"INTERNAL_PORT must differ from the public port {PUBLIC_PORT}")
        return port

    def database_url(self) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )

    def database_connect_args(self) -> dict:
        # Store and read TIMESTAMP columns in UTC regardless of the server's time zone.
        args: dict = {"init_command": "SET time_zone = '+00:00'"}
        if self.db_ssl_ca:
            args["ssl"] = {"ca": self.db_ssl_ca}
        return args


@lru_cache
def get_settings() -> Settings:
    return Settings()
