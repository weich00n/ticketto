from __future__ import annotations

import pytest

from scripts.load_smoke import Stats, percentile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("pct", "expected"), [(50, 50.0), (95, 95.0), (99, 99.0), (100, 100.0), (1, 1.0)]
)
def test_nearest_rank_percentile(pct: float, expected: float) -> None:
    assert percentile([float(v) for v in range(100, 0, -1)], pct) == expected


def test_percentile_of_nothing_is_zero() -> None:
    assert percentile([], 99) == 0.0


def test_report_lists_each_endpoint() -> None:
    stats = Stats()
    stats.record("join", 0.010, 201)
    stats.record("me", 0.004, 200)
    stats.record("me", 0.006, 200)
    report = stats.report()
    assert "join" in report and "me" in report and "{200: 2}" in report
