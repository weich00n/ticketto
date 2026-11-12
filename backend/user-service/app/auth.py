import json
import re
from dataclasses import dataclass

from fastapi import Header

from app.errors import unauthenticated


@dataclass(frozen=True)
class CurrentUser:
    """The caller, as identified by API Gateway after it verified the access token."""

    sub: str
    username: str
    groups: frozenset[str]


def parse_groups(raw: str | None) -> frozenset[str]:
    """Parse the X-User-Groups header into a set of group names.

    API Gateway passes `cognito:groups` as a string rather than a JSON array, and
    the exact format can vary, so accept all of: `[organiser]`, `organiser`,
    `organiser,admin`, `organiser admin`, `["organiser","admin"]`.
    Always compare the result by exact membership, never by substring.
    """
    if not raw or not raw.strip():
        return frozenset()
    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return frozenset(str(g).strip() for g in parsed if str(g).strip())
        text = text[1:-1] if text.endswith("]") else text[1:]
    names = (part.strip().strip("\"'") for part in re.split(r"[,\s]+", text))
    return frozenset(name for name in names if name)


def get_current_user(
    x_user_sub: str | None = Header(default=None),
    x_user_username: str | None = Header(default=None),
    x_user_groups: str | None = Header(default=None),
) -> CurrentUser:
    # Behind API Gateway these are always set; missing means the request bypassed it.
    if not x_user_sub or not x_user_username:
        raise unauthenticated()
    return CurrentUser(
        sub=x_user_sub.strip(),
        username=x_user_username.strip(),
        groups=parse_groups(x_user_groups),
    )
