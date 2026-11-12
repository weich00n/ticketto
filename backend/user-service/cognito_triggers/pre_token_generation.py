"""Cognito pre token generation trigger (event version V2_0), deployed as its own Lambda.

API Gateway HTTP APIs can't map a claim whose name contains a colon, so `cognito:groups` never
reaches the services. This copies the groups into a colon-free `groups` claim, comma-separated
and sorted ("organiser,staff", or "" for no groups), in both the access token and the ID token.
API Gateway then sets X-User-Groups from `$context.authorizer.claims.groups`.

If this raises, Cognito refuses the sign-in, for every user of every service. So it has no
dependencies, makes no network calls, and falls back to "" rather than fail.
Plan and rollout: ../cognito-groups-plan.md
"""

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLAIM = "groups"


def groups_claim(event: dict) -> str:
    group_configuration = (event.get("request") or {}).get("groupConfiguration") or {}
    groups = group_configuration.get("groupsToOverride")
    if not isinstance(groups, list):
        return ""
    return ",".join(sorted({str(group) for group in groups if group}))


def handler(event: dict, _context) -> dict:
    try:
        value = groups_claim(event)
    except Exception:  # pylint: disable=broad-exception-caught  # never block a sign-in
        logger.exception("Could not read the groups; issuing the token with an empty claim")
        value = ""

    claims = {CLAIM: value}
    event["response"] = {
        **(event.get("response") or {}),
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {"claimsToAddOrOverride": claims},
            "idTokenGeneration": {"claimsToAddOrOverride": claims},
        },
    }
    return event
