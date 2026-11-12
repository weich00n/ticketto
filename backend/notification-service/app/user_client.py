"""Who to email, from user-service's internal API (no API Gateway route; inside the VPC).

  GET /internal/v1/users?notify_new_events=true     users who want new-event emails
  GET /internal/v1/users?purchased_event_id=<uuid>  users who bought tickets for the event
  GET /internal/v1/users/by-sub/{sub}               one user, by Cognito sub

Lists are paged: pass the previous page's next_after as `after` until it is null.
"""

import functools
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .http import HttpError, get_json

__all__ = ["HttpError", "Recipient", "UserClient", "UserNotFound", "UserServiceUnavailable"]

PAGE_SIZE = 500
MAX_PAGES = 1000  # 500 000 users; stops a server bug from looping forever


class UserServiceUnavailable(Exception):
    """user-service is down, slow or answered something unexpected. Transient: retried."""


class UserNotFound(Exception):
    """No user has this Cognito sub (yet). Retried: the profile may be created shortly."""


@dataclass(frozen=True)
class Recipient:
    user_id: str
    email: str
    name: str
    # Stored with each notification so the notifications API can find a signed-in user's rows
    # by the sub in their token. None from an older user-service (or by-sub, which omits it).
    cognito_sub: str | None = None


def _recipient(item: Any) -> Recipient:
    if not isinstance(item, dict):
        raise UserServiceUnavailable("user-service returned a malformed user")
    values = [item.get(k) for k in ("user_id", "email", "name")]
    if not all(isinstance(v, str) and v for v in values):
        raise UserServiceUnavailable("user-service returned a user without user_id/email/name")
    cognito_sub = item.get("cognito_sub")
    return Recipient(*values, cognito_sub=cognito_sub if isinstance(cognito_sub, str) else None)


class UserClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 2.0,
        fetch: Callable[[str], Any] | None = None,
        page_size: int = PAGE_SIZE,
    ):
        self._base = base_url.rstrip("/")
        self._fetch = fetch or functools.partial(get_json, timeout=timeout)
        self._page_size = page_size

    def _get(self, path: str) -> Any:
        try:
            return self._fetch(f"{self._base}{path}")
        except HttpError as error:
            if error.status == 404:
                raise
            raise UserServiceUnavailable(str(error)) from error
        except (OSError, ValueError) as error:
            raise UserServiceUnavailable(f"{type(error).__name__}: {error}") from error

    def _list(self, query: dict[str, str]) -> list[Recipient]:
        recipients: list[Recipient] = []
        after = None
        for _ in range(MAX_PAGES):
            params = {**query, "limit": str(self._page_size)}
            if after:
                params["after"] = after
            try:
                page = self._get(f"/internal/v1/users?{urllib.parse.urlencode(params)}")
            except HttpError as error:
                raise UserServiceUnavailable(str(error)) from error
            items = page.get("items") if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise UserServiceUnavailable("user-service returned a malformed page")
            recipients += [_recipient(item) for item in items]
            after = page.get("next_after")
            if not after:
                return recipients
        raise UserServiceUnavailable("user-service kept returning pages")

    def opted_in(self) -> list[Recipient]:
        return self._list({"notify_new_events": "true"})

    def buyers(self, event_id: str) -> list[Recipient]:
        return self._list({"purchased_event_id": event_id})

    def by_sub(self, cognito_sub: str) -> Recipient:
        try:
            body = self._get(f"/internal/v1/users/by-sub/{urllib.parse.quote(cognito_sub)}")
        except HttpError as error:  # only 404 gets here
            raise UserNotFound(cognito_sub) from error
        return _recipient(body)
