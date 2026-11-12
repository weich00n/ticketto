import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cognito import CognitoUnavailable, CognitoUserNotFound
from app.context import get_correlation_id

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def user_not_found() -> ApiError:
    return ApiError(404, "USER_NOT_FOUND", "User profile has not been initialised.")


def unauthenticated() -> ApiError:
    return ApiError(401, "UNAUTHENTICATED", "Missing or invalid authentication.")


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message},
            "correlation_id": get_correlation_id(),
        },
    )


_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


def _describe_validation_error(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "path", "query"))
        if err.get("type") == "extra_forbidden":
            parts.append(f"{loc}: field is not allowed")
        else:
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "Invalid request."


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    # FastAPI returns 422 for invalid input by default; the contract uses 400.
    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(400, "VALIDATION_ERROR", _describe_validation_error(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(CognitoUnavailable)
    async def _cognito_unavailable(_: Request, exc: CognitoUnavailable) -> JSONResponse:
        logger.error("Cognito call failed", extra={"cognito_error": str(exc)})
        return error_response(503, "COGNITO_UNAVAILABLE", "Please try again.")

    # The token was valid but the Cognito user no longer exists (e.g. deleted by an admin).
    @app.exception_handler(CognitoUserNotFound)
    async def _cognito_user_missing(_: Request, _exc: CognitoUserNotFound) -> JSONResponse:
        logger.warning("Cognito user not found for a valid token")
        return error_response(401, "UNAUTHENTICATED", "Missing or invalid authentication.")
