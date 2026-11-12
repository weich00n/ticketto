"""Classify a contract change and check that contracts/VERSION was bumped enough.

Compares two bundles (tools/build_contracts_bundle.py), usually the merge request's target branch
and the working tree, and lists every change with its level:

  none   a consumer reads a new field (already guaranteed by the requirement test)
  patch  descriptions, titles, examples, a message's status
  minor  a new message, consumer, schema or optional field; a consumer reading fewer fields; a
         removed field or a field made optional that no consumer reads
  major  anything that can break a publisher or a consumer: a removed field or a field made
         optional that a consumer reads, a new required field, an optional field made required,
         any change to a field's type or constraints, a changed routing key or publisher, a
         removed consumer, message or schema, any envelope change except its descriptions

A schema change this tool can't classify is major. The rules are the versioning table in
docs/CONTRACT-TESTING-PLAN.md.

Usage:
  python -m tools.check_contract_compat --base-ref origin/main
  python -m tools.check_contract_compat --base-dir path/to/published/bundle
  python -m tools.check_contract_compat --base-ref origin/main --affected   # services, one per line
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import build_contracts_bundle as bundles
from tools.build_contracts_bundle import Bundle

LEVELS = ("none", "patch", "minor", "major")
ANNOTATIONS = frozenset(
    {"description", "title", "examples", "$comment", "default", "$id", "$schema"}
)
STRUCTURE = frozenset({"properties", "required", "items", "$defs", "$ref"})


@dataclass(frozen=True)
class Change:
    level: str
    what: str
    services: frozenset[str] = frozenset()

    def __str__(self) -> str:
        return f"{self.level.upper():<5}  {self.what}"


# ---------- schemas as flat field maps ----------


def _merged(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """`node` with local $refs resolved, keeping keywords written next to the $ref."""
    merged = dict(node)
    while "$ref" in merged:
        ref = merged.pop("$ref")
        target = root["$defs"][ref.removeprefix("#/$defs/")]
        merged = {**target, **merged}
    return merged


def _constraints(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Every validation keyword of a field, its array items' included, without annotations
    or the nested properties (those are fields of their own)."""
    merged = _merged(node, root)
    found = {k: v for k, v in merged.items() if k not in ANNOTATIONS | STRUCTURE}
    if "items" in merged:
        found["items"] = _constraints(merged["items"], root)
    return found


def _annotations(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    merged = _merged(node, root)
    found = {k: v for k, v in merged.items() if k in ANNOTATIONS}
    if "items" in merged:
        found["items"] = _annotations(merged["items"], root)
    return found


@dataclass(frozen=True)
class _Field:
    required: bool
    constraints: dict[str, Any]
    annotations: dict[str, Any]


def _object_fields(node: dict[str, Any], root: dict[str, Any], prefix: str) -> dict[str, _Field]:
    merged = _merged(node, root)
    required = set(merged.get("required", []))
    fields: dict[str, _Field] = {}
    for name, child in merged.get("properties", {}).items():
        path = f"{prefix}{name}"
        fields[path] = _Field(
            name in required, _constraints(child, root), _annotations(child, root)
        )
        target = _merged(child, root)
        if "items" in target:
            fields |= _object_fields(target["items"], root, f"{path}[].")
        elif "properties" in target:
            fields |= _object_fields(target, root, f"{path}.")
    return fields


def fields(schema: dict[str, Any]) -> dict[str, _Field]:
    """Every field of a schema by requirement-style path (`a`, `a.b`, `a[].b`). The schema root
    itself is the field ''."""
    root = _Field(True, _constraints(schema, schema), _annotations(schema, schema))
    return {"": root} | _object_fields(schema, schema, "")


def _ancestors(path: str) -> list[str]:
    """The fields containing `path`: `a[].b.c` -> [`a`, `a[].b`]."""
    parts = path.split(".")
    return [".".join(parts[:i]).removesuffix("[]") for i in range(1, len(parts))]


def _parent_also(path: str, paths: set[str]) -> bool:
    """True if a containing field is in `paths`, so only the outermost change is reported."""
    return any(ancestor in paths for ancestor in _ancestors(path))


def _readers(path: str, message: str, requirements: dict[str, dict[str, list[str]]]) -> list[str]:
    """Consumers whose requirements include `path` or anything inside it."""
    return sorted(
        consumer
        for consumer, messages in requirements.items()
        if any(
            p == path or p.startswith((f"{path}.", f"{path}[].")) for p in messages.get(message, [])
        )
    )


def _field_label(path: str) -> str:
    return f"field `{path}`" if path else "the schema"


def _removed_or_made_optional(path: str, what: str, who: list[str]) -> tuple[str, str]:
    """A field consumers can no longer count on: major if one reads it, minor otherwise."""
    if who:
        return "major", f"{_field_label(path)} {what}, but read by {', '.join(who)}"
    return "minor", f"{_field_label(path)} {what} (no consumer reads it)"


def _field_changes(
    before: dict[str, _Field], after: dict[str, _Field], readers: dict[str, list[str]]
) -> list[tuple[str, str]]:
    """(level, description) for every field-level difference."""
    removed, added = set(before) - set(after), set(after) - set(before)
    found = [
        _removed_or_made_optional(path, "removed", readers.get(path, []))
        for path in sorted(removed)
        if not _parent_also(path, removed)
    ]
    for path in sorted(p for p in added if not _parent_also(p, added)):
        if after[path].required:
            found.append(
                ("major", f"required {_field_label(path)} added (publishers must send it)")
            )
        else:
            found.append(("minor", f"optional {_field_label(path)} added"))
    for path in sorted(set(before) & set(after)):
        old, cur = before[path], after[path]
        if old.required and not cur.required:
            found.append(_removed_or_made_optional(path, "made optional", readers.get(path, [])))
        elif cur.required and not old.required:
            found.append(("major", f"{_field_label(path)} made required (publishers must send it)"))
        if old.constraints != cur.constraints:
            found.append(("major", f"{_field_label(path)} type or constraints changed"))
        if old.annotations != cur.annotations:
            found.append(("patch", f"{_field_label(path)} description changed"))
    return found


def diff_schema(
    label: str,
    base: dict[str, Any],
    new: dict[str, Any],
    readers: dict[str, list[str]] | None,
    services: frozenset[str],
) -> list[Change]:
    """Changes between two versions of one schema. `readers` maps a field path to the consumers
    that read it; None means every change that isn't an annotation is major (the envelope)."""
    return [
        Change(
            "major" if readers is None and level != "patch" else level, f"{label}: {what}", services
        )
        for level, what in _field_changes(fields(base), fields(new), readers or {})
    ]


# ---------- whole bundles ----------


def _message_services(message: dict[str, Any] | None) -> set[str]:
    return {message["publisher"], *message["consumers"]} if message else set()


def diff_messages(base: Bundle, new: Bundle) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(set(base.messages) | set(new.messages)):
        old, cur = base.messages.get(name), new.messages.get(name)
        services = frozenset(_message_services(old) | _message_services(cur))
        if old is None:
            changes.append(Change("minor", f"{name}: new message", services))
            continue
        if cur is None:
            changes.append(Change("major", f"{name}: message removed", services))
            continue
        if old["publisher"] != cur["publisher"]:
            changes.append(
                Change(
                    "major", f"{name}: publisher {old['publisher']} -> {cur['publisher']}", services
                )
            )
        if old["routing_key"] != cur["routing_key"]:
            changes.append(
                Change(
                    "major",
                    f"{name}: routing key {old['routing_key']} -> {cur['routing_key']}",
                    services,
                )
            )
        for consumer in sorted(set(cur["consumers"]) - set(old["consumers"])):
            changes.append(
                Change("minor", f"{name}: new consumer {consumer}", frozenset({consumer}))
            )
        for consumer in sorted(set(old["consumers"]) - set(cur["consumers"])):
            changes.append(
                Change("major", f"{name}: consumer {consumer} removed", frozenset({consumer}))
            )
        if old["status"] != cur["status"]:
            changes.append(Change("patch", f"{name}: status {old['status']} -> {cur['status']}"))
    return changes


def diff_payloads(base: Bundle, new: Bundle) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(set(base.schemas) | set(new.schemas)):
        services = frozenset(
            _message_services(base.messages.get(name)) | _message_services(new.messages.get(name))
        )
        old, cur = base.schemas.get(name), new.schemas.get(name)
        if old is None:
            changes.append(Change("minor", f"{name}: payload schema added", services))
        elif cur is None:
            changes.append(Change("major", f"{name}: payload schema removed", services))
        else:
            readers = {path: _readers(path, name, base.requirements) for path in fields(old)}
            changes += diff_schema(name, old, cur, readers, services)
    for name in sorted(set(base.examples) | set(new.examples)):
        old, cur = base.examples.get(name), new.examples.get(name)
        if old != cur:
            message = new.messages.get(name) or base.messages.get(name)
            what = "added" if old is None else "removed" if cur is None else "changed"
            changes.append(
                Change("patch", f"{name}: example {what}", frozenset(_message_services(message)))
            )
    return changes


def diff_requirements(base: Bundle, new: Bundle) -> list[Change]:
    changes: list[Change] = []
    for consumer in sorted(set(base.requirements) | set(new.requirements)):
        old, cur = base.requirements.get(consumer, {}), new.requirements.get(consumer, {})
        for message in sorted(set(old) | set(cur)):
            before, after = set(old.get(message, [])), set(cur.get(message, []))
            who = frozenset({consumer})
            if after - before:
                fields_ = ", ".join(f"`{p}`" for p in sorted(after - before))
                changes.append(Change("none", f"{consumer} now reads {message} {fields_}", who))
            if before - after:
                fields_ = ", ".join(f"`{p}`" for p in sorted(before - after))
                changes.append(
                    Change("minor", f"{consumer} no longer reads {message} {fields_}", who)
                )
    return changes


def diff(base: Bundle, new: Bundle) -> list[Change]:
    """Every change from `base` to `new`, most severe first."""
    everyone = frozenset(base.services() | new.services())
    changes = (
        diff_schema("envelope", base.envelope, new.envelope, None, everyone)
        + diff_messages(base, new)
        + diff_payloads(base, new)
        + diff_requirements(base, new)
    )
    return sorted(changes, key=lambda c: -LEVELS.index(c.level))


def required_level(changes: Iterable[Change]) -> str:
    return max((c.level for c in changes), key=LEVELS.index, default="none")


def affected_services(changes: Iterable[Change]) -> set[str]:
    return set().union(*(c.services for c in changes))


# ---------- versions ----------


def _semver(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"not MAJOR.MINOR.PATCH: {version!r}")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def minimum_version(base: str, level: str) -> str:
    major, minor, patch = _semver(base)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return base


def bump_problem(base: str, new: str, level: str) -> str | None:
    """Why `new` isn't an acceptable VERSION after a `level` change from `base`, or None."""
    needed = minimum_version(base, level)
    if _semver(new) < _semver(base):
        return f"VERSION went down: {base} -> {new}"
    if _semver(new) < _semver(needed):
        return f"a {level} change needs VERSION {needed} or later; it is {new}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base_arg = parser.add_mutually_exclusive_group(required=True)
    base_arg.add_argument("--base-ref", help="git ref to compare with, e.g. origin/main")
    base_arg.add_argument("--base-dir", type=Path, help="bundle directory to compare with")
    parser.add_argument("--affected", action="store_true", help="only print the affected services")
    args = parser.parse_args(argv)

    try:
        base = (
            bundles.collect_ref(args.base_ref)
            if args.base_ref
            else bundles.load_bundle(args.base_dir)
        )
        new = bundles.collect()
    except subprocess.CalledProcessError as error:
        print(
            f"git archive {args.base_ref} failed: {error.stderr.decode().strip()}", file=sys.stderr
        )
        return 2

    changes = diff(base, new)
    if args.affected:
        print("\n".join(sorted(affected_services(changes))))
        return 0

    level = required_level(changes)
    for change in changes:
        print(change)
    if not changes:
        print("No contract changes.")
    problem = bump_problem(base.version, new.version, level)
    print(f"\nRequired bump: {level}. VERSION {base.version} -> {new.version}.")
    if affected := affected_services(changes):
        print(f"Affected services: {', '.join(sorted(affected))}")
    if problem:
        print(
            f"FAIL: {problem}. Bump contracts/VERSION and add a CHANGELOG.md entry.",
            file=sys.stderr,
        )
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
