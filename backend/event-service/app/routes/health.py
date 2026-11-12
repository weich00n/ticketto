from flask import Blueprint, jsonify
from sqlalchemy import select

from ._common import open_session

blueprint = Blueprint("health", __name__)


@blueprint.get("/health")
def liveness():
    return jsonify({"status": "ok", "service": "event-service"})


@blueprint.get("/ready")
def readiness():
    try:
        with open_session() as session:
            session.execute(select(1))
        return jsonify({"status": "ready"})
    except Exception:  # pylint: disable=broad-exception-caught
        return jsonify({"status": "not_ready"}), 503
