"""Build the contracts bundle: everything the Python and Java packages ship, in one directory.

The bundle is the only thing the packages read, so neither has to parse messages.yaml or know
naming rules such as "checkout-service publishes to checkout.events".

  build/contracts-bundle/
    catalogue.json                     contract version; per message its publisher, exchange,
                                       routing key, consumers, queues, schema and example; per
                                       service what it publishes and consumes; consumer
                                       requirements
    VERSION
    schemas/envelope.schema.json
    schemas/<Message>.payload.schema.json
    examples/<Message>.json

Usage:
  python -m tools.build_contracts_bundle                  # build from the working tree
  python -m tools.build_contracts_bundle --out DIR
  python -m tools.build_contracts_bundle --ref origin/main --out DIR   # build from a git ref

Building from a ref reads that ref's messages.yaml and contracts/ with this version of the
builder, so an older commit can be compared with the working tree (tools/check_contract_compat.py).
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools import contracts
from tools import generate_definitions as gen

ROOT = gen.ROOT
BUNDLE_DIR = ROOT / "build" / "contracts-bundle"
ENVELOPE = "envelope.schema.json"
NO_VERSION = "0.0.0"  # a ref from before contracts/VERSION existed


@dataclass
class Bundle:
    """A bundle in memory: what build_bundle writes and load_bundle reads."""

    catalogue: dict[str, Any]
    envelope: dict[str, Any]
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    examples: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.catalogue["version"]

    @property
    def messages(self) -> dict[str, dict[str, Any]]:
        return self.catalogue["messages"]

    @property
    def requirements(self) -> dict[str, dict[str, list[str]]]:
        return self.catalogue["requirements"]

    def services(self) -> set[str]:
        return set(self.catalogue["services"])


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _message_entry(m: gen.Message, has_schema: bool, has_example: bool) -> dict[str, Any]:
    return {
        "publisher": m.publisher,
        "exchange": m.exchange,
        "routing_key": m.routing_key,
        "status": m.status,
        "consumers": list(m.consumers),
        "queues": [f"{c}.{m.name}" for c in m.consumers],
        "schema": f"schemas/{m.name}.payload.schema.json" if has_schema else None,
        "example": f"examples/{m.name}.json" if has_example else None,
    }


def _service_entries(catalogue: gen.Catalogue) -> dict[str, dict[str, Any]]:
    """Per service: its exchange (publishers only), what it publishes and consumes, its queues."""
    messages = catalogue.messages
    names = sorted({m.publisher for m in messages} | {c for m in messages for c in m.consumers})
    services = {}
    for svc in names:
        publishes = [m.name for m in messages if m.publisher == svc]
        consumes = [m.name for m in messages if svc in m.consumers]
        services[svc] = {
            "exchange": gen.publisher_exchange(svc) if publishes else None,
            "publishes": publishes,
            "consumes": consumes,
            "queues": [f"{svc}.{name}" for name in consumes],
        }
    return services


def _read_if_exists(path: Path) -> dict[str, Any] | None:
    return _read_json(path) if path.exists() else None


def collect(root: Path = ROOT) -> Bundle:
    """Read messages.yaml and contracts/ under `root` into a Bundle."""
    contracts_dir = root / "contracts"
    catalogue = gen.load_catalogue(root / "topology" / "messages.yaml")
    version_file = contracts_dir / "VERSION"
    requirements_dir = contracts_dir / "requirements"

    bundle = Bundle(catalogue={}, envelope=_read_json(contracts_dir / ENVELOPE))
    for m in catalogue.messages:
        schema = _read_if_exists(contracts_dir / f"{m.name}.payload.schema.json")
        example = _read_if_exists(contracts_dir / "examples" / f"{m.name}.json")
        if schema is not None:
            bundle.schemas[m.name] = schema
        if example is not None:
            bundle.examples[m.name] = example
    bundle.catalogue = {
        "version": (
            version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
        )
        or NO_VERSION,
        "vhost": catalogue.vhost,
        "envelope_schema": f"schemas/{ENVELOPE}",
        "messages": {
            m.name: _message_entry(m, m.name in bundle.schemas, m.name in bundle.examples)
            for m in catalogue.messages
        },
        "services": _service_entries(catalogue),
        "requirements": (
            contracts.load_requirements(requirements_dir) if requirements_dir.exists() else {}
        ),
    }
    return bundle


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_bundle(bundle: Bundle, out: Path) -> None:
    """Replace `out` with the bundle's files."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _dump(out / "catalogue.json", bundle.catalogue)
    (out / "VERSION").write_text(bundle.version + "\n", encoding="utf-8")
    _dump(out / "schemas" / ENVELOPE, bundle.envelope)
    for name, schema in bundle.schemas.items():
        _dump(out / "schemas" / f"{name}.payload.schema.json", schema)
    for name, example in bundle.examples.items():
        _dump(out / "examples" / f"{name}.json", example)


def load_bundle(path: Path) -> Bundle:
    """Read a bundle directory, e.g. one unpacked from a published package."""
    catalogue = _read_json(path / "catalogue.json")
    return Bundle(
        catalogue=catalogue,
        envelope=_read_json(path / catalogue["envelope_schema"]),
        schemas={
            n: _read_json(path / m["schema"])
            for n, m in catalogue["messages"].items()
            if m["schema"]
        },
        examples={
            n: _read_json(path / m["example"])
            for n, m in catalogue["messages"].items()
            if m["example"]
        },
    )


def collect_ref(ref: str, root: Path = ROOT) -> Bundle:
    """The bundle as it was at git `ref`: its messages.yaml and contracts/, read by this code."""
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "archive",
            "--format=tar",
            ref,
            "topology/messages.yaml",
            "contracts",
        ],
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp, filter="data")
        return collect(Path(tmp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=BUNDLE_DIR, help=f"default: {BUNDLE_DIR.relative_to(ROOT)}"
    )
    parser.add_argument("--ref", help="build from this git ref instead of the working tree")
    args = parser.parse_args(argv)
    try:
        bundle = collect_ref(args.ref) if args.ref else collect()
    except gen.CatalogueError as error:
        print(f"messages.yaml is invalid: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"git archive {args.ref} failed: {error.stderr.decode().strip()}", file=sys.stderr)
        return 2
    write_bundle(bundle, args.out)
    print(
        f"Contracts {bundle.version}: {len(bundle.messages)} messages, "
        f"{len(bundle.schemas)} payload schemas, {len(bundle.services())} services -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
