"""Upload the tested ticketto-contracts wheel to this project's GitLab PyPI registry.

Uploads the one wheel contracts-python built and tested in dist/:

  python scripts/ci/publish_contracts_python.py               # main: X.Y.Z, contracts/VERSION
  python scripts/ci/publish_contracts_python.py --prerelease  # merge request: X.Y.Z.devN

With --prerelease it also writes prerelease.env (PRERELEASE_VERSION=...), a dotenv report for
the downstream contract checks. A version already in the registry is skipped: published versions
are immutable, so a rerun is harmless.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import requests  # installed with twine; only speaks http(s), unlike urllib (Bandit B310)

WHEEL = re.compile(r"ticketto_contracts-(?P<version>[^-]+)-py3-none-any\.whl")


def _published_files(index: str, token: str) -> str:
    """The registry's simple-index page for ticketto-contracts, or "" if nothing is published."""
    response = requests.get(
        f"{index}/simple/ticketto-contracts/", auth=("gitlab-ci-token", token), timeout=30
    )
    if response.status_code == 404:
        return ""
    response.raise_for_status()
    return response.text


def _wheel(prerelease: bool) -> tuple[Path, str] | str:
    """(wheel, version), or why there is no acceptable wheel."""
    wheels = sorted(Path("dist").glob("ticketto_contracts-*.whl"))
    if len(wheels) != 1:
        return f"expected one ticketto_contracts wheel in dist/, found {len(wheels)}"
    match = WHEEL.fullmatch(wheels[0].name)
    if not match:
        return f"unexpected wheel name {wheels[0].name}"
    version = match["version"]
    contract = Path("contracts/VERSION").read_text(encoding="utf-8").strip()
    if prerelease and not re.fullmatch(rf"{re.escape(contract)}\.dev\d+", version):
        return f"a pre-release must be {contract}.devN, the wheel is {version}"
    if not prerelease and version != contract:
        return f"the wheel is {version}, contracts/VERSION is {contract}"
    return wheels[0], version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prerelease", action="store_true", help="upload an X.Y.Z.devN wheel")
    args = parser.parse_args(argv)

    found = _wheel(args.prerelease)
    if isinstance(found, str):
        print(found, file=sys.stderr)
        return 1
    wheel, version = found
    index = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}/packages/pypi"
    token = os.environ["CI_JOB_TOKEN"]
    if wheel.name in _published_files(index, token):
        print(f"ticketto-contracts {version} is already published; nothing to upload.")
    else:
        subprocess.run(
            [sys.executable, "-m", "twine", "upload", "--non-interactive"]
            + ["--repository-url", index, str(wheel)],
            check=True,
            env={**os.environ, "TWINE_USERNAME": "gitlab-ci-token", "TWINE_PASSWORD": token},
        )
        print(f"Published ticketto-contracts {version}.")
    if args.prerelease:
        Path("prerelease.env").write_text(f"PRERELEASE_VERSION={version}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
