from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

pytestmark = pytest.mark.unit

REQUIRED = {
    "MYSQL_URL": "mysql+asyncmy://u:p@localhost:3306/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "RABBITMQ_URL": "amqp://u:p@localhost:5672/%2F",
    "JWT_PRIVATE_KEY_PATH": "secrets/jwt_private.pem",
    "JWT_KID": "queue-local-1",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for name in [
        *REQUIRED,
        "JWT_PUBLIC_KEYS_DIR",
        "SELECTION_WINDOW_SECONDS",
        "RABBITMQ_DECLARE_TOPOLOGY",
    ]:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_reads_env_with_defaults(env: pytest.MonkeyPatch) -> None:
    s = Settings()
    assert s.mysql_url == REQUIRED["MYSQL_URL"]
    assert s.jwt_private_key_path == Path("secrets/jwt_private.pem")
    assert s.jwt_public_keys_dir is None
    assert s.admitter_tick_seconds == 1.0
    assert s.log_level == "INFO"
    assert s.rabbitmq_declare_topology is True  # local default: declare everything


def test_shared_broker_mode(env: pytest.MonkeyPatch) -> None:
    env.setenv("RABBITMQ_DECLARE_TOPOLOGY", "false")
    assert Settings().rabbitmq_declare_topology is False


def test_admission_deadlines_default_to_domain_values(env: pytest.MonkeyPatch) -> None:
    s = Settings()
    assert s.selection_window_seconds == 5 * 60
    assert s.admission_window_seconds == 11 * 60


def test_empty_public_keys_dir_means_none(env: pytest.MonkeyPatch) -> None:
    env.setenv("JWT_PUBLIC_KEYS_DIR", "")
    assert Settings().jwt_public_keys_dir is None
    env.setenv("JWT_PUBLIC_KEYS_DIR", "secrets/old")
    assert Settings().jwt_public_keys_dir == Path("secrets/old")


def test_missing_required_setting_fails(env: pytest.MonkeyPatch) -> None:
    env.delenv("REDIS_URL")
    with pytest.raises(ValidationError, match="redis_url"):
        Settings()


def test_selection_window_must_end_before_admission(env: pytest.MonkeyPatch) -> None:
    env.setenv("SELECTION_WINDOW_SECONDS", "660")
    with pytest.raises(ValidationError, match="selection_window_seconds"):
        Settings()
