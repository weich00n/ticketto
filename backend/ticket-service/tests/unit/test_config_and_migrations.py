from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.config import ConfigError, Settings, parse_groups, parse_signing_keys
from app.db import Base

ROOT = Path(__file__).resolve().parents[2]
SECRET = "s" * 32


def test_parse_signing_keys():
    assert parse_signing_keys(f"k1:{SECRET}, k0:{SECRET}x") == {"k1": SECRET, "k0": SECRET + "x"}


@pytest.mark.parametrize(
    "raw",
    [None, "", "k1", f"bad key:{SECRET}", "k1:short", f"k1:{SECRET},k1:{SECRET}"],
)
def test_invalid_signing_keys(raw):
    with pytest.raises(ConfigError):
        parse_signing_keys(raw)


def test_from_environment(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEYS", f"k2:{SECRET},k1:{SECRET}")
    monkeypatch.setenv("QR_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_SSL_CA", "/etc/ssl/rds/global-bundle.pem")
    monkeypatch.setenv("EVENT_SERVICE_URL", "http://event-service:3001/")
    monkeypatch.setenv("OUTBOX_RETRY_MAX_SECONDS", "120")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings.from_environment()

    assert settings.qr_active_key_id == "k1"
    assert settings.database_url.host == "db.internal"
    assert settings.database_url.database == "ticket_service"
    assert settings.database_connect_args()["ssl"] == {"ca": "/etc/ssl/rds/global-bundle.pem"}
    assert settings.event_service_url == "http://event-service:3001"
    assert settings.outbox_retry_max_seconds == 120
    assert (settings.outbox_poll_seconds, settings.outbox_retry_base_seconds) == (1, 5)


def test_outbox_backoff_must_be_positive_and_below_its_cap(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEYS", f"k1:{SECRET}")
    monkeypatch.setenv("OUTBOX_RETRY_BASE_SECONDS", "600")
    monkeypatch.setenv("OUTBOX_RETRY_MAX_SECONDS", "300")
    with pytest.raises(ConfigError):
        Settings.from_environment()
    monkeypatch.setenv("OUTBOX_RETRY_BASE_SECONDS", "0")
    with pytest.raises(ConfigError):
        Settings.from_environment()


def test_qr_keys_are_required_except_for_migrations(monkeypatch):
    monkeypatch.delenv("QR_SIGNING_KEYS", raising=False)

    with pytest.raises(ConfigError):
        Settings.from_environment()
    assert Settings.from_environment(require_qr_keys=False).qr_active_key_id == "unused"


def test_auth_mode(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEYS", f"k1:{SECRET}")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings.from_environment().auth_mode == "none"
    monkeypatch.setenv("AUTH_MODE", "Gateway")
    assert Settings.from_environment().auth_mode == "gateway"
    monkeypatch.setenv("AUTH_MODE", "cognito")
    with pytest.raises(ConfigError):
        Settings.from_environment()


def test_active_key_must_exist():
    with pytest.raises(ConfigError):
        Settings(
            database_url="sqlite://",
            rabbitmq_url="amqp://unused",
            qr_signing_keys={"k1": SECRET},
            qr_active_key_id="k9",
        )


def test_migrations_match_the_models(tmp_path, settings):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["settings"] = type(settings)(
        **{**settings.__dict__, "database_url": database_url}
    )
    config.attributes["skip_logging"] = True

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert diff == []


def test_check_in_groups(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEYS", f"k1:{SECRET}")
    monkeypatch.delenv("CHECK_IN_GROUPS", raising=False)
    assert Settings.from_environment().check_in_groups == {"organiser", "staff"}
    monkeypatch.setenv("CHECK_IN_GROUPS", " organiser , ")
    assert Settings.from_environment().check_in_groups == {"organiser"}
    with pytest.raises(ConfigError):
        parse_groups("organiser,bad group", frozenset())


def test_rabbitmq_defaults_to_tickets_own_broker_user(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEYS", f"k1:{SECRET}")
    monkeypatch.delenv("RABBITMQ_URL", raising=False)

    assert Settings.from_environment().rabbitmq_url.startswith("amqp://ticket-service:")


EMAIL_STATUS_QUERY = "SELECT order_id, status FROM email_deliveries"


def _alembic(tmp_path, settings) -> tuple[Config, str]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'handover.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["settings"] = type(settings)(
        **{**settings.__dict__, "database_url": database_url}
    )
    config.attributes["skip_logging"] = True
    return config, database_url


def test_migration_0003_hands_unsent_emails_over_as_ticket_issued(tmp_path, settings):
    config, database_url = _alembic(tmp_path, settings)
    command.upgrade(config, "0002")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for order_id, status in (("o-unsent", "PENDING"), ("o-sent", "SENT")):
            connection.execute(
                text(
                    "INSERT INTO ticket_orders (order_id, user_id, event_id, category_id,"
                    " quantity, issuance_status, source_message_id, received_at)"
                    " VALUES (:o, 'u', 'e', 'c', 1, 'ISSUED', :o, '2026-11-01 00:00:00')"
                ),
                {"o": order_id},
            )
            connection.execute(
                text(
                    "INSERT INTO email_deliveries (order_id, recipient, status, attempts)"
                    " VALUES (:o, 'buyer@example.com', :s, 0)"
                ),
                {"o": order_id, "s": status},
            )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        outbox = connection.execute(
            text("SELECT order_id, message_type, routing_key, status FROM outbox_messages")
        ).all()
        emails = dict(connection.execute(text(EMAIL_STATUS_QUERY)).all())
    # Only the email that hadn't gone out: notification-service sends it now, the old
    # worker never will, and the one already sent isn't sent again.
    assert outbox == [("o-unsent", "TicketIssued", "ticket.issued", "PENDING")]
    assert emails == {"o-unsent": "CANCELLED", "o-sent": "SENT"}

    command.downgrade(config, "0002")

    with engine.connect() as connection:
        emails = dict(connection.execute(text(EMAIL_STATUS_QUERY)).all())
    engine.dispose()
    assert emails == {"o-unsent": "PENDING", "o-sent": "SENT"}
