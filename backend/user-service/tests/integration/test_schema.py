"""The Alembic migrations created the tables exactly as cognito-settings.md §5 specifies,
plus the notification setting, purchased events and cancelled events (0002)."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


def _columns(db_engine) -> dict[str, dict]:
    with db_engine.connect() as conn:
        rows = conn.execute(text("SHOW COLUMNS FROM users")).mappings().all()
    return {r["Field"]: dict(r) for r in rows}


def test_migration_is_at_head(db_engine):
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"


def test_users_columns(db_engine):
    cols = _columns(db_engine)
    assert set(cols) == {
        "user_id",
        "cognito_sub",
        "email",
        "name",
        "role",
        "notify_new_events",
        "purchased_event_ids",
        "created_at",
        "updated_at",
    }
    assert cols["user_id"]["Type"] == "char(36)" and cols["user_id"]["Key"] == "PRI"
    assert cols["cognito_sub"]["Type"] == "char(36)" and cols["cognito_sub"]["Key"] == "UNI"
    assert cols["role"]["Type"] == "enum('buyer','organiser')"
    assert cols["role"]["Default"] == "buyer"
    assert cols["updated_at"]["Type"] == "timestamp"
    assert "on update CURRENT_TIMESTAMP" in cols["updated_at"]["Extra"]
    assert cols["notify_new_events"]["Type"] == "tinyint(1)"
    assert cols["notify_new_events"]["Default"] == "1"
    assert cols["purchased_event_ids"]["Type"] == "text"
    for name in cols:
        assert cols[name]["Null"] == ("YES" if name == "purchased_event_ids" else "NO"), name


def test_existing_users_are_opted_in(db_engine):
    # A row written without the new columns (as every row before 0002 was) gets the defaults.
    user_id = str(uuid.uuid4())
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, cognito_sub, email, name) "
                "VALUES (:id, UUID(), 'opt@test.com', 'Opt')"
            ),
            {"id": user_id},
        )
    try:
        with db_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT notify_new_events, purchased_event_ids FROM users WHERE user_id = :id"
                ),
                {"id": user_id},
            ).one()
        assert row == (1, None)
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE user_id = :id"), {"id": user_id})


def test_cancelled_events_table(db_engine):
    with db_engine.connect() as conn:
        rows = conn.execute(text("SHOW COLUMNS FROM cancelled_events")).mappings().all()
    cols = {r["Field"]: dict(r) for r in rows}
    assert set(cols) == {"event_id", "cancelled_at"}
    assert cols["event_id"]["Type"] == "char(36)" and cols["event_id"]["Key"] == "PRI"


def test_role_enum_rejects_other_values(db_engine):
    with pytest.raises(DBAPIError):
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (user_id, cognito_sub, email, name, role) "
                    "VALUES (UUID(), UUID(), 'x@test.com', 'X', 'admin')"
                )
            )
