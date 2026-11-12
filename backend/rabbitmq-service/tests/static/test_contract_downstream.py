"""The downstream contract checks: tools/plan_contract_downstream.py, the service map in
scripts/ci/contract-test-projects.yaml, and scripts/ci/cleanup_contract_prereleases.py."""

import importlib.util
import json
import shutil
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from tools import generate_definitions as gen
from tools import plan_contract_downstream as downstream

PROJECTS = {
    "queue-service": {"project": "ticketto/queue-service", "branch": "main"},
    "event-service": {"project": "ticketto/event-service", "branch": "develop"},
}

# ---------- the plan ----------


def test_one_trigger_per_wired_affected_service():
    pipeline = downstream.plan({"queue-service", "event-service"}, PROJECTS, "1.1.0.dev7")
    assert list(pipeline) == ["contract-tests event-service", "contract-tests queue-service"]
    assert pipeline["contract-tests queue-service"] == {
        "rules": [{"when": "always"}],
        "variables": {"CONTRACTS_VERSION": "1.1.0.dev7"},
        "trigger": {"project": "ticketto/queue-service", "branch": "main", "strategy": "depend"},
    }
    assert pipeline["contract-tests event-service"]["trigger"]["branch"] == "develop"


@pytest.mark.parametrize("level", ["patch", "minor"])
def test_a_patch_or_minor_change_fails_on_a_failing_service(level):
    pipeline = downstream.plan({"queue-service", "event-service"}, PROJECTS, "1.1.0.dev7", level)
    assert list(pipeline) == ["contract-tests event-service", "contract-tests queue-service"]
    assert not any(job.get("allow_failure") for job in pipeline.values())


def test_a_major_change_only_warns_on_a_failing_service():
    pipeline = downstream.plan({"queue-service", "event-service"}, PROJECTS, "2.0.0.dev7", "major")
    for service in ("event-service", "queue-service"):
        job = pipeline[f"contract-tests {service}"]
        assert job["allow_failure"] is True
        assert job["trigger"]["strategy"] == "depend"  # still runs, and its result still shows
    report = pipeline["contract-tests major change"]
    assert "trigger" not in report
    assert "allow_failure" not in report
    assert any("event-service, queue-service" in line for line in report["script"])


def test_a_major_change_affecting_no_wired_service_has_no_major_report():
    pipeline = downstream.plan({"ticket-service"}, PROJECTS, "2.0.0.dev7", "major")
    assert list(pipeline) == ["contract-tests not wired"]


def test_unaffected_services_are_not_triggered():
    pipeline = downstream.plan({"queue-service"}, PROJECTS, "1.1.0.dev7")
    assert list(pipeline) == ["contract-tests queue-service"]


def test_affected_services_not_wired_yet_are_reported_not_failed():
    pipeline = downstream.plan({"queue-service", "ticket-service"}, PROJECTS, "1.1.0.dev7")
    report = pipeline["contract-tests not wired"]
    assert "trigger" not in report
    assert any("ticket-service" in line for line in report["script"])
    assert "contract-tests queue-service" in pipeline


def test_a_pipeline_always_has_a_job():
    # GitLab rejects an empty child pipeline.
    assert list(downstream.plan(set(), PROJECTS, "1.1.0.dev7")) == ["contract-tests not wired"]
    assert list(downstream.plan({"ticket-service"}, {}, "1.1.0.dev7")) == [
        "contract-tests not wired"
    ]


@pytest.mark.parametrize("level", ["minor", "major"])
@pytest.mark.parametrize(
    "affected", [set(), {"queue-service"}, {"ticket-service"}, {"queue-service", "ticket-service"}]
)
def test_every_job_has_rules_so_merge_request_child_pipelines_keep_it(affected, level):
    # Without rules a job defaults to `only: [branches, tags]`, and is dropped from a child of a
    # merge request pipeline: GitLab then refuses the empty pipeline ("would have been empty").
    pipeline = downstream.plan(affected, PROJECTS, "1.1.0.dev7", level)
    assert pipeline
    assert all(job.get("rules") == [{"when": "always"}] for job in pipeline.values())


@pytest.mark.parametrize("level", ["minor", "major"])
def test_the_plan_is_valid_yaml_for_gitlab(level):
    pipeline = downstream.plan({"queue-service", "ticket-service"}, PROJECTS, "1.1.0.dev7", level)
    assert yaml.safe_load(yaml.safe_dump(pipeline)) == pipeline


# ---------- the service map ----------


def test_the_committed_service_map_loads():
    projects = downstream.load_projects()
    assert set(projects) <= {m.publisher for m in gen.load_catalogue().messages} | {
        c for m in gen.load_catalogue().messages for c in m.consumers
    }


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"billing-service": {"project": "ticketto/billing"}}, "not a service"),
        ({"queue-service": {"project": "queue-service"}}, "needs `project"),
        ({"queue-service": "ticketto/queue-service"}, "needs `project"),
        ({"queue-service": {"project": "ticketto/q", "ref": "main"}}, "unknown keys"),
    ],
)
def test_bad_service_map_entries_are_rejected(tmp_path, entry, error):
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": entry}))
    with pytest.raises(ValueError, match=error):
        downstream.load_projects(path)


def test_branch_defaults_to_main(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": {"queue-service": {"project": "ticketto/q"}}}))
    assert downstream.load_projects(path)["queue-service"]["branch"] == "main"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_main_writes_a_child_pipeline(tmp_path):
    out = tmp_path / "downstream.yml"
    assert (
        downstream.main(["--base-ref", "HEAD", "--version", "1.0.0.dev1", "--out", str(out)]) == 0
    )
    text = out.read_text()
    assert text.startswith("# Generated by tools/plan_contract_downstream.py")
    assert yaml.safe_load(text)


# ---------- pre-release cleanup ----------

SPEC = importlib.util.spec_from_file_location(
    "cleanup_contract_prereleases", gen.ROOT / "scripts" / "ci" / "cleanup_contract_prereleases.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def _age(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def packages_api(monkeypatch):
    """A fake packages API: the PyPI package has two pages, the Maven one one page; records
    DELETEs."""
    pages = {
        1: [
            {"id": 1, "version": "1.0.0", "created_at": _age(90)},
            {"id": 2, "version": "1.1.0.dev10", "created_at": _age(45)},
        ],
        2: [
            {"id": 3, "version": "1.1.0.dev11", "created_at": _age(5)},
            {"id": 4, "version": "1.1.0.dev12", "created_at": _age(31)},
        ],
    }
    maven = [
        {
            "id": 5,
            "name": "com/ticketto/ticketto-contracts",
            "version": "1.1.0.dev10",
            "created_at": _age(45),
        },
        {
            "id": 6,
            "name": "com/ticketto/ticketto-contracts",
            "version": "2.0.0",
            "created_at": _age(45),
        },
        {"id": 7, "name": "com/ticketto/other", "version": "1.1.0.dev9", "created_at": _age(45)},
    ]
    calls: dict = {"deleted": [], "tokens": set(), "queries": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name  # http.server's API
            calls["tokens"].add(self.headers.get("PRIVATE-TOKEN"))
            calls["queries"].append(self.path)
            page = int(self.path.split("page=")[-1])
            self.send_response(200)
            if "package_type=maven" in self.path:
                self.end_headers()
                self.wfile.write(json.dumps(maven).encode())
                return
            if page < len(pages):
                self.send_header("X-Next-Page", str(page + 1))
            self.end_headers()
            self.wfile.write(json.dumps(pages[page]).encode())

        def do_DELETE(self):  # pylint: disable=invalid-name  # http.server's API
            calls["deleted"].append(int(self.path.rsplit("/", 1)[-1]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            del args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CI_API_V4_URL", f"http://127.0.0.1:{server.server_port}/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("PACKAGE_CLEANUP_TOKEN", "cleanup-token")
    yield calls
    server.shutdown()


def test_cleanup_deletes_only_old_prereleases_across_pages(packages_api):
    assert cleanup.main(["--days", "30"]) == 0
    # never a release, never a recent one, never another package
    assert sorted(packages_api["deleted"]) == [2, 4, 5]
    assert any("package_type=maven" in q for q in packages_api["queries"])
    assert packages_api["tokens"] == {"cleanup-token"}


def test_cleanup_dry_run_deletes_nothing(packages_api, capsys):
    assert cleanup.main(["--days", "30", "--dry-run"]) == 0
    assert packages_api["deleted"] == []
    assert "Would delete ticketto-contracts 1.1.0.dev10" in capsys.readouterr().out


def test_cleanup_needs_its_token(packages_api, monkeypatch):
    del packages_api
    monkeypatch.delenv("PACKAGE_CLEANUP_TOKEN")
    assert cleanup.main([]) == 1
