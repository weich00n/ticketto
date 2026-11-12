"""A stand-in for user-service's internal API, for the integration tests and local runs.

Answers the three calls notification-service makes, from users the test sets with
PUT /_stub/users (a JSON list of {user_id, cognito_sub, email, name, notify_new_events,
purchased_event_ids}). Paging works like user-service: ordered by user_id, `after` and
`limit`, `next_after` null on the last page. Standard library only: `python user_service.py`.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_USERS: list[dict] = []
_LOCK = threading.Lock()


def _summary(user: dict) -> dict:
    return {
        "user_id": user["user_id"],
        "cognito_sub": user["cognito_sub"],
        "email": user["email"],
        "name": user["name"],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's naming
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        with _LOCK:
            users = sorted(_USERS, key=lambda u: u["user_id"])
        if url.path == "/health":
            return self._send(200, {"status": "ok"})
        if url.path.startswith("/internal/v1/users/by-sub/"):
            sub = url.path.rsplit("/", 1)[1].lower()
            for user in users:
                if user["cognito_sub"] == sub:
                    return self._send(200, {**_summary(user), "role": "buyer"})
            return self._send(404, {"error": {"code": "USER_NOT_FOUND", "message": "nope"}})
        if url.path == "/internal/v1/users":
            if query.get("notify_new_events") == "true":
                matches = [u for u in users if u.get("notify_new_events", True)]
            elif "purchased_event_id" in query:
                event_id = query["purchased_event_id"].lower()
                matches = [u for u in users if event_id in u.get("purchased_event_ids", [])]
            else:
                return self._send(400, {"error": {"code": "VALIDATION_ERROR", "message": "?"}})
            after, limit = query.get("after"), int(query.get("limit", "500"))
            if after:
                matches = [u for u in matches if u["user_id"] > after]
            page = matches[:limit]
            next_after = page[-1]["user_id"] if len(page) == limit else None
            return self._send(200, {"items": [_summary(u) for u in page], "next_after": next_after})
        return self._send(404, {"error": {"code": "NOT_FOUND", "message": self.path}})

    def do_PUT(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        if self.path != "/_stub/users":
            return self._send(404, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with _LOCK:
            _USERS[:] = body
        return self._send(200, {"users": len(body)})

    def log_message(self, *_args) -> None:  # quiet
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # nosec B104
