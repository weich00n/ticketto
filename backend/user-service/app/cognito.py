from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings


class CognitoUnavailable(Exception):
    """A Cognito call failed or timed out. Safe for the client to retry."""


class CognitoUserNotFound(Exception):
    """Cognito has no user with this username."""


class CognitoClient:
    """Thin wrapper around the Cognito admin APIs the User Service uses."""

    def __init__(self, user_pool_id: str, organiser_group: str, client) -> None:
        self._pool_id = user_pool_id
        self._organiser_group = organiser_group
        self._client = client

    def _call(self, operation: str, **params) -> dict:
        try:
            return getattr(self._client, operation)(UserPoolId=self._pool_id, **params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "UserNotFoundException":
                raise CognitoUserNotFound() from exc
            raise CognitoUnavailable(f"{operation}: {code}") from exc
        except BotoCoreError as exc:
            raise CognitoUnavailable(f"{operation}: {type(exc).__name__}") from exc

    def get_user_attributes(self, username: str) -> dict[str, str]:
        result = self._call("admin_get_user", Username=username)
        return {a["Name"]: a["Value"] for a in result.get("UserAttributes", [])}

    def add_to_organiser_group(self, username: str) -> None:
        # Adding a user who is already in the group succeeds, so this is safe to repeat.
        self._call("admin_add_user_to_group", Username=username, GroupName=self._organiser_group)

    def update_name(self, username: str, name: str) -> None:
        self._call(
            "admin_update_user_attributes",
            Username=username,
            UserAttributes=[{"Name": "name", "Value": name}],
        )


@lru_cache
def get_cognito() -> CognitoClient:
    settings = get_settings()
    client = boto3.client(
        "cognito-idp",
        region_name=settings.aws_region,
        endpoint_url=settings.cognito_endpoint or None,
        config=Config(
            connect_timeout=settings.cognito_timeout_seconds,
            read_timeout=settings.cognito_timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    return CognitoClient(settings.cognito_user_pool_id, settings.cognito_organiser_group, client)
