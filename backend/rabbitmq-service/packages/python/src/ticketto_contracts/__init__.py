"""Ticketto's AMQP message contracts, and the contract tests a service runs against them.

    from ticketto_contracts import catalogue, assert_valid_publish, RecordingChannel

The contracts (catalogue, schemas, examples, consumer requirements) are bundled in data/ and
versioned together: ticketto-contracts X.Y.Z is contracts X.Y.Z. The generated pytest suite
is in ticketto_contracts.suite.
"""

from ticketto_contracts import bodies, catalogue
from ticketto_contracts.listeners import assert_listener_queues, check_listener_queues
from ticketto_contracts.recording import RecordingChannel
from ticketto_contracts.validate import (
    ContractError,
    Problem,
    PublishedMessage,
    assert_valid_publish,
    check_publish,
)

__all__ = [
    "ContractError",
    "Problem",
    "PublishedMessage",
    "RecordingChannel",
    "assert_listener_queues",
    "assert_valid_publish",
    "bodies",
    "catalogue",
    "check_listener_queues",
    "check_publish",
]
