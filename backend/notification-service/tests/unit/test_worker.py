"""The worker loop that ECS keeps running. Tests 1-7 of rollout-plan.md, Part 3."""

import os
import signal
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import run as run_module
from app import worker
from tests.unit.conftest import OTHER_EVENT, FakeChannel, event_published, user

QUEUE = "notification-service.EventPublished"
PUBLISHED = QUEUE
QUIET_RUN = {"drained": {QUEUE: 0}, "requeued": {}, "dead_lettered": {}, "sent": 0}


@pytest.fixture
def worker_settings(settings, tmp_path):
    return replace(
        settings,
        worker_run_seconds=40,
        worker_poll_seconds=10,
        worker_retry_seconds=60,
        worker_heartbeat_file=str(tmp_path / "heartbeat"),
    )


@pytest.fixture
def app_state(worker_settings):
    return SimpleNamespace(
        settings=worker_settings,
        session_factory="f",
        users="u",
        events="e",
        sender="m",
        connect="c",
    )


class StopAfter:
    """Stands in for threading.Event: records each wait, and stops the loop after `waits`."""

    def __init__(self, waits: int):
        self.waits: list[float] = []
        self.left = waits

    def is_set(self) -> bool:
        return self.left <= 0

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.left -= 1


def _runs(monkeypatch, *results) -> list[dict]:
    """Make run_once return (or raise) each of `results` in turn; returns the calls made."""
    calls: list[dict] = []
    outcomes = iter(results)

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    return calls


def test_a_normal_run_waits_the_poll_interval(monkeypatch, app_state):
    _runs(monkeypatch, QUIET_RUN)
    stop = StopAfter(1)
    worker.run_forever(app_state, stop)
    assert stop.waits == [10]


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({**QUIET_RUN, "requeued": {QUEUE: 1}}, id="requeued message"),
        pytest.param({**QUIET_RUN, "broker_error": "AMQPConnectionError: down"}, id="broker error"),
    ],
)
def test_a_failed_run_waits_the_retry_interval(monkeypatch, app_state, result):
    # A 10 s loop would otherwise use up a message's 5 deliveries in under a minute.
    _runs(monkeypatch, result)
    stop = StopAfter(1)
    worker.run_forever(app_state, stop)
    assert stop.waits == [60]


def test_a_dead_lettered_message_isnt_a_failure(monkeypatch, app_state):
    # Poison goes straight to its DLQ: waiting longer wouldn't change anything.
    _runs(monkeypatch, {**QUIET_RUN, "dead_lettered": {QUEUE: 1}})
    stop = StopAfter(1)
    worker.run_forever(app_state, stop)
    assert stop.waits == [10]


def test_a_run_that_hit_its_limit_starts_the_next_straight_away(monkeypatch, app_state):
    # A backlog clears at full speed, and an idle queue still waits.
    _runs(monkeypatch, {**QUIET_RUN, "more_to_do": True}, {**QUIET_RUN, "more_to_do": False})
    stop = StopAfter(2)
    worker.run_forever(app_state, stop)
    assert stop.waits == [0, 10]


def test_a_failure_still_waits_even_with_more_to_do(monkeypatch, app_state):
    # Retrying straight away would use up the failing message's deliveries.
    _runs(monkeypatch, {**QUIET_RUN, "requeued": {QUEUE: 1}, "more_to_do": True})
    stop = StopAfter(1)
    worker.run_forever(app_state, stop)
    assert stop.waits == [60]


def test_a_run_that_raises_doesnt_end_the_loop(monkeypatch, app_state, caplog):
    calls = _runs(monkeypatch, RuntimeError("database down"), QUIET_RUN)
    stop = StopAfter(2)
    worker.run_forever(app_state, stop)
    assert len(calls) == 2
    assert stop.waits == [60, 10]
    assert "run failed" in caplog.text


def test_each_run_gets_a_deadline_of_the_run_seconds(monkeypatch, app_state):
    calls = _runs(monkeypatch, QUIET_RUN)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    worker.run_forever(app_state, StopAfter(1))
    assert calls[0]["deadline"] == pytest.approx(140.0)
    assert calls[0]["settings"] is app_state.settings
    assert calls[0]["sender"] == "m" and calls[0]["connect"] == "c"


def _heartbeat_age(app_state) -> float:
    return os.path.getmtime(app_state.settings.worker_heartbeat_file)


def test_the_heartbeat_is_written_after_a_run(monkeypatch, app_state):
    _runs(monkeypatch, QUIET_RUN)
    worker.run_forever(app_state, StopAfter(1))
    assert os.path.exists(app_state.settings.worker_heartbeat_file)


def test_the_heartbeat_is_written_after_a_failed_run_too(monkeypatch, app_state):
    # A failed run is still alive: only a stuck one should get the task replaced.
    path = app_state.settings.worker_heartbeat_file
    written = []

    def fail_and_check(**_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(worker, "run_once", fail_and_check)
    real = worker.touch_heartbeat
    monkeypatch.setattr(worker, "touch_heartbeat", lambda p: written.append(p) or real(p))
    worker.run_forever(app_state, StopAfter(1))
    assert written[-1] == path and os.path.exists(path)


def test_the_heartbeat_exists_before_the_first_run_ends(monkeypatch, app_state):
    # The health check mustn't see a missing file during a long first run.
    path = app_state.settings.worker_heartbeat_file
    seen = []

    def check_during_run(**_kwargs):
        seen.append(os.path.exists(path))
        return QUIET_RUN

    monkeypatch.setattr(worker, "run_once", check_during_run)
    worker.run_forever(app_state, StopAfter(1))
    assert seen == [True]


def test_a_heartbeat_that_cant_be_written_doesnt_stop_the_loop(monkeypatch, app_state, tmp_path):
    bad = replace(app_state.settings, worker_heartbeat_file=str(tmp_path / "missing" / "beat"))
    calls = _runs(monkeypatch, QUIET_RUN)
    worker.run_forever(SimpleNamespace(**{**vars(app_state), "settings": bad}), StopAfter(1))
    assert len(calls) == 1


@pytest.fixture
def signals():
    """Put the real signal handlers back after a test that replaces them."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for number, handler in saved.items():
        signal.signal(number, handler)


@pytest.mark.usefixtures("signals")
def test_sigterm_ends_the_run_at_the_next_check_and_exits_0(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    monkeypatch, app_state, session_factory, users, events, sender
):
    # Stopping takes one message or one email at most, not the rest of a 40 s run.
    users.opted_in_users = [user(1)]
    channel = FakeChannel({PUBLISHED: [event_published(), event_published(OTHER_EVENT)]})
    handled = []
    real_handle = run_module.handle_message

    def handle_then_get_stopped(*args):
        outcome = real_handle(*args)  # the message in hand still finishes
        handled.append(outcome)
        os.kill(os.getpid(), signal.SIGTERM)  # ECS stopping the task, mid-run
        return outcome

    monkeypatch.setattr(run_module, "handle_message", handle_then_get_stopped)
    app = SimpleNamespace(
        settings=app_state.settings,
        session_factory=session_factory,
        users=users,
        events=events,
        sender=sender,
        connect=lambda _url: SimpleNamespace(channel=lambda: channel, close=lambda: None),
    )
    monkeypatch.setattr(worker, "build_app", lambda _settings: app)
    monkeypatch.setattr(worker.Settings, "from_environment", lambda: app.settings)

    assert worker.main() == 0

    assert handled == ["queued_1"]  # the first message, and not the second
    assert len(channel.queues[PUBLISHED]) == 1  # which waits safely for the next task
    assert sender.sent == []  # and no emails were started after the signal


@pytest.mark.usefixtures("signals")
def test_sigterm_during_the_wait_exits_straight_away(monkeypatch, app_state):
    _runs(monkeypatch, QUIET_RUN)
    monkeypatch.setattr(worker, "build_app", lambda _settings: app_state)
    monkeypatch.setattr(worker.Settings, "from_environment", lambda: app_state.settings)
    long_wait = replace(app_state.settings, worker_poll_seconds=3600)
    app_state.settings = long_wait
    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        assert worker.main() == 0
    finally:
        timer.cancel()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RABBITMQ_URL", ""),
        ("USER_SERVICE_URL", ""),
        ("EMAIL_BACKEND", "carrier-pigeon"),
        ("WORKER_POLL_SECONDS", "soon"),
        ("WORKER_RUN_SECONDS", "0"),
    ],
)
def test_bad_config_exits_non_zero_at_startup(monkeypatch, name, value):
    # So the deployment's circuit breaker sees a bad release, not a loop that fails quietly.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused.db")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://notification-service:x@broker:5672/%2F")
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-service.ticketto.local:8000")
    monkeypatch.setenv("EMAIL_BACKEND", "log")
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(worker, "run_forever", lambda *_: pytest.fail("must not start"))
    assert worker.main() != 0


def test_the_clock_says_times_up_once_stopping():
    stop = threading.Event()
    clock = worker.stoppable_clock(stop)
    assert clock() < 1e12
    stop.set()
    assert clock() == float("inf")
