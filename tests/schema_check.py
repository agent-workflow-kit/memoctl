"""Schema harness: validate a document against a named schema of ``schemas/``, offline.

A schema is named by its path below ``schemas/`` without ``.json`` (``cli/error``, ``config``).
``validate`` raises ``SchemaViolation`` listing every violation with its path in the document
(``$`` is the root, ``error.retryable``, ``notes[2].id``). ``$ref`` resolves only against the
schemas of this directory; anything else is unresolvable and nothing is ever fetched.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import regress
from jsonschema import validators
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
NAME_RE = re.compile(r"[a-z0-9-]+(/[a-z0-9-]+)*")

# Keywords that make a schema describe a document by itself (defs.json has none: it only holds
# definitions for other schemas to reference).
_ROOT_KEYWORDS = frozenset({"type", "oneOf", "anyOf", "allOf", "$ref", "properties"})
# check-jsonschema 0.38.2 (the version CI pins) matches `pattern` with regress and the "u" flag
# by default; Python's `re` accepts and rejects different patterns and treats `$` differently.
REGRESS_FLAGS = "u"

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
def _cached_schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_ROOT.rglob("*.json")):
        name = path.relative_to(SCHEMA_ROOT).with_suffix("").as_posix()
        out[name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def all_schemas() -> dict[str, dict[str, Any]]:
    """Every schema by name; a private copy, so a test cannot change what others validate with."""
    return copy.deepcopy(_cached_schemas())


def load_schema(name: str) -> dict[str, Any]:
    if not NAME_RE.fullmatch(name) or name not in _cached_schemas():
        raise KeyError(f"unknown schema {name!r}")
    return copy.deepcopy(_cached_schemas()[name])


def _no_retrieval(uri: str) -> Any:
    raise NoSuchResource(ref=uri)


@cache
def _registry() -> Registry[Any]:
    resources = [(s["$id"], Resource.from_contents(s)) for s in _cached_schemas().values()]
    return Registry(retrieve=_no_retrieval).with_resources(resources)  # type: ignore[call-arg]


def compile_pattern(pattern: str) -> regress.Regex:
    """Compile ``pattern`` the way check-jsonschema does (ECMAScript semantics, "u" flag)."""
    return regress.Regex(pattern, flags=REGRESS_FLAGS)


def _matches(pattern: str, instance: str) -> bool:
    return compile_pattern(pattern).find(instance) is not None


def _pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[Any]:
    if validator.is_type(instance, "string") and not _matches(pattern, instance):
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


def _pattern_properties(
    validator: Any, patterns: dict[str, Any], instance: Any, schema: Any
) -> Iterator[Any]:
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in patterns.items():
        for key, value in instance.items():
            if _matches(pattern, key):
                yield from validator.descend(value, subschema, path=key, schema_path=pattern)


def _additional_properties(
    validator: Any, additional: Any, instance: Any, schema: Any
) -> Iterator[Any]:
    if not validator.is_type(instance, "object"):
        return
    known = schema.get("properties", {})
    patterns = list(schema.get("patternProperties", {}))
    extras = [k for k in instance if k not in known and not any(_matches(p, k) for p in patterns)]
    if validator.is_type(additional, "object"):
        for key in extras:
            yield from validator.descend(instance[key], additional, path=key)
    elif not additional and extras:
        yield jsonschema.ValidationError(
            f"additional properties are not allowed ({', '.join(map(repr, extras))} unexpected)"
        )


# The Draft 2020-12 validator with `pattern`, `patternProperties` and `additionalProperties`
# matched by regress, as CI's check-jsonschema does, instead of Python's `re` (where `$` also
# matches before a trailing newline).
_Validator = validators.extend(
    jsonschema.Draft202012Validator,
    {
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "additionalProperties": _additional_properties,
    },
)


def validator_for_schema(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return _Validator(schema, registry=_registry())  # type: ignore[return-value]


def _path(parts: Any) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}" if out else str(p)
    return out or "$"


def validate(name: str, document: Any) -> None:
    schema = load_schema(name)
    if not _ROOT_KEYWORDS & set(schema):
        raise SchemaViolation(
            name, [f"$: not an output schema (it has none of {sorted(_ROOT_KEYWORDS)} at its root)"]
        )
    errors = sorted(
        validator_for_schema(schema).iter_errors(document),
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
    validate it and return the document.

    The document is one line ending in a single LF with nothing before it: docs/interface.md
    (Output on failure) fixes "one JSON object followed by LF; nothing else is printed on
    stdout", and every ``--json`` output follows that rule.
    """
    if not isinstance(raw, bytes):
        raise TypeError("output must be bytes")
    if not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw or raw[:1] in (b" ", b"\t"):
        raise SchemaViolation(
            name,
            ["$: output must be one single-line JSON document followed by exactly one LF"],
        )
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


def resolve_all_refs(name: str, schema: dict[str, Any] | None = None) -> None:
    """Resolve every ``$ref`` of ``schema`` (default: schema ``name``) against the registry,
    raising ``referencing.exceptions.Unresolvable`` for one that points nowhere."""
    schema = load_schema(name) if schema is None else schema
    resolver = _registry().resolver(base_uri=schema["$id"])
    for ref in iter_refs(schema):
        resolver.lookup(ref)
