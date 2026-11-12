"""HTTP calls through the API Gateway (and to Stripe), retried the way a careful client would.

A step that fails with a network error, 429 or 5xx is sent again with the same Idempotency-Key
until its time budget runs out. Every failed attempt is recorded, so a run reports both what the
buyer saw in the end (the outcome) and how much went wrong along the way (the errors).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

RETRYABLE = {429, 500, 502, 503, 504}
STRIPE_API = "https://api.stripe.com/v1"


@dataclass
class Step:
    """One step of a journey: how long it took and every failed attempt."""

    started: float = field(default_factory=time.monotonic)
    ms: int = 0
    errors: list[str] = field(default_factory=list)

    def done(self) -> None:
        self.ms = int((time.monotonic() - self.started) * 1000)


class StepFailed(Exception):
    """A step ended without success: a final answer (4xx) or its retry budget ran out."""

    def __init__(self, tag: str, status: int | None = None, body: object = None):
        super().__init__(tag)
        self.tag = tag
        self.status = status
        self.body = body


def error_code(resp: requests.Response) -> str:
    """The services' {"error": {"code": ...}} code, or the HTTP status."""
    try:
        body = resp.json()
    except ValueError:
        return str(resp.status_code)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
        if isinstance(body.get("code"), str):
            return body["code"]
    return str(resp.status_code)


def call(session: requests.Session, method: str, url: str, step: Step, *, budget: float = 60.0,
         timeout: float = 10.0, ok: tuple[int, ...] = (200, 201), **kwargs) -> requests.Response:
    """Send until a status in `ok`, a final 4xx (raises StepFailed), or `budget` seconds pass."""
    deadline = time.monotonic() + budget
    delay = 0.5
    while True:
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            step.errors.append(type(exc).__name__)
        else:
            if resp.status_code in ok:
                return resp
            if resp.status_code not in RETRYABLE:
                raise StepFailed(error_code(resp), resp.status_code, _body(resp))
            step.errors.append(f"{resp.status_code} {error_code(resp)}")
        if time.monotonic() + delay > deadline:
            raise StepFailed("retries_exhausted")
        time.sleep(delay)
        delay = min(delay * 2, 5.0)


def _body(resp: requests.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:300]


class Gateway:
    """One signed-in user's view of the platform."""

    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def url(self, path: str) -> str:
        return self.base + path
