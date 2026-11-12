"""A small JSON GET over urllib, so the service needs no HTTP client library."""

import json
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import Any

from .correlation import get_correlation_id


class HttpError(Exception):
    """The server answered with a non-2xx status."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def get_json(url: str, timeout: float) -> Any:
    """Raises HttpError for a non-2xx answer, OSError when the server can't be reached or
    times out, and ValueError when the body isn't JSON."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "X-Correlation-Id": get_correlation_id()}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise HttpError(error.code, error.read().decode("utf-8", "replace")) from error
    except HTTPException as error:  # e.g. IncompleteRead: not an OSError of its own
        raise OSError(f"{type(error).__name__}: {error}") from error
