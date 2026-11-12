import logging

from flask import Flask, jsonify
from sqlalchemy.exc import InterfaceError, OperationalError
from werkzeug.exceptions import HTTPException

from . import correlation
from .config import Settings
from .db import create_session_factory
from .errors import ApiError
from .logging_config import configure_logging
from .routes import blueprints

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_environment()
    configure_logging(settings.log_level)
    app = Flask(__name__)
    app.json.sort_keys = False
    app.extensions["session_factory"] = create_session_factory(settings)
    app.extensions["settings"] = settings
    correlation.init_app(app)
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify(error.body()), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        status = error.code or 500
        code = "VALIDATION_ERROR" if status == 400 else error.name.upper().replace(" ", "_")
        return jsonify(ApiError(status, code, error.name + ".").body()), status

    @app.errorhandler(OperationalError)
    @app.errorhandler(InterfaceError)
    def handle_database_unavailable(_error: Exception):
        logger.exception("Database unavailable")
        body = ApiError(503, "SERVICE_UNAVAILABLE", "Please retry shortly.").body()
        return jsonify(body), 503

    @app.errorhandler(Exception)
    def handle_unexpected_error(_error: Exception):
        logger.exception("Unhandled error")
        body = ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.").body()
        return jsonify(body), 500

    return app
