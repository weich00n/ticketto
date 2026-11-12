from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import error_response

# Everything else (/api/v1, /docs, /openapi.json) is refused on the internal port.
_INTERNAL_PREFIX = "/internal/"
_HEALTH_PATHS = frozenset({"/health", "/health/ready"})


class InternalPortMiddleware:
    """On the internal port, answers 404 to anything but /internal/v1 and /health.

    A security group rule can only allow a port, not a path. Other services are let in on this
    port alone, so this is what keeps them away from the /api/v1 routes."""

    def __init__(self, app: ASGIApp, internal_port: int | None) -> None:
        self.app = app
        self.internal_port = internal_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._refused(scope):
            await error_response(404, "NOT_FOUND", "Not Found")(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _refused(self, scope: Scope) -> bool:
        if self.internal_port is None:
            return False
        server = scope.get("server")  # (host, port) of the socket the request arrived on
        if not server or server[1] != self.internal_port:
            return False
        path = scope["path"]
        return not (path.startswith(_INTERNAL_PREFIX) or path in _HEALTH_PATHS)
