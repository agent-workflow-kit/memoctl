"""Author kind and actor resolution: precedence, hostile variable values, allow list."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from memoctl.author import (
    ActorSource,
    KindSource,
    kind_is_shown,
    resolve_actor,
    resolve_kind,
)
from memoctl.config import Config
from memoctl.grammar import GrammarError
from tests.fake_gateway import FakeGateway


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path)


def test_flag_wins_over_variable(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    result = resolve_kind(gw, Config(), "implement")
    assert (result.kind, result.source) == ("implement", KindSource.FLAG)


def test_variable_used_without_flag(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    result = resolve_kind(gw, Config(), None)
    assert (result.kind, result.source) == ("audit", KindSource.ENV)


def test_unset_variable_is_unknown(gw: FakeGateway) -> None:
    result = resolve_kind(gw, Config(), None)
    assert (result.kind, result.source) == ("unknown", KindSource.DEFAULT)


def test_empty_variable_is_unknown(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = ""
    assert resolve_kind(gw, Config(), None).kind == "unknown"


def test_configured_variable_name_is_read(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    gw.environ["MY_KIND"] = "improve"
    assert resolve_kind(gw, Config(kind_env="MY_KIND"), None).kind == "improve"


def test_per_repo_config_keeps_global_kind_env(gw: FakeGateway) -> None:
    config = Config.model_validate(
        {"kind_env": "MY_KIND", "repos": {"example.com/a/b": {"retention": {"max_age": "2d"}}}}
    )
    gw.environ["MY_KIND"] = "improve"
    assert resolve_kind(gw, config.for_repo("example.com/a/b"), None).kind == "improve"


HOSTILE = [
    "Audit",
    "a b",
    "audit\n",
    "\naudit",
    "audit\0",
    "-audit",
    "_audit",
    "a" * 65,
    "a" * 1_000_000,
    "ａｕｄｉｔ",  # noqa: RUF001
    "audit́",
    "‮audit",
    " ",
    "$(id)",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_malformed_variable_is_unknown_and_not_echoed(gw: FakeGateway, value: str) -> None:
    gw.environ["CSESSION_KIND"] = value
    result = resolve_kind(gw, Config(), None)
    assert result.kind == "unknown"
    assert result.source is KindSource.DEFAULT
    assert len(value) < 3 or value not in repr(result)
    assert gw.terminal.stderr == "" and gw.terminal.stdout == ""


def test_longest_valid_variable_accepted(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "a" * 64
    assert resolve_kind(gw, Config(), None).kind == "a" * 64


@pytest.mark.parametrize("value", HOSTILE + ["", "UNKNOWN "])
def test_malformed_flag_is_usage_error_naming_field(gw: FakeGateway, value: str) -> None:
    with pytest.raises(GrammarError) as info:
        resolve_kind(gw, Config(), value)
    assert info.value.field == "kind"
    assert len(value) < 3 or value not in str(info.value)


def test_malformed_flag_is_not_rescued_by_good_variable(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    with pytest.raises(GrammarError):
        resolve_kind(gw, Config(), "Bad Kind")


def test_explicit_unknown_flag_is_allowed(gw: FakeGateway) -> None:
    assert resolve_kind(gw, Config(), "unknown").kind == "unknown"


def test_resolution_never_reads_the_process_environment(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CSESSION_KIND", "audit")
    assert resolve_kind(gw, Config(), None).kind == "unknown"


# ---------- allow list ----------


def test_default_allow_list() -> None:
    config = Config()
    assert kind_is_shown("implement", config)
    assert not kind_is_shown("unknown", config)
    assert not kind_is_shown("custom", config)


def test_allow_list_can_admit_unknown_and_be_empty() -> None:
    admit = Config.model_validate({"context": {"author_kinds": ["unknown"]}})
    assert kind_is_shown("unknown", admit)
    empty = Config.model_validate({"context": {"author_kinds": []}})
    assert not kind_is_shown("implement", empty)


def test_allow_list_is_per_repo() -> None:
    config = Config.model_validate(
        {"repos": {"example.com/a/b": {"context": {"author_kinds": ["custom"]}}}}
    )
    assert kind_is_shown("custom", config.for_repo("example.com/a/b"))
    assert not kind_is_shown("custom", config.for_repo("example.com/a/c"))


# ---------- actor ----------


def test_actor_flag_is_checked_and_sourced(gw: FakeGateway) -> None:
    result = resolve_actor(gw, "auto:memoctl-cron")
    assert (result.actor, result.source) == ("auto:memoctl-cron", ActorSource.FLAG)


@pytest.mark.parametrize(
    "value", ["", "root", "human:", "human:a b", "human:a\n", "robot:x", "human:" + "x" * 65]
)
def test_bad_actor_flag(gw: FakeGateway, value: str) -> None:
    with pytest.raises(GrammarError) as info:
        resolve_actor(gw, value)
    assert info.value.field == "actor"


def test_default_actor_not_a_terminal(gw: FakeGateway) -> None:
    result = resolve_actor(gw, None)
    assert (result.actor, result.source) == ("auto:unknown", ActorSource.DEFAULT)


def test_default_actor_terminal_uses_login(gw: FakeGateway) -> None:
    gw.terminal.tty[0] = True
    result = resolve_actor(gw, None)
    assert (result.actor, result.source) == ("human:tester", ActorSource.TTY)


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        (None, "unknown"),
        ("", "unknown"),
        ("a b/c", "a_b_c"),
        ("jäke", "j_ke"),
        ("a\nb\0c", "a_b_c"),
        ("x" * 100, "x" * 64),
        ("a.b_c@d-e", "a.b_c@d-e"),
        ("日本語", "___"),
    ],
)
def test_login_name_is_sanitized(gw: FakeGateway, login: str | None, expected: str) -> None:
    gw.terminal.tty[0] = True
    gw.login = login
    assert resolve_actor(gw, None).actor == f"human:{expected}"


def test_login_lookup_oserror_is_unknown(gw: FakeGateway) -> None:
    def failing() -> str | None:
        raise OSError("no passwd entry")

    gw.terminal.tty[0] = True
    gw.login_name = failing  # type: ignore[method-assign]
    result = resolve_actor(gw, None)
    assert (result.actor, result.source) == ("human:unknown", ActorSource.TTY)


def test_flag_wins_over_terminal(gw: FakeGateway) -> None:
    gw.terminal.tty[0] = True
    assert resolve_actor(gw, "human:dash").source is ActorSource.FLAG


def test_grammar_error_pickles() -> None:
    assert pickle.loads(pickle.dumps(GrammarError("kind"))).field == "kind"
