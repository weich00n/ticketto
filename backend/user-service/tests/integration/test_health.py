import pytest

pytestmark = pytest.mark.integration


def test_liveness(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_readiness_reaches_real_mysql(api):
    res = api.get("/health/ready")
    assert res.status_code == 200
    assert res.json()["checks"]["database"] == "up"
