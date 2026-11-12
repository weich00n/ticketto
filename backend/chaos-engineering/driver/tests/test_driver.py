from unittest import mock

import pytest
import requests

from driver import journey
from driver.api import Gateway, Step, StepFailed, call
from driver.report import percentile, summarise


def response(status: int, body: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = (str(body).replace("'", '"') if body is not None else "").encode()
    return resp


def session(*replies):
    s = mock.Mock()
    s.request.side_effect = list(replies)
    return s


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_call_retries_5xx_and_network_errors_then_succeeds():
    step = Step()
    s = session(response(503, {"error": {"code": "DEPENDENCY_UNAVAILABLE"}}),
                requests.ConnectionError("refused"), response(201, {"ok": True}))
    assert call(s, "POST", "u", step).status_code == 201
    assert step.errors == ["503 DEPENDENCY_UNAVAILABLE", "ConnectionError"]


def test_call_stops_on_a_final_4xx_with_its_code():
    s = session(response(409, {"error": {"code": "SOLD_OUT"}}))
    with pytest.raises(StepFailed) as exc:
        call(s, "POST", "u", Step())
    assert exc.value.tag == "SOLD_OUT" and exc.value.status == 409


def test_call_gives_up_when_the_budget_runs_out():
    s = mock.Mock()
    s.request.return_value = response(502)
    with pytest.raises(StepFailed, match="retries_exhausted"):
        call(s, "GET", "u", Step(), budget=0)


def test_journey_tags_the_step_that_failed():
    gw = Gateway("https://gw", "token")
    gw.session = session(response(201, {"entry_status": "ADMITTED", "admission_token": "t"}),
                         response(409, {"error": {"code": "HOLD_LIMIT_EXCEEDED"}}))
    result = journey.run(gw, "sk_test", "e", "c", "a@b.c", "buyer", journey.Limits())
    assert result.outcome == "hold_failed"
    assert result.detail == "HOLD_LIMIT_EXCEEDED"
    assert set(result.steps) == {"queue", "hold"}


def test_percentile_and_summary():
    assert percentile([1, 2, 3, 4, 100], 50) == 3
    assert percentile([], 95) == 0
    rows = [
        {"outcome": "confirmed", "detail": "", "steps": {"hold": {"ms": 100, "errors": []}}},
        {"outcome": "hold_failed", "detail": "retries_exhausted",
         "steps": {"hold": {"ms": 900, "errors": ["503 DEPENDENCY_UNAVAILABLE"]}}},
    ]
    text = summarise(rows)
    assert "success rate (confirmed with ticket): 50.0%" in text
    assert "503 DEPENDENCY_UNAVAILABLE" in text
