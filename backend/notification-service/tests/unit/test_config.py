"""Settings from the environment."""

import pytest

from app.config import ConfigError, Settings

ENV = {
    "DATABASE_URL": "sqlite:///unused.db",
    "RABBITMQ_URL": "amqp://notification-service:x@broker:5672/%2F",
    "USER_SERVICE_URL": "http://user-service.ticketto.local:8000",
    "EMAIL_FROM": "Ticketto <n@ticketto.test>",
}


@pytest.fixture
def env(monkeypatch):
    for name in (
        "DATABASE_URL",
        "DB_HOST",
        "RABBITMQ_URL",
        "USER_SERVICE_URL",
        "EVENT_SERVICE_URL",
        "EMAIL_BACKEND",
        "SES_CONFIGURATION_SET",
        "DRAIN_BUDGET_FRACTION",
        "WORKER_RUN_SECONDS",
        "WORKER_POLL_SECONDS",
        "WORKER_RETRY_SECONDS",
        "WORKER_HEARTBEAT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_settings_from_environment(env):
    env.setenv("EVENT_SERVICE_URL", "http://event-service.ticketto.local:3001/")
    env.setenv("EMAIL_BACKEND", "SES")
    env.setenv("DRAIN_BUDGET_FRACTION", "0.25")
    settings = Settings.from_environment()
    assert settings.user_service_url == "http://user-service.ticketto.local:8000"
    assert settings.event_service_url == "http://event-service.ticketto.local:3001"
    assert settings.email_backend == "ses"
    assert settings.drain_budget_fraction == 0.25
    assert settings.email_max_attempts == 5 and settings.email_retry_base_seconds == 30


@pytest.mark.usefixtures("env")
def test_the_worker_defaults():
    settings = Settings.from_environment()
    assert settings.worker_run_seconds == 40
    assert settings.worker_poll_seconds == 10
    assert settings.worker_retry_seconds == 60
    assert settings.worker_heartbeat_file


def test_the_worker_settings_come_from_the_environment(env):
    env.setenv("WORKER_RUN_SECONDS", "10")
    env.setenv("WORKER_POLL_SECONDS", "3")
    env.setenv("WORKER_RETRY_SECONDS", "4")
    env.setenv("WORKER_HEARTBEAT_FILE", "/tmp/beat")
    settings = Settings.from_environment()
    assert (settings.worker_run_seconds, settings.worker_poll_seconds) == (10, 3)
    assert settings.worker_retry_seconds == 4
    assert settings.worker_heartbeat_file == "/tmp/beat"


@pytest.mark.parametrize(
    "name", ["WORKER_RUN_SECONDS", "WORKER_POLL_SECONDS", "WORKER_RETRY_SECONDS"]
)
def test_a_worker_interval_must_be_positive(env, name):
    env.setenv(name, "0")
    with pytest.raises(ConfigError, match=name):
        Settings.from_environment()


@pytest.mark.usefixtures("env")
def test_no_ses_configuration_set_by_default():
    assert Settings.from_environment().ses_configuration_set == ""


def test_the_ses_configuration_set_comes_from_the_environment(env):
    env.setenv("SES_CONFIGURATION_SET", "tracking")
    assert Settings.from_environment().ses_configuration_set == "tracking"


def test_database_url_built_from_parts(env):
    env.delenv("DATABASE_URL")
    env.setenv("DB_HOST", "db.example")
    env.setenv("DB_USER", "notification_service")
    env.setenv("DB_PASSWORD", "p@ss/word")
    settings = Settings.from_environment()
    url = settings.database_url
    assert (url.host, url.username, url.password, url.database) == (
        "db.example",
        "notification_service",
        "p@ss/word",
        "db_notification",
    )


@pytest.mark.parametrize("missing", ["RABBITMQ_URL", "USER_SERVICE_URL"])
def test_missing_required_setting(env, missing):
    env.delenv(missing)
    with pytest.raises(ConfigError, match=missing):
        Settings.from_environment()


def test_bad_email_backend(env):
    env.setenv("EMAIL_BACKEND", "carrier-pigeon")
    with pytest.raises(ConfigError, match="EMAIL_BACKEND"):
        Settings.from_environment()
