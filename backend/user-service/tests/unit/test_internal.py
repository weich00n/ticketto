def test_internal_get_user(client, make_user):
    user = make_user()
    res = client.get(f"/internal/v1/users/{user.user_id}")
    assert res.status_code == 200
    assert res.json()["email"] == "alice@example.com"
    assert res.json()["user_id"] == user.user_id


def test_internal_get_user_uppercase_id(client, make_user):
    user = make_user()
    assert client.get(f"/internal/v1/users/{user.user_id.upper()}").status_code == 200


def test_internal_get_user_not_found(client):
    res = client.get("/internal/v1/users/00000000-0000-4000-8000-000000000000")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "USER_NOT_FOUND"


def test_internal_get_user_invalid_id(client):
    res = client.get("/internal/v1/users/not-a-uuid")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
