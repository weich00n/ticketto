"""Build the contracts bundle from this checkout into the package before any test reads it,
so the package is always tested against the contracts next to it."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "packages" / "python" / "src" / "ticketto_contracts" / "data"
CASES = REPO / "packages" / "conformance" / "cases.json"

sys.path.insert(0, str(REPO))  # rabbitmq-service's tools/
from tools import build_contracts_bundle  # noqa: E402  pylint: disable=wrong-import-position

build_contracts_bundle.write_bundle(build_contracts_bundle.collect(), DATA)
