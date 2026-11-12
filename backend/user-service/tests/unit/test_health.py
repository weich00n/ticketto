from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import app


def test_liveness(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_readiness_db_up(client):
    res = client.get("/health/ready")
    assert res.status_code == 200
    assert res.json()["checks"]["database"] == "up"


def test_readiness_db_down(client):
    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = BrokenSession
    res = client.get("/health/ready")
    assert res.status_code == 503
    assert res.json()["checks"]["database"] == "down"


def test_liveness_does_not_touch_db(client):
    def fail():
        raise AssertionError("liveness must not use the database")

    app.dependency_overrides[get_db] = fail
    assert client.get("/health").status_code == 200
