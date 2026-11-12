"""Check the queues a consumer listens on against the catalogue."""

from __future__ import annotations

from collections.abc import Iterable

from ticketto_contracts import catalogue
from ticketto_contracts.validate import ContractError, Problem


def check_listener_queues(service: str, queues: Iterable[str]) -> list[str]:
    """`missing_queue:<q>` for each catalogued queue `service` doesn't listen on, and
    `extra_queue:<q>` for each queue it listens on that the catalogue doesn't give it. Sorted;
    [] if they match."""
    expected, listened = set(catalogue.queues_for(service)), set(queues)
    return sorted(
        [f"missing_queue:{q}" for q in expected - listened]
        + [f"extra_queue:{q}" for q in listened - expected]
    )


def assert_listener_queues(service: str, queues: Iterable[str]) -> None:
    problems = [
        Problem(
            code,
            f"{queue} "
            + ("is not listened on" if code == "missing_queue" else "is not in the catalogue"),
        )
        for code, queue in (item.split(":", 1) for item in check_listener_queues(service, queues))
    ]
    if problems:
        raise ContractError(f"{service}'s listeners", problems)
