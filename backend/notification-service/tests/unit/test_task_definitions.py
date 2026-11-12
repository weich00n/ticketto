"""The ECS task definitions agree with the code. A mismatch would only show up in AWS."""

import json
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.worker import STOP_TIMEOUT_SECONDS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
DOCKERFILE = DEPLOY.parent / "Dockerfile"
API_PORT = 8082  # the API's own listener on the shared internal ALB (frontend/deploy/gateway)


def _load(name: str) -> dict:
    return json.loads((DEPLOY / name).read_text(encoding="utf-8"))


API = _load("task-definition.json")
WORKER = _load("worker-task-definition.json")


def _container(definition: dict) -> dict:
    [container] = definition["containerDefinitions"]
    return container


def _names(container: dict, key: str) -> set[str]:
    return {entry["name"] for entry in container.get(key, [])}


@pytest.mark.parametrize("definition", [API, WORKER], ids=["api", "worker"])
def test_both_read_their_database_login_from_secrets_manager(definition):
    container = _container(definition)
    assert {"DB_USER", "DB_PASSWORD"} <= _names(container, "secrets")
    assert not {"DB_USER", "DB_PASSWORD", "RABBITMQ_URL"} & _names(container, "environment")


def test_the_api_has_the_database_and_nothing_else():
    container = _container(API)
    assert _names(container, "secrets") == {"DB_USER", "DB_PASSWORD"}
    assert "taskRoleArn" not in API  # it sends no email, so it needs no AWS permissions
    assert not {"EMAIL_BACKEND", "USER_SERVICE_URL"} & _names(container, "environment")


def test_the_api_listens_where_the_load_balancer_sends_traffic():
    container = _container(API)
    assert container["portMappings"] == [{"containerPort": API_PORT, "protocol": "tcp"}]
    assert f"127.0.0.1:{API_PORT}/health" in " ".join(container["healthCheck"]["command"])
    assert f'"--port", "{API_PORT}"' in DOCKERFILE.read_text(encoding="utf-8")


def test_the_worker_sends_email_through_ses_with_its_own_role():
    container = _container(WORKER)
    assert WORKER["taskRoleArn"] == "__ECS_TASK_ROLE_ARN__"
    assert "RABBITMQ_URL" in _names(container, "secrets")
    environment = {e["name"]: e["value"] for e in container["environment"]}
    assert environment["EMAIL_BACKEND"] == "ses"


def test_the_worker_sends_through_the_tracking_configuration_set():
    environment = {e["name"]: e["value"] for e in _container(WORKER)["environment"]}
    assert environment["SES_CONFIGURATION_SET"] == "__SES_CONFIGURATION_SET__"


def test_the_deploy_script_fills_in_every_placeholder():
    # A missed one fails the deploy job, after the image is already pushed.
    script = (DEPLOY.parent / "scripts" / "ci" / "ecs-migrate.sh").read_text(encoding="utf-8")
    templates = (DEPLOY / "task-definition.json", DEPLOY / "worker-task-definition.json")
    placeholders = {p for t in templates for p in re.findall(r"__[A-Z_]+__", t.read_text())}
    assert placeholders
    assert not {p for p in placeholders if f"s|{p}|" not in script}


def test_the_worker_runs_the_worker_and_has_no_port():
    container = _container(WORKER)
    assert container["command"] == ["python", "-m", "app.worker"]
    assert "portMappings" not in container


def test_the_worker_gets_the_time_to_stop_that_the_code_expects():
    assert _container(WORKER)["stopTimeout"] == STOP_TIMEOUT_SECONDS


def test_the_health_check_reads_the_file_the_worker_writes():
    command = " ".join(_container(WORKER)["healthCheck"]["command"])
    assert Settings.worker_heartbeat_file in command


def test_the_heartbeat_can_get_older_than_a_run_and_a_retry_wait():
    # A healthy worker's file is at most one run plus one wait old (40 s + 60 s).
    command = " ".join(_container(WORKER)["healthCheck"]["command"])
    limit = int(command.rsplit(">", maxsplit=1)[1].split(")")[0])
    assert limit > Settings.worker_run_seconds + Settings.worker_retry_seconds


@pytest.mark.parametrize("definition", [API, WORKER], ids=["api", "worker"])
def test_logs_go_to_the_services_log_group(definition):
    options = _container(definition)["logConfiguration"]["options"]
    assert options["awslogs-group"] == "/ecs/notification-service"
