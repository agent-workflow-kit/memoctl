from __future__ import annotations

import json
from typing import Any

import pytest

from memoctl import cli_errors
from memoctl.cli_errors import (
    FAILED,
    INTERNAL,
    NEEDS_HUMAN,
    NOT_FOUND,
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

    register_error(Base, lambda e: Failure(NOT_FOUND, "not_found", "not found"))
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
        ((NOT_FOUND, "not_found", "m"), {"reason": "timeout"}),
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
    assert Failure(REFUSED, "refused", "m", reason="rate_limited").retryable
    assert not Failure(REFUSED, "refused", "m", reason="repo_full").retryable


def test_envelope_has_all_five_keys_and_one_lf() -> None:
    text = Failure(
        REFUSED, "refused", "m", "secret_detected", {"class": "token", "field": "body"}
    ).envelope()
    assert text.endswith("}\n")
    assert text.count("\n") == 1
    assert list(json.loads(text)["error"]) == ["code", "message", "reason", "details", "retryable"]


def test_status_lines_follow_the_interface() -> None:
    assert Failure(INTERNAL, "internal", "boom").status_line() == "internal: boom\n"
    assert Failure(FAILED, "failed", "m", "timeout").status_line() == "failed (timeout): m\n"
    assert Failure(REFUSED, "refused", "m", "repo_full").status_line() == "refused (repo_full): m\n"
    assert Failure(NOT_FOUND, "not_found", "m").status_line() == "not_found: m\n"


@pytest.mark.parametrize("field", ["", "A", "a-b", "x" * 33, "é", "a\nb"])
def test_usage_field_must_be_an_identifier(field: str) -> None:
    assert usage_failure(field).exit_code == INTERNAL
