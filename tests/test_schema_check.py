"""The schema harness and the schemas themselves: valid, offline, regress-compilable."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing.exceptions import Unresolvable

from tests.schema_check import (
    SCHEMA_ROOT,
    SchemaViolation,
    all_schemas,
    compile_pattern,
    iter_patterns,
    iter_refs,
    load_schema,
    resolve_all_refs,
    validate,
    validate_json_output,
    validator_for_schema,
)

DRAFT = "https://json-schema.org/draft/2020-12/schema"
NAMES = sorted(all_schemas())

GOOD_ERROR: dict[str, Any] = {
    "error": {
        "code": "usage",
        "message": "m",
        "reason": None,
        "details": {"field": "limit"},
        "retryable": False,
    }
}


def test_the_schema_root_is_the_repository_schemas_directory() -> None:
    assert SCHEMA_ROOT == Path(__file__).resolve().parent.parent / "schemas"


def test_every_schema_file_is_found() -> None:
    on_disk = sorted(
        p.relative_to(SCHEMA_ROOT).with_suffix("").as_posix() for p in SCHEMA_ROOT.rglob("*.json")
    )
    assert NAMES == on_disk
    assert {"defs", "config", "log", "cli/error", "cli/add"} <= set(NAMES)


# --- the schemas themselves (what the CI schemas job also checks) -----------------------------


@pytest.mark.parametrize("name", NAMES)
def test_schema_is_draft_2020_12_and_valid(name: str) -> None:
    schema = load_schema(name)
    assert schema["$schema"] == DRAFT
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name", NAMES)
def test_schema_id_matches_its_path(name: str) -> None:
    assert load_schema(name)["$id"] == f"memoctl/{name}.json"


@pytest.mark.parametrize("name", NAMES)
def test_every_ref_resolves_offline(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> Any:
        raise AssertionError("a schema check opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for ref in iter_refs(load_schema(name)):
        assert not ref.startswith(("http:", "https:")), ref
    resolve_all_refs(name)


@pytest.mark.parametrize("name", NAMES)
def test_every_pattern_compiles_with_regress(name: str) -> None:
    for pattern in iter_patterns(load_schema(name)):
        compile_pattern(pattern)


def test_the_regress_engine_rejects_a_bad_pattern() -> None:
    with pytest.raises(Exception):  # noqa: B017 - regress raises its own error type
        compile_pattern("(")


@pytest.mark.parametrize("pattern", [r"^a\_b$", r"^\d+\.x{$"])
def test_a_pattern_only_valid_without_the_unicode_flag_fails_the_helper(pattern: str) -> None:
    # Accepted by regress without flags, rejected with check-jsonschema 0.38.2's "u" flag.
    import regress

    regress.Regex(pattern)
    with pytest.raises(Exception):  # noqa: B017
        compile_pattern(pattern)


def test_iter_patterns_finds_nested_and_property_name_patterns() -> None:
    doc = {
        "properties": {"a": {"pattern": "^a$"}},
        "patternProperties": {"^x-": {"pattern": "b"}},
        "items": [{"pattern": "c"}],
        "pattern": "d",
        "enum": [{"pattern": "not a keyword here"}],
        "properties2": {"pattern": "e"},
    }
    assert sorted(iter_patterns(doc)) == ["^a$", "^x-", "b", "c", "d", "e"]


def test_iter_patterns_does_not_treat_a_property_named_pattern_as_a_keyword() -> None:
    doc = {"properties": {"pattern": {"type": "string"}}}
    assert list(iter_patterns(doc)) == []


def test_iter_refs_finds_refs() -> None:
    doc = {"properties": {"a": {"$ref": "defs.json#/$defs/x"}}, "items": {"$ref": "#/$defs/y"}}
    assert sorted(iter_refs(doc)) == ["#/$defs/y", "defs.json#/$defs/x"]


@pytest.mark.parametrize("name", NAMES)
def test_examples_validate(name: str) -> None:
    schema = load_schema(name)
    for example in schema.get("examples", []):
        validate(name, example)


def test_the_config_example_validates_through_the_helper() -> None:
    import yaml

    root = SCHEMA_ROOT.parent
    validate("config", yaml.safe_load((root / "config.example.yaml").read_text()))


# --- the validate helper ----------------------------------------------------------------------


def test_a_valid_document_passes() -> None:
    validate("cli/error", GOOD_ERROR)


def test_refs_into_defs_are_enforced() -> None:
    bad = {"error": {**GOOD_ERROR["error"], "retryable": "no"}}
    with pytest.raises(SchemaViolation) as e:
        validate("cli/error", bad)
    assert "error.retryable" in str(e.value)


def test_the_violation_names_the_path_and_schema() -> None:
    bad = {"error": {**GOOD_ERROR["error"], "code": ["x"]}}
    with pytest.raises(SchemaViolation) as e:
        validate("cli/error", bad)
    msg = str(e.value)
    assert "cli/error" in msg
    assert "error.code" in msg


def test_a_violation_at_the_root_is_reported_as_the_root() -> None:
    with pytest.raises(SchemaViolation, match=r"\$:"):
        validate("cli/error", [])


def test_every_violation_is_listed() -> None:
    with pytest.raises(SchemaViolation) as e:
        validate("cli/error", {"error": {}, "extra": 1})
    assert len(e.value.violations) >= 2


def test_an_unknown_schema_name_fails() -> None:
    with pytest.raises(KeyError):
        validate("cli/nope", {})


@pytest.mark.parametrize("name", ["", "../x", "/etc/passwd", "cli/../defs", "cli/error.json"])
def test_hostile_schema_names_fail(name: str) -> None:
    with pytest.raises(KeyError):
        validate(name, {})


def test_an_unresolvable_ref_does_not_go_to_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> Any:
        raise AssertionError("network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    v = validator_for_schema({"$ref": "https://example.invalid/x.json"})
    with pytest.raises(Unresolvable) as e:
        list(v.iter_errors({}))
    assert "network" not in str(e.value)


# --- patterns are matched by regress, as CI does ------------------------------------------------


def test_a_pattern_does_not_accept_a_trailing_newline() -> None:
    v = validator_for_schema({"type": "string", "pattern": "^[a-z]+$"})
    assert list(v.iter_errors("abc")) == []
    assert list(v.iter_errors("abc\n"))


def test_pattern_properties_use_regress() -> None:
    v = validator_for_schema(
        {"type": "object", "patternProperties": {"^x[a-z]+$": {"type": "integer"}}}
    )
    assert list(v.iter_errors({"xab": 1})) == []
    assert list(v.iter_errors({"xab": "s"}))
    assert list(v.iter_errors({"xab\n": "s"})) == []  # the key does not match, so no rule


def test_additional_properties_respects_pattern_properties_with_regress() -> None:
    v = validator_for_schema(
        {"patternProperties": {"^x[a-z]+$": {}}, "additionalProperties": False}
    )
    assert list(v.iter_errors({"xab": 1})) == []
    assert list(v.iter_errors({"xab\n": 1}))


def test_additional_properties_with_a_schema() -> None:
    v = validator_for_schema({"properties": {"a": {}}, "additionalProperties": {"type": "integer"}})
    assert list(v.iter_errors({"a": "s", "b": 1})) == []
    assert list(v.iter_errors({"b": "s"}))


def _nodes_with(keyword: str, node: Any) -> bool:
    if isinstance(node, dict):
        return keyword in node or any(_nodes_with(keyword, v) for v in node.values())
    if isinstance(node, list):
        return any(_nodes_with(keyword, v) for v in node)
    return False


def test_no_schema_combines_unevaluated_properties_with_pattern_properties() -> None:
    # jsonschema evaluates `unevaluatedProperties` through a private helper that matches
    # `patternProperties` with Python's `re` (where `$` accepts a trailing newline), and that
    # helper cannot be replaced by the regress-based overrides of the harness's validator. So a
    # schema must not use the two together, or the harness and CI's check-jsonschema disagree.
    for name, schema in all_schemas().items():
        assert not (
            _nodes_with("unevaluatedProperties", schema)
            and _nodes_with("patternProperties", schema)
        ), f"{name}: unevaluatedProperties together with patternProperties"


def test_a_deeply_nested_output_is_a_violation_not_a_traceback() -> None:
    with pytest.raises(SchemaViolation, match="not one strict JSON document"):
        validate_json_output("cli/error", b"[" * 100000 + b"\n")


# --- a schema is not a way to skip validation -------------------------------------------------


def test_a_definitions_only_schema_is_refused_as_an_output_schema() -> None:
    with pytest.raises(SchemaViolation, match="not an output schema"):
        validate("defs", {})
    with pytest.raises(SchemaViolation, match="not an output schema"):
        validate_json_output("defs", b"{}\n")


def test_load_schema_returns_a_private_copy() -> None:
    first = load_schema("cli/error")
    first["properties"]["error"] = {"type": "null"}
    del first["$id"]
    assert load_schema("cli/error")["$id"] == "memoctl/cli/error.json"
    validate("cli/error", GOOD_ERROR)


def test_all_schemas_returns_a_private_copy() -> None:
    all_schemas()["cli/error"]["type"] = "null"
    assert all_schemas()["cli/error"]["type"] == "object"
    validate("cli/error", GOOD_ERROR)


# --- parsing raw command output ---------------------------------------------------------------


def test_raw_output_is_parsed_and_validated() -> None:
    doc = validate_json_output("cli/error", json.dumps(GOOD_ERROR).encode() + b"\n")
    assert doc == GOOD_ERROR


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff\xfe{}",
        b'{"error": {}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": 1, "a": 2}',  # duplicate keys
        b"\xef\xbb\xbf{}",  # BOM
    ],
)
def test_malformed_output_is_a_violation(raw: bytes) -> None:
    with pytest.raises(SchemaViolation):
        validate_json_output("cli/error", raw)


def test_output_must_be_one_json_document_per_line_ending() -> None:
    with pytest.raises(SchemaViolation):
        validate_json_output("cli/error", b"{}{}")


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",  # no LF
        b"{}\n\n",  # two LFs
        b"{}\r\n",  # CRLF
        b"\n{}\n",  # leading LF
        b" {}\n",  # leading space
        b'{\n"a": 1}\n',  # a pretty-printed document
        b'{"a":\r1}\n',
    ],
)
def test_output_must_be_one_line_ending_in_one_lf(raw: bytes) -> None:
    with pytest.raises(SchemaViolation, match="single-line"):
        validate_json_output("cli/error", raw)


def test_bytes_only() -> None:
    with pytest.raises(TypeError):
        validate_json_output("cli/error", "{}")  # type: ignore[arg-type]


@pytest.mark.parametrize("ref", ["#/$defs/nope", "nope.json", "../defs.json#/$defs/nope"])
def test_a_broken_ref_is_caught_by_resolution(ref: str) -> None:
    broken = {"$id": "memoctl/cli/error.json", "$ref": ref}
    with pytest.raises(Unresolvable):
        resolve_all_refs("cli/error", broken)


def test_a_good_ref_passes_resolution() -> None:
    resolve_all_refs("cli/error", {"$id": "memoctl/cli/error.json", "$ref": "../defs.json"})
