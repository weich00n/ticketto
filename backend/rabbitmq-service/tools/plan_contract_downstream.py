"""Write the child pipeline that runs affected services' contract tests on a contract change.

For a merge request that changes the contracts, contracts-downstream runs the pipeline this
writes. It has one trigger job per affected service listed in
scripts/ci/contract-test-projects.yaml: each runs that service's `contract-tests` against its
default branch with CONTRACTS_VERSION set to this merge request's pre-release, and with
`strategy: depend`. Affected services that aren't listed yet are reported, not failed.

What a failing service does depends on the change's level (tools/check_contract_compat.py):
  patch, minor  it fails the merge request. These versions promise not to break anyone, so a
                failure means the change is breaking after all: make it compatible, or make it
                a major change.
  major         a warning (allow_failure), and a report job lists the services that must update
                before moving to the new major. Breaking them is expected, and services pin an
                exact version, so publishing the new major can't break them until they upgrade.

Usage (in CI):
  python -m tools.plan_contract_downstream --base-ref FETCH_HEAD --version 1.1.0.dev4821 \\
      --out downstream.yml
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from tools import build_contracts_bundle as bundles
from tools import check_contract_compat as compat

PROJECTS_FILE = bundles.ROOT / "scripts" / "ci" / "contract-test-projects.yaml"
PROJECT_PATH = re.compile(r"^[\w.-]+(/[\w.-]+)+$")
REPORT_IMAGE = "python:3.12-slim"
# Every generated job needs explicit rules. A job without rules defaults to `only: [branches,
# tags]`, and this child pipeline belongs to a merge request pipeline, not a branch: GitLab would
# drop every job and refuse to create an empty pipeline.
ALWAYS = [{"when": "always"}]


def load_projects(path: Path = PROJECTS_FILE) -> dict[str, dict[str, str]]:
    """{service: {"project": path, "branch": branch}} from contract-test-projects.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects = raw.get("projects") or {}
    services = bundles.collect().services()
    result = {}
    for service, entry in projects.items():
        if service not in services:
            raise ValueError(f"{path.name}: {service!r} is not a service in messages.yaml")
        if not isinstance(entry, dict) or not PROJECT_PATH.match(str(entry.get("project", ""))):
            raise ValueError(f"{path.name}: {service}: needs `project: <group>/<project>`")
        unknown = set(entry) - {"project", "branch"}
        if unknown:
            raise ValueError(f"{path.name}: {service}: unknown keys {sorted(unknown)}")
        result[service] = {"project": entry["project"], "branch": entry.get("branch", "main")}
    return result


def _report_job(lines: list[str]) -> dict[str, Any]:
    return {
        "image": REPORT_IMAGE,
        "rules": ALWAYS,
        "script": [f"echo {line!r}" for line in lines],
    }


def _trigger_job(project: dict[str, str], version: str, major: bool) -> dict[str, Any]:
    job: dict[str, Any] = {
        "rules": ALWAYS,
        "variables": {"CONTRACTS_VERSION": version},
        "trigger": {
            "project": project["project"],
            "branch": project["branch"],
            "strategy": "depend",
        },
    }
    if major:
        job["allow_failure"] = True
    return job


def plan(
    affected: set[str], projects: dict[str, dict[str, str]], version: str, level: str = "minor"
) -> dict[str, Any]:
    """The child pipeline: a trigger job per wired affected service, and a report of the rest.

    `level` is the change's level (check_contract_compat.required_level): for "major", a failing
    service is a warning, not a failure.
    """
    major = level == "major"
    pipeline: dict[str, Any] = {}
    wired = sorted(affected & set(projects))
    for service in wired:
        pipeline[f"contract-tests {service}"] = _trigger_job(projects[service], version, major)
    if major and wired:
        pipeline["contract-tests major change"] = _report_job(
            [
                f"Contracts {version} is a major change, so a failing service here is a warning, "
                "not a failure.",
                "A failing service must update before it moves to the new major version: "
                f"check the contract-tests jobs of {', '.join(wired)}.",
            ]
        )
    unwired = sorted(affected - set(projects))
    if unwired or not pipeline:
        lines = [f"Contracts {version}."]
        if not affected:
            lines.append("No service is affected by this change.")
        if unwired:
            lines.append(
                "Affected but not wired into scripts/ci/contract-test-projects.yaml yet, so not "
                f"checked: {', '.join(unwired)}"
            )
        pipeline["contract-tests not wired"] = _report_job(lines)
    return pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True, help="git ref to compare with")
    parser.add_argument("--version", required=True, help="the pre-release to test, X.Y.Z.devN")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        changes = compat.diff(bundles.collect_ref(args.base_ref), bundles.collect())
    except subprocess.CalledProcessError as error:
        print(
            f"git archive {args.base_ref} failed: {error.stderr.decode().strip()}", file=sys.stderr
        )
        return 2
    affected = compat.affected_services(changes)
    level = compat.required_level(changes)
    pipeline = plan(affected, load_projects(), args.version, level)
    header = "# Generated by tools/plan_contract_downstream.py. Do not edit.\n"
    args.out.write_text(header + yaml.safe_dump(pipeline, sort_keys=False), encoding="utf-8")
    wired = [job for job, spec in pipeline.items() if "trigger" in spec]
    print(
        f"Change: {level}. Affected: {', '.join(sorted(affected)) or 'none'}. "
        f"Triggering: {', '.join(wired) or 'none'}"
        + (" (failures are warnings: major change)." if level == "major" else ".")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
