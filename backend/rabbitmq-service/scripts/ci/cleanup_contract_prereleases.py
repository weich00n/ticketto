"""Delete ticketto-contracts pre-releases (X.Y.Z.devN) older than a number of days, of both the
Python package (PyPI) and the Java one (Maven, com.ticketto:ticketto-contracts).

Merge requests publish a pre-release per pipeline for the downstream contract checks; nothing
needs them once the merge request is done. Runs in CI on main merges that change the contracts
(cleanup-contract-prereleases). Released versions (X.Y.Z) are never touched.

GitLab's package cleanup policy can't delete versions by age, so this uses the packages API with
PACKAGE_CLEANUP_TOKEN: a project access token with the `api` scope and the Maintainer role.

  python scripts/ci/cleanup_contract_prereleases.py [--days 30] [--dry-run]
"""

import argparse
import os
import re
import sys
from datetime import UTC, datetime, timedelta

import requests  # only speaks http(s), unlike urllib (Bandit B310)

PRERELEASE = re.compile(r"^\d+\.\d+\.\d+\.dev\d+$")
# (package_type, package_name) of each ticketto-contracts package in the registry
PACKAGES = (("pypi", "ticketto-contracts"), ("maven", "com/ticketto/ticketto-contracts"))


def _call(url: str, token: str, method: str = "GET") -> requests.Response:
    response = requests.request(method, url, headers={"PRIVATE-TOKEN": token}, timeout=30)
    response.raise_for_status()
    return response


def prereleases(api: str, token: str) -> list[dict]:
    """Every ticketto-contracts package version (Python and Java) that is a pre-release, all
    pages. The packages API matches package_name as a substring, so names are checked exactly."""
    found = []
    for package_type, name in PACKAGES:
        page = 1
        while True:
            url = (
                f"{api}/packages?package_type={package_type}&package_name={name}"
                f"&per_page=100&page={page}"
            )
            response = _call(url, token)
            found += [
                p
                for p in response.json()
                if p.get("name", name) == name and PRERELEASE.match(p["version"])
            ]
            if not response.headers.get("X-Next-Page"):
                break
            page = int(response.headers["X-Next-Page"])
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    api = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}"
    token = os.environ.get("PACKAGE_CLEANUP_TOKEN", "")
    if not token:
        print("PACKAGE_CLEANUP_TOKEN is not set; see the docstring.", file=sys.stderr)
        return 1
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    old = [
        p
        for p in prereleases(api, token)
        if datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) < cutoff
    ]
    for package in old:
        verb = "Would delete" if args.dry_run else "Deleting"
        print(
            f"{verb} {package.get('name', 'ticketto-contracts')} {package['version']} "
            f"({package['created_at']})"
        )
        if not args.dry_run:
            _call(f"{api}/packages/{package['id']}", token, method="DELETE")
    print(f"{len(old)} pre-release(s) older than {args.days} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
