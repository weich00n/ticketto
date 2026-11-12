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
        database=os.environ.get("DB_NAME", "checkout_service"),
        query={"charset": "utf8mb4"},
    )


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    database_url: str | URL
    rabbitmq_url: str
    log_level: str = "INFO"
    db_ssl_ca: str | None = None  # RDS CA bundle; turns on TLS

    # Which of the broker's pre-created checkout queues we consume from.
    broker_consumer_queues: tuple[str, ...] = (
        "checkout-service.EventCancelled",
        "checkout-service.ReservationExpired",
        "checkout-service.RefundRequired",
    )

    # Reservation-service (service-to-service). No auth header: it is reachable only
    # inside the VPC, matching the other internal callers.
    reservation_base_url: str = "http://reservation-service:8081"

    # Stripe. The live keys come from the secrets manager in AWS and from .env locally.
    # Never baked into the image or committed.
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""

    # How long a PENDING_PAYMENT order may live before the worker expires it. The
    # deadline itself comes from the hold (payment_expires_at); this is the sweep cadence.
    expiry_sweep_seconds: int = 10

    @classmethod
    def from_environment(cls) -> "Settings":
        url = os.environ.get("DATABASE_URL")
        return cls.from_environment_or(
            database_url=make_url(url) if url else _database_url_from_parts(),
        )

    @classmethod
    def from_environment_or(cls, *, database_url: str | URL) -> "Settings":
        return cls(
            database_url=database_url,
            # The shared broker has no guest user; this is its local admin (rabbitmq-service).
            rabbitmq_url=os.environ.get(
                "RABBITMQ_URL",
                "amqp://ticketto:ticketto-local@localhost:5672/%2F",  # gitleaks:allow
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            db_ssl_ca=os.environ.get("DB_SSL_CA") or None,
            reservation_base_url=os.environ.get(
                "RESERVATION_BASE_URL", "http://reservation-service:8081"
            ),
            stripe_api_key=os.environ.get("STRIPE_API_KEY", ""),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
            expiry_sweep_seconds=int(os.environ.get("EXPIRY_SWEEP_SECONDS", "10")),
        )

    def database_connect_args(self) -> dict:
        if make_url(self.database_url).get_backend_name() != "mysql":
            return {}
        args: dict = {"init_command": "SET time_zone = '+00:00'"}
        if self.db_ssl_ca:
            # Production RDS: TLS to the RDS endpoint, verified against its CA bundle.
            args["ssl"] = {"ca": self.db_ssl_ca}
        else:
            # Local dev / non-RDS MySQL (no client-side CA to verify): skip the
            # PREFERRED-TLS upgrade and do a plain connection. PyMySQL then uses the
            # RSA full-auth exchange (needs `cryptography`), which is deterministic;
            # the PREFERRED path can otherwise fall into an SSL upgrade that MySQL 8's
            # caching_sha2 rejects with 1045.
            args["ssl_disabled"] = True
        return args
