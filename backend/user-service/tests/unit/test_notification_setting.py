"""The caller's new-event email setting: on by default (opt-out), changed with PATCH /me."""

from tests.unit.conftest import auth_headers, get_user_row


def test_new_profile_is_opted_in(client):
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 201
    assert res.json()["notify_new_events"] is True


def test_get_me_shows_the_setting(client, make_user):
    make_user()
    res = client.get("/api/v1/users/me", headers=auth_headers())
    assert res.json()["notify_new_events"] is True
    assert "purchased_event_ids" not in res.json()


def test_patch_turns_the_setting_off(client, cognito, make_user, session_factory):
    make_user()
    cognito.fail = True  # the setting is not stored in Cognito, so it must not be called
    res = client.patch(
        "/api/v1/users/me", headers=auth_headers(), json={"notify_new_events": False}
    )
    assert res.status_code == 200
    assert res.json()["notify_new_events"] is False
    assert get_user_row(session_factory).notify_new_events is False


def test_patch_turns_the_setting_back_on(client, make_user, session_factory):
    make_user(notify_new_events=False)
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"notify_new_events": True})
    assert res.status_code == 200
    assert get_user_row(session_factory).notify_new_events is True


def test_patch_name_only_keeps_the_setting(client, make_user, session_factory):
    make_user(notify_new_events=False)
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"name": "Alice Lim"})
    assert res.status_code == 200
    row = get_user_row(session_factory)
    assert row.name == "Alice Lim" and row.notify_new_events is False


def test_patch_both_fields(client, cognito, make_user, session_factory):
    make_user()
    res = client.patch(
        "/api/v1/users/me",
        headers=auth_headers(),
        json={"name": "Alice Lim", "notify_new_events": False},
    )
    assert res.status_code == 200
    row = get_user_row(session_factory)
    assert row.name == "Alice Lim" and row.notify_new_events is False
    assert cognito.users[row.cognito_sub]["name"] == "Alice Lim"


def test_patch_rejects_an_empty_body(client, make_user):
    make_user()
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_rejects_non_boolean_values(client, make_user, session_factory):
    make_user()
    for value in ["maybe", "yes", 1, None]:
        res = client.patch(
            "/api/v1/users/me", headers=auth_headers(), json={"notify_new_events": value}
        )
        assert res.status_code == 400, value
    assert get_user_row(session_factory).notify_new_events is True


def test_patch_rejects_null_name(client, make_user):
    make_user()
    res = client.patch(
        "/api/v1/users/me",
        headers=auth_headers(),
        json={"name": None, "notify_new_events": False},
    )
    assert res.status_code == 400


def test_patch_cannot_set_purchases(client, make_user, session_factory):
    make_user()
    res = client.patch(
        "/api/v1/users/me",
        headers=auth_headers(),
        json={"notify_new_events": False, "purchased_event_ids": "x"},
    )
    assert res.status_code == 400
    row = get_user_row(session_factory)
    assert row.purchased_event_ids is None and row.notify_new_events is True
