import os
from dataclasses import dataclass

from sqlalchemy import URL, make_url

EMAIL_BACKENDS = ("ses", "smtp", "log")
# Nothing else stops a stuck query on ECS. Reads and writes wait longer than MySQL's default
# 50 s lock wait, so a query waiting its turn isn't cut off.
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_IO_TIMEOUT_SECONDS = 60


class ConfigError(ValueError):
    """The environment is missing or has an invalid setting."""


def _database_url_from_parts() -> URL:
    return URL.create(
        "mysql+pymysql",
        username=os.environ.get("DB_USER", "notification_service"),
        password=os.environ.get("DB_PASSWORD", ""),
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        database=os.environ.get("DB_NAME", "db_notification"),
        query={"charset": "utf8mb4"},
    )


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _url(name: str) -> str | None:
    return (os.environ.get(name) or "").strip().rstrip("/") or None


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    database_url: str | URL
    rabbitmq_url: str
    user_service_url: str
    # Private address of event-service; only used for titles of events published before this
    # service started. Optional.
    event_service_url: str | None = None
    db_ssl_ca: str | None = None  # RDS CA bundle; turns on TLS
    log_level: str = "INFO"
    aws_region: str = "us-east-1"
    # Email: "ses" (AWS: the SES API with the worker's task role), "smtp" (Mailpit locally) or
    # "log" (only log what would be sent).
    email_backend: str = "ses"
    email_from: str = "Ticketto <notifications@ticketto.local>"
    # Makes SES publish each email's delivery events (deploy/terraform/email_tracking.tf).
    ses_configuration_set: str = ""
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    email_max_attempts: int = 5
    email_retry_base_seconds: int = 30
    # Share of each run spent taking messages off the queues; the rest sends emails.
    drain_budget_fraction: float = 0.5
    http_timeout_seconds: float = 2.0
    # The worker: one run's time limit, then how long to wait after a normal or a failed run.
    worker_run_seconds: int = 40
    worker_poll_seconds: int = 10
    worker_retry_seconds: int = 60
    # Touched after every run; the task definition's health check reads its age.
    worker_heartbeat_file: str = "/tmp/notification-worker-heartbeat"
    # False for migrations and the API, which only need the database.
    require_runtime: bool = True

    def __post_init__(self) -> None:
        if self.require_runtime:
            for name, value in (
                ("RABBITMQ_URL", self.rabbitmq_url),
                ("USER_SERVICE_URL", self.user_service_url),
            ):
                if not value:
                    raise ConfigError(f"{name} is required.")
        if self.email_backend not in EMAIL_BACKENDS:
            raise ConfigError(f"EMAIL_BACKEND must be one of {', '.join(EMAIL_BACKENDS)}.")
        if not 0 < self.drain_budget_fraction < 1:
            raise ConfigError("DRAIN_BUDGET_FRACTION must be between 0 and 1.")
        if self.email_max_attempts < 1:
            raise ConfigError("EMAIL_MAX_ATTEMPTS must be at least 1.")
        for name, value in (
            ("WORKER_RUN_SECONDS", self.worker_run_seconds),
            ("WORKER_POLL_SECONDS", self.worker_poll_seconds),
            ("WORKER_RETRY_SECONDS", self.worker_retry_seconds),
        ):
            if value < 1:
                raise ConfigError(f"{name} must be at least 1.")

    @classmethod
    def from_environment(cls, *, require_runtime: bool = True) -> "Settings":
        url = os.environ.get("DATABASE_URL")
        return cls(
            database_url=make_url(url) if url else _database_url_from_parts(),
            rabbitmq_url=os.environ.get("RABBITMQ_URL", "").strip(),
            user_service_url=_url("USER_SERVICE_URL") or "",
            event_service_url=_url("EVENT_SERVICE_URL"),
            db_ssl_ca=os.environ.get("DB_SSL_CA") or None,
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            email_backend=os.environ.get("EMAIL_BACKEND", "ses").strip().lower(),
            email_from=os.environ.get("EMAIL_FROM", "Ticketto <notifications@ticketto.local>"),
            ses_configuration_set=os.environ.get("SES_CONFIGURATION_SET", ""),
            smtp_host=os.environ.get("SMTP_HOST", "localhost"),
            smtp_port=int(os.environ.get("SMTP_PORT", "1025")),
            smtp_username=os.environ.get("SMTP_USERNAME") or None,
            smtp_password=os.environ.get("SMTP_PASSWORD") or None,
            smtp_starttls=_bool("SMTP_STARTTLS", False),
            email_max_attempts=int(os.environ.get("EMAIL_MAX_ATTEMPTS", "5")),
            email_retry_base_seconds=int(os.environ.get("EMAIL_RETRY_BASE_SECONDS", "30")),
            drain_budget_fraction=float(os.environ.get("DRAIN_BUDGET_FRACTION", "0.5")),
            http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "2")),
            worker_run_seconds=int(os.environ.get("WORKER_RUN_SECONDS", "40")),
            worker_poll_seconds=int(os.environ.get("WORKER_POLL_SECONDS", "10")),
            worker_retry_seconds=int(os.environ.get("WORKER_RETRY_SECONDS", "60")),
            worker_heartbeat_file=os.environ.get(
                "WORKER_HEARTBEAT_FILE", "/tmp/notification-worker-heartbeat"
            ),
            require_runtime=require_runtime,
        )

    def database_connect_args(self, *, timeouts: bool = True) -> dict:
        """`timeouts=False` is for migrations: cutting off a long ALTER TABLE could leave MySQL
        half-migrated."""
        if make_url(self.database_url).get_backend_name() != "mysql":
            return {}
        args: dict = {"init_command": "SET time_zone = '+00:00'"}
        if timeouts:
            args["connect_timeout"] = DB_CONNECT_TIMEOUT_SECONDS
            args["read_timeout"] = args["write_timeout"] = DB_IO_TIMEOUT_SECONDS
        if self.db_ssl_ca:
            args["ssl"] = {"ca": self.db_ssl_ca}
        return args
