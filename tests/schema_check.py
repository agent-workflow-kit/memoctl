"""Schema harness: validate a document against a named schema of ``schemas/``, offline.

A schema is named by its path below ``schemas/`` without ``.json`` (``cli/error``, ``config``).
``validate`` raises ``SchemaViolation`` listing every violation with its path in the document
(``$`` is the root, ``error.retryable``, ``notes[2].id``). ``$ref`` resolves only against the
schemas of this directory; anything else is unresolvable and nothing is ever fetched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
NAME_RE = re.compile(r"[a-z0-9-]+(/[a-z0-9-]+)*")

# Keywords whose value maps arbitrary names to schemas (the names are not keywords).
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
# Keywords whose value is data, not schema.
_DATA = frozenset({"enum", "const", "examples", "default"})


class SchemaViolation(AssertionError):
    def __init__(self, name: str, violations: list[str]) -> None:
        self.name = name
        self.violations = violations
        super().__init__(f"{name}: " + f"\n{name}: ".join(violations))


@cache
def all_schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_ROOT.rglob("*.json")):
        name = path.relative_to(SCHEMA_ROOT).with_suffix("").as_posix()
        out[name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def load_schema(name: str) -> dict[str, Any]:
    if not NAME_RE.fullmatch(name) or name not in all_schemas():
        raise KeyError(f"unknown schema {name!r}")
    return all_schemas()[name]


def _no_retrieval(uri: str) -> Any:
    raise NoSuchResource(ref=uri)


@cache
def _registry() -> Registry[Any]:
    resources = [(s["$id"], Resource.from_contents(s)) for s in all_schemas().values()]
    return Registry(retrieve=_no_retrieval).with_resources(resources)  # type: ignore[call-arg]


def validator_for_schema(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(schema, registry=_registry())


def _path(parts: Any) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}" if out else str(p)
    return out or "$"


def validate(name: str, document: Any) -> None:
    errors = sorted(
        validator_for_schema(load_schema(name)).iter_errors(document),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if errors:
        raise SchemaViolation(name, [f"{_path(e.absolute_path)}: {e.message}" for e in errors])


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-JSON constant {token}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def validate_json_output(name: str, raw: bytes) -> Any:
    """Parse a command's stdout strictly (UTF-8, one document, no NaN, no duplicate keys),
    validate it and return the document."""
    if not isinstance(raw, bytes):
        raise TypeError("output must be bytes")
    try:
        doc = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_duplicates
        )
    except ValueError as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise SchemaViolation(name, [f"$: output is not one strict JSON document: {e}"]) from e
    validate(name, doc)
    return doc


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _nodes(item)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in _DATA:
                continue
            if key in _NAME_MAPS and isinstance(value, dict):
                for sub in value.values():
                    yield from _nodes(sub)
            else:
                yield from _nodes(value)


def iter_patterns(schema: Any) -> Iterator[str]:
    for node in _nodes(schema):
        if isinstance(node.get("pattern"), str):
            yield node["pattern"]
        if isinstance(node.get("patternProperties"), dict):
            yield from node["patternProperties"]


def iter_refs(schema: Any) -> Iterator[str]:
    for node in _nodes(schema):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]


def resolve_all_refs(name: str) -> None:
    schema = load_schema(name)
    resolver = _registry().resolver(base_uri=schema["$id"])
    for ref in iter_refs(schema):
        resolver.lookup(ref)
