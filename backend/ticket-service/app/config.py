import os
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import URL, make_url

_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
MIN_SECRET_LENGTH = 32
# The team broker's local credentials for Ticket's own user (rabbitmq-service has no guest).
DEFAULT_RABBITMQ_URL = "amqp://ticket-service:ticketto-local@localhost:5672/%2F"  # gitleaks:allow


class ConfigError(ValueError):
    """The environment is missing or has an invalid setting."""


def _database_url_from_parts() -> URL:
    return URL.create(
        "mysql+pymysql",
        username=os.environ.get("DB_USER", "ticketto"),
        password=os.environ.get("DB_PASSWORD", ""),
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        database=os.environ.get("DB_NAME", "ticket_service"),
        query={"charset": "utf8mb4"},
    )


def parse_signing_keys(raw: str | None) -> dict[str, str]:
    """`QR_SIGNING_KEYS` is `key_id:secret[,key_id:secret...]`."""
    if not raw or not raw.strip():
        raise ConfigError("QR_SIGNING_KEYS is required, e.g. 'k1:<random secret>'.")
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        key_id, sep, secret = entry.strip().partition(":")
        if not sep or not _KEY_ID.match(key_id):
            raise ConfigError("Each QR_SIGNING_KEYS entry must look like 'key_id:secret'.")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"QR signing secret '{key_id}' must be at least 32 characters.")
        if key_id in keys:
            raise ConfigError(f"QR signing key '{key_id}' is listed twice.")
        keys[key_id] = secret
    return keys


DEFAULT_CHECK_IN_GROUPS = frozenset({"organiser", "staff"})
_GROUP = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def parse_groups(raw: str | None, default: frozenset[str]) -> frozenset[str]:
    """`CHECK_IN_GROUPS` is a comma-separated list of Cognito groups, e.g. `organiser,staff`."""
    if raw is None or not raw.strip():
        return default
    groups = frozenset(g.strip() for g in raw.split(",") if g.strip())
    if not groups or not all(_GROUP.match(g) for g in groups):
        raise ConfigError("CHECK_IN_GROUPS must be group names separated by commas.")
    return groups


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    database_url: str | URL
    rabbitmq_url: str
    qr_signing_keys: dict[str, str]
    qr_active_key_id: str
    log_level: str = "INFO"
    db_ssl_ca: str | None = None  # RDS CA bundle; turns on TLS
    # Outbox relay (TicketIssued): how often the worker looks for due messages, and the
    # backoff between attempts when the broker refuses or is unreachable. It never gives up.
    outbox_poll_seconds: float = 1.0
    outbox_retry_base_seconds: float = 5.0
    outbox_retry_max_seconds: float = 300.0
    # Private address of Event Service, used to fill in event details missing from messages
    # before TicketIssued is published.
    event_service_url: str | None = None
    # "none": no authentication yet, buyers send X-User-Id. "gateway": trust API Gateway's
    # Cognito headers and require one of check_in_groups for check-in.
    auth_mode: str = "none"
    check_in_groups: frozenset[str] = DEFAULT_CHECK_IN_GROUPS

    def __post_init__(self) -> None:
        if self.qr_active_key_id not in self.qr_signing_keys:
            raise ConfigError("QR_ACTIVE_KEY_ID must name one of the QR_SIGNING_KEYS.")
        if not 0 < self.outbox_retry_base_seconds <= self.outbox_retry_max_seconds:
            raise ConfigError(
                "OUTBOX_RETRY_BASE_SECONDS must be above 0 and at most OUTBOX_RETRY_MAX_SECONDS."
            )
        if self.auth_mode not in ("none", "gateway"):
            raise ConfigError("AUTH_MODE must be 'none' or 'gateway'.")

    @classmethod
    def from_environment(cls, *, require_qr_keys: bool = True) -> "Settings":
        """`require_qr_keys=False` is for migrations, which never sign tickets."""
        url = os.environ.get("DATABASE_URL")
        raw_keys = os.environ.get("QR_SIGNING_KEYS")
        if not require_qr_keys and not raw_keys:
            raw_keys = f"unused:{secrets.token_urlsafe(32)}"
        keys = parse_signing_keys(raw_keys)
        return cls(
            database_url=make_url(url) if url else _database_url_from_parts(),
            rabbitmq_url=os.environ.get("RABBITMQ_URL", DEFAULT_RABBITMQ_URL),
            qr_signing_keys=keys,
            qr_active_key_id=os.environ.get("QR_ACTIVE_KEY_ID") or next(iter(keys)),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            db_ssl_ca=os.environ.get("DB_SSL_CA") or None,
            outbox_poll_seconds=float(os.environ.get("OUTBOX_POLL_SECONDS", "1")),
            outbox_retry_base_seconds=float(os.environ.get("OUTBOX_RETRY_BASE_SECONDS", "5")),
            outbox_retry_max_seconds=float(os.environ.get("OUTBOX_RETRY_MAX_SECONDS", "300")),
            event_service_url=(os.environ.get("EVENT_SERVICE_URL") or "").rstrip("/") or None,
            auth_mode=os.environ.get("AUTH_MODE", "none").strip().lower(),
            check_in_groups=parse_groups(
                os.environ.get("CHECK_IN_GROUPS"), DEFAULT_CHECK_IN_GROUPS
            ),
        )

    def database_connect_args(self) -> dict:
        if make_url(self.database_url).get_backend_name() != "mysql":
            return {}
        args: dict = {"init_command": "SET time_zone = '+00:00'"}
        if self.db_ssl_ca:
            args["ssl"] = {"ca": self.db_ssl_ca}
        return args
