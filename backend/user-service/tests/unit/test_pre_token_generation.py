import copy
import json
from pathlib import Path

import pytest

from cognito_triggers.pre_token_generation import handler

EVENTS = Path(__file__).parent.parent / "events" / "pre_token_generation"


def load(name: str) -> dict:
    return json.loads((EVENTS / f"{name}.json").read_text())


def claims(result: dict) -> tuple[dict, dict]:
    details = result["response"]["claimsAndScopeOverrideDetails"]
    return (
        details["accessTokenGeneration"]["claimsToAddOrOverride"],
        details["idTokenGeneration"]["claimsToAddOrOverride"],
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("organiser", "organiser"),
        ("organiser_staff", "organiser,staff"),  # sorted, comma-separated, no spaces
        ("no_groups", ""),
    ],
)
def test_groups_claim_in_both_tokens(event, expected):
    access, id_token = claims(handler(load(event), None))
    assert access == {"groups": expected}
    assert id_token == {"groups": expected}


@pytest.mark.parametrize(
    "group_configuration",
    [
        None,
        {},
        {"groupsToOverride": None},
        {"groupsToOverride": "organiser"},  # not a list
        {"groupsToOverride": ["", None]},
    ],
)
def test_missing_or_malformed_groups_give_an_empty_claim(group_configuration):
    event = load("no_groups")
    event["request"]["groupConfiguration"] = group_configuration
    access, id_token = claims(handler(event, None))
    assert access == id_token == {"groups": ""}


@pytest.mark.parametrize("request_value", [None, "not-a-dict"])
def test_never_raises_on_a_broken_event(request_value):
    # A raise here would block every sign-in on the platform.
    event = load("organiser")
    event["request"] = request_value
    access, _ = claims(handler(event, None))
    assert access == {"groups": ""}


def test_duplicate_groups_appear_once():
    event = load("organiser")
    event["request"]["groupConfiguration"]["groupsToOverride"] = ["organiser", "organiser"]
    access, _ = claims(handler(event, None))
    assert access == {"groups": "organiser"}


def test_rest_of_the_event_is_unchanged():
    event = load("organiser_staff")
    before = copy.deepcopy(event)
    result = handler(event, None)
    del result["response"]["claimsAndScopeOverrideDetails"]
    del before["response"]["claimsAndScopeOverrideDetails"]
    assert result == before


def test_no_other_claims_or_scopes_are_touched():
    details = handler(load("organiser"), None)["response"]["claimsAndScopeOverrideDetails"]
    assert set(details) == {"accessTokenGeneration", "idTokenGeneration"}
    for token in details.values():
        assert set(token) == {"claimsToAddOrOverride"}  # no suppressions, no scope changes
