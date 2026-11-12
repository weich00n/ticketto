"""The consumer process exits cleanly on SIGTERM, even while the broker is down.

Runs `python -m workers.consumer` as a subprocess pointed at closed ports, so it
needs no test services.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_sigterm_while_waiting_for_the_broker(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "MYSQL_URL": "mysql+asyncmy://queue:queue@127.0.0.1:1/queue_service",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "RABBITMQ_URL": "amqp://queue:queue@127.0.0.1:1/%2F",
        "JWT_PRIVATE_KEY_PATH": str(tmp_path / "unused.pem"),
        "JWT_KID": "unused",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "workers.consumer"],
        cwd=tmp_path,  # no stray .env
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(1.5)  # retrying the broker connection
        assert process.poll() is None, process.stdout.read() if process.stdout else ""
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, output
    assert "rabbitmq not ready" in output
