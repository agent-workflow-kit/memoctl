from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memoctl import cli_errors
from memoctl.cli_errors import (
    FAILED,
    FAILURE_REASONS,
    INTERNAL,
    NEEDS_HUMAN,
    NOT_FOUND,
    REFUSAL_REASONS,
    REFUSED,
    USAGE,
    Failure,
    failure_for,
    register_error,
    usage_failure,
)
from memoctl.config import ConfigError
from memoctl.grammar import GrammarError


@pytest.fixture(autouse=True)
def _restore_registry() -> Any:
    saved = dict(cli_errors.MAPPERS)
    yield
    cli_errors.MAPPERS.clear()
    cli_errors.MAPPERS.update(saved)


def test_grammar_error_maps_to_usage_with_the_field() -> None:
    f = failure_for(GrammarError("expires"))
    assert (f.exit_code, f.code, f.reason, f.details) == (
        USAGE,
        "usage",
        None,
        {"field": "expires"},
    )


def test_config_error_maps_to_config_invalid() -> None:
    f = failure_for(ConfigError("repos.x.limits.y"))
    assert (f.exit_code, f.code, f.details) == (
        USAGE,
        "config_invalid",
        {"field": "repos.x.limits.y"},
    )


def test_unknown_exception_and_plain_value_error_are_internal() -> None:
    assert failure_for(RuntimeError("x")).exit_code == INTERNAL
    assert failure_for(ValueError("x")).exit_code == INTERNAL


def test_nearest_registered_type_in_the_mro_wins() -> None:
    class Base(Exception):
        pass

    class Child(Base):
        pass

    register_error(Base, lambda e: Failure(NOT_FOUND, "not_found", "not found", details={"id": 1}))
    assert failure_for(Child()).exit_code == NOT_FOUND
    register_error(Child, lambda e: Failure(NEEDS_HUMAN, "needs_human", "needs a human"))
    assert failure_for(Child()).exit_code == NEEDS_HUMAN


def test_a_mapper_that_raises_or_misreturns_is_internal() -> None:
    class A(Exception):
        pass

    class B(Exception):
        pass

    def bad(e: Any) -> Failure:
        raise KeyError("x")

    register_error(A, bad)
    register_error(B, lambda e: "not a failure")  # type: ignore[arg-type, return-value]
    assert failure_for(A()).exit_code == INTERNAL
    assert failure_for(B()).exit_code == INTERNAL


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((USAGE, "internal", "m"), {}),  # code of another exit
        ((FAILED, "failed", "m"), {}),  # exit 12 needs a reason
        ((FAILED, "failed", "m"), {"reason": "repo_full"}),  # a refusal reason on exit 12
        ((REFUSED, "refused", "m"), {"reason": "timeout"}),
        ((REFUSED, "refused", "m"), {}),
        ((NOT_FOUND, "not_found", "m"), {"reason": "timeout", "details": {"id": 1}}),
        ((NOT_FOUND, "not_found", "m"), {"details": {"body": "x"}}),
        ((99, "failed", "m"), {}),
    ],
)
def test_inconsistent_failures_are_rejected(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Failure(*args, **kwargs)


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [("db_locked", True), ("timeout", True), ("db_corrupt", False), ("crontab_failed", False)],
)
def test_retryable_follows_the_reason_on_exit_12(reason: str, retryable: bool) -> None:
    f = Failure(FAILED, "failed", "m", reason=reason)
    assert json.loads(f.envelope())["error"]["retryable"] is retryable


def test_rate_limited_is_retryable_and_other_refusals_are_not() -> None:
    rate = {"retry_after_s": 5, "limit": 3, "scope": "repo"}
    assert Failure(REFUSED, "refused", "m", reason="rate_limited", details=rate).retryable
    full = Failure(REFUSED, "refused", "m", reason="repo_full", details={"limit": 3})
    assert not full.retryable


def test_envelope_has_all_five_keys_and_one_lf() -> None:
    text = Failure(
        REFUSED, "refused", "m", "secret_detected", {"class": "github_token", "field": "body"}
    ).envelope()
    assert text.endswith("}\n")
    assert text.count("\n") == 1
    assert list(json.loads(text)["error"]) == ["code", "message", "reason", "details", "retryable"]


def test_status_lines_follow_the_interface() -> None:
    assert Failure(INTERNAL, "internal", "boom").status_line() == "internal: boom\n"
    assert Failure(FAILED, "failed", "m", "timeout").status_line() == "failed (timeout): m\n"
    full = Failure(REFUSED, "refused", "m", "repo_full", {"limit": 1})
    assert full.status_line() == "refused (repo_full): m\n"
    not_found = Failure(NOT_FOUND, "not_found", "m", details={"id": 3})
    assert not_found.status_line() == "not_found: m\n"


@pytest.mark.parametrize("field", ["", "A", "a-b", "x" * 33, "é", "a\nb"])
def test_usage_field_must_be_an_identifier(field: str) -> None:
    assert usage_failure(field).exit_code == INTERNAL


# --- the schema rules (schemas/defs.json error and error_details) are enforced --------------

SECRET_DETAILS = {"class": "github_token", "field": "body"}
RATE_DETAILS = {"retry_after_s": 5, "limit": 3, "scope": "repo"}

# One valid Failure per exit code and reason, built the way a mapper builds it.
VALID: list[Failure] = [
    Failure(INTERNAL, "internal", "internal error"),
    Failure(USAGE, "usage", "invalid topic", details={"field": "topic"}),
    Failure(
        USAGE, "usage", "invalid body", details={"field": "body", "class": "control", "line": 2}
    ),
    Failure(USAGE, "usage", "invalid body", details={"field": "body", "limit": 0}),
    Failure(USAGE, "config_invalid", "invalid configuration", details={"field": "limits.x"}),
    Failure(NOT_FOUND, "not_found", "not found", details={"id": 12}),
    Failure(NEEDS_HUMAN, "needs_human", "needs a human"),
    *[
        Failure(FAILED, "failed", "failed", reason=r)
        for r in ("db_unreadable", "db_locked", "db_corrupt", "timeout", "crontab_failed")
    ],
    Failure(FAILED, "failed", "failed", "schema_newer", {"found": 3, "supported": 2}),
    *[
        Failure(REFUSED, "refused", "refused", reason=r)
        for r in (
            "supersede_mismatch",
            "already_superseded",
            "unsafe_state",
            "cron_block_malformed",
        )
    ],
    Failure(REFUSED, "refused", "refused", "repo_full", {"limit": 100}),
    Failure(REFUSED, "refused", "refused", "store_full", {"limit": 0}),
    Failure(REFUSED, "refused", "refused", "rate_limited", RATE_DETAILS),
    Failure(REFUSED, "refused", "refused", "secret_detected", SECRET_DETAILS),
]


def _validator() -> Any:
    import jsonschema
    from referencing import Registry, Resource

    root = Path(__file__).resolve().parent.parent / "schemas"
    defs = json.loads((root / "defs.json").read_text())
    error = json.loads((root / "cli" / "error.json").read_text())
    registry = Registry().with_resources(
        [(defs["$id"], Resource.from_contents(defs)), (error["$id"], Resource.from_contents(error))]
    )
    return jsonschema.Draft202012Validator(error, registry=registry)


@pytest.mark.parametrize("failure", VALID, ids=lambda f: f"{f.code}-{f.reason}")
def test_every_row_validates_against_the_envelope_schema(failure: Failure) -> None:
    errors = list(_validator().iter_errors(json.loads(failure.envelope())))
    assert errors == []


def test_the_schema_validator_rejects_a_bad_envelope() -> None:
    bad = {"error": {"code": "not_found", "message": "m", "reason": None, "details": None}}
    assert not _validator().is_valid(bad)


def test_every_exit_code_and_reason_has_a_valid_row() -> None:
    assert {f.reason for f in VALID if f.reason} == FAILURE_REASONS | REFUSAL_REASONS
    assert {f.code for f in VALID} == {c for codes in cli_errors._CODES.values() for c in codes}


def test_hostile_field_of_4096_characters_is_valid_and_ascii_escaped() -> None:
    field = "\x1b\u202e" * 2048
    f = Failure(USAGE, "config_invalid", "invalid configuration", details={"field": field})
    assert _validator().is_valid(json.loads(f.envelope()))
    assert f.envelope().isascii()


def test_config_error_field_over_4096_is_cut_to_the_schema_limit() -> None:
    f = failure_for(ConfigError("x" * 5000))
    assert f.code == "config_invalid"
    assert _validator().is_valid(json.loads(f.envelope()))


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        # null details where the schema requires null
        ((INTERNAL, "internal", "m"), {"details": {"field": "x"}}),
        ((NEEDS_HUMAN, "needs_human", "m"), {"details": {"field": "x"}}),
        *[
            ((FAILED, "failed", "m"), {"reason": r, "details": {"limit": 1}})
            for r in ("db_unreadable", "db_locked", "db_corrupt", "timeout", "crontab_failed")
        ],
        *[
            ((REFUSED, "refused", "m"), {"reason": r, "details": {"limit": 1}})
            for r in ("supersede_mismatch", "already_superseded", "unsafe_state")
        ],
        ((REFUSED, "refused", "m"), {"reason": "cron_block_malformed", "details": {"limit": 1}}),
        # required details missing
        ((NOT_FOUND, "not_found", "m"), {}),
        ((NOT_FOUND, "not_found", "m"), {"details": {}}),
        ((USAGE, "usage", "m"), {}),
        ((USAGE, "usage", "m"), {"details": {"limit": 1}}),
        ((USAGE, "config_invalid", "m"), {}),
        ((USAGE, "config_invalid", "m"), {"details": {"id": 1}}),
        ((REFUSED, "refused", "m"), {"reason": "rate_limited"}),
        ((REFUSED, "refused", "m"), {"reason": "rate_limited", "details": {"limit": 1}}),
        ((REFUSED, "refused", "m"), {"reason": "secret_detected"}),
        ((REFUSED, "refused", "m"), {"reason": "secret_detected", "details": {"field": "body"}}),
        ((REFUSED, "refused", "m"), {"reason": "secret_detected", "details": {"class": "jwt"}}),
        ((REFUSED, "refused", "m"), {"reason": "repo_full"}),
        ((REFUSED, "refused", "m"), {"reason": "store_full", "details": {"field": "x"}}),
        ((FAILED, "failed", "m"), {"reason": "schema_newer"}),
        ((FAILED, "failed", "m"), {"reason": "schema_newer", "details": {"found": 3}}),
        # value rules
        ((USAGE, "usage", "m"), {"details": {"field": "Topic"}}),
        ((USAGE, "usage", "m"), {"details": {"field": "x" * 33}}),
        ((USAGE, "usage", "m"), {"details": {"field": "a\n"}}),
        ((USAGE, "config_invalid", "m"), {"details": {"field": "x" * 4097}}),
        ((USAGE, "config_invalid", "m"), {"details": {"field": 5}}),
        ((NOT_FOUND, "not_found", "m"), {"details": {"id": 0}}),
        ((NOT_FOUND, "not_found", "m"), {"details": {"id": 10**18}}),
        ((NOT_FOUND, "not_found", "m"), {"details": {"id": True}}),
        ((NOT_FOUND, "not_found", "m"), {"details": {"id": "3"}}),
        ((USAGE, "usage", "m"), {"details": {"field": "body", "line": 0}}),
        ((USAGE, "usage", "m"), {"details": {"field": "body", "class": "nosuch"}}),
        ((USAGE, "usage", "m"), {"details": {"field": "body", "limit": -1}}),
        ((REFUSED, "refused", "m"), {"reason": "repo_full", "details": {"limit": 1.5}}),
        (
            (REFUSED, "refused", "m"),
            {"reason": "secret_detected", "details": {"class": "private_key", "field": "path"}},
        ),
        (
            (REFUSED, "refused", "m"),
            {"reason": "secret_detected", "details": {"class": "control", "field": "body"}},
        ),
        (
            (REFUSED, "refused", "m"),
            {
                "reason": "rate_limited",
                "details": {"retry_after_s": 0, "limit": 1, "scope": "repo"},
            },
        ),
        (
            (REFUSED, "refused", "m"),
            {
                "reason": "rate_limited",
                "details": {"retry_after_s": 1, "limit": 1, "scope": "user"},
            },
        ),
        (
            (FAILED, "failed", "m"),
            {"reason": "schema_newer", "details": {"found": 3, "supported": 0}},
        ),
        # message rules
        ((INTERNAL, "internal", "x" * 2049), {}),
        ((INTERNAL, "internal", 5), {}),
        ((USAGE, "usage", "m"), {"details": "field"}),
    ],
)
def test_failures_that_break_a_schema_rule_are_rejected(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        Failure(*args, **kwargs)


def test_message_of_exactly_2048_characters_is_accepted() -> None:
    assert Failure(INTERNAL, "internal", "x" * 2048).message == "x" * 2048


def test_a_mapper_that_builds_an_invalid_failure_is_internal() -> None:
    class Bad(Exception):
        pass

    register_error(Bad, lambda e: Failure(NOT_FOUND, "not_found", "m"))
    assert failure_for(Bad()).exit_code == INTERNAL
