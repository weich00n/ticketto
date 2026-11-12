"""The OpenTofu after the move to ECS. A mistake in a rename would only show up in AWS."""

import re
from pathlib import Path

import pytest

TERRAFORM = Path(__file__).resolve().parents[2] / "deploy" / "terraform"
SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(TERRAFORM.glob("*.tf")))


@pytest.mark.parametrize(
    "gone",
    [
        r'"aws_lambda_',
        r'"aws_scheduler_',
        r'aws_apigatewayv2_integration" "notifications"',
        r'aws_iam_role" "lambda"',
        r'aws_iam_role" "scheduler"',
        r"/aws/lambda/",
        r'"lambda:',
        r"ecr:SetRepositoryPolicy",
        r'variable "create_functions"',
        r'variable "schedule_enabled"',
        r'variable "api_on_ecs"',
        r'variable "timeout_seconds"',
        r'variable "reserved_concurrency"',
        r'variable "memory_mb"',
    ],
)
def test_the_lambda_setup_is_gone(gone):
    assert not re.search(gone, SOURCE)


def test_opentofu_no_longer_reads_the_broker_password():
    # ECS gets it from Secrets Manager by ARN, so its value needn't be in the state.
    assert 'data "aws_secretsmanager_secret_version" "broker"' not in SOURCE
    assert 'data "aws_secretsmanager_secret" "broker"' in SOURCE


def test_the_workers_security_group_keeps_its_description():
    # Changing a description replaces the group, and rabbitmq-service hard-codes its ID.
    assert 'description = "${var.name} Lambda: outbound only"' in SOURCE


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("aws_security_group.lambda", "aws_security_group.worker"),
        (
            "aws_vpc_security_group_egress_rule.lambda_all",
            "aws_vpc_security_group_egress_rule.worker_all",
        ),
        (
            "aws_vpc_security_group_ingress_rule.user_service_from_lambda",
            "aws_vpc_security_group_ingress_rule.user_service_from_worker",
        ),
        (
            "aws_vpc_security_group_ingress_rule.rds_from_lambda",
            "aws_vpc_security_group_ingress_rule.rds_from_worker",
        ),
    ],
)
def test_renames_are_moved_so_nothing_is_replaced(old, new):
    assert re.search(
        rf"moved \{{\s*from = {re.escape(old)}\s*to\s*= {re.escape(new)}\s*\}}", SOURCE
    )


@pytest.mark.parametrize(
    "gone",
    [
        r'"aws_vpc_endpoint"',
        r"ses_endpoint",
        r'data "aws_subnet" "endpoint"',
    ],
)
def test_the_ses_endpoint_is_gone(gone):
    # The worker has a public IP, so it reaches the public SES API; the endpoint cost ~US$7-8/month.
    assert not re.search(gone, SOURCE)


def test_the_worker_still_has_a_public_ip_to_reach_ses():
    worker = SOURCE[SOURCE.index('resource "aws_ecs_service" "worker"') :]
    assert re.search(r"assign_public_ip\s*=\s*true", worker[: worker.index("\n}\n")])


def test_the_routes_only_point_at_ecs():
    assert (
        'target             = "integrations/${aws_apigatewayv2_integration.notifications_ecs.id}"'
        in SOURCE
    )


def _variable_default(name):
    match = re.search(rf'variable "{name}" \{{.*?default\s*=\s*(.+?)\n', SOURCE, re.S)
    assert match, name
    return match.group(1).strip()


def test_the_worker_reaches_user_service_only_on_its_internal_port():
    # Port 8001 serves only /internal/v1; 8000 has the routes that trust X-User-* headers.
    assert _variable_default("user_service_ports") == "[8001]"
    assert _variable_default("user_service_url") == '"http://user-service.ticketto.local:8001"'
    assert 'variable "user_service_port"' not in SOURCE


def test_the_existing_8000_rule_is_moved_not_replaced():
    # So `tofu apply -var 'user_service_ports=[8000,8001]'` adds 8001 and keeps 8000 until the
    # worker has switched, then a plain apply removes 8000.
    assert re.search(
        r"moved \{\s*from = aws_vpc_security_group_ingress_rule\.user_service_from_worker\s*"
        r'to\s*= aws_vpc_security_group_ingress_rule\.user_service_from_worker\["8000"\]\s*\}',
        SOURCE,
    )
