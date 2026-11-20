"""Grammar and duration behavior: documented cases, hostile input, linear time."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from memoctl import grammar
from memoctl.grammar import GrammarError

NON_ASCII_DIGITS = ["٣", "１２", "५", "𝟙", "²", "①"]  # noqa: RUF001


# ---------- fullmatch, never `$` ----------

VALID = {
    "repo": ["example.com/org/repo", "a/b", "host:8080/a/b.c_d-e", "h.o-st9/x/y/z", "a-b.c/.x"],
    "topic": ["a", "0", "build.notes_v-2", "a" * 64],
    "label_key": ["a", "k.e_y-1", "a" * 64],
    "label_value": ["", "v", "a=b=c", "A-Z.0_9:/#@+=,-", "a" * 256],
    "kind": ["a", "implement", "draft-ticket", "a_b-c", "a" * 64],
    "id": ["1", "9", "10", "123456789012345678"],
    "actor": ["human:jake", "auto:unknown", "auto:a.b_c@d/e-f", "human:" + "x" * 64],
    "kind_env": ["A", "CSESSION_KIND", "A" + "B" * 63],
}

INVALID = {
    "repo": [
        "",
        "a",
        "a/",
        "/a",
        "A/b",
        "a/b/",
        "a//b",
        "a/./b",
        "a/../b",
        "a/b/.",
        "a/b/..",
        "a/b c",
        "a:/b",
        "a:123456/b",
        "a:12a/b",
        "-a/b",
        "a-/b",
        ".a/b",
        "a./b",
        "a/b\\c",
        "a/é",
        "a:١٢/b",
    ],
    "topic": ["", "-a", ".a", "_a", "A", "a" * 65, "a b", "a/b", "é"],
    "label_key": ["", "-a", "A", "a" * 65, "a=b", "a b"],
    "label_value": ["a b", "a" * 257, "a;b", "é", "a\x00", "a\t"],
    "kind": ["", "-a", "_a", "A", "a" * 65, "a.b", "a b", "unknown\x00"],
    "id": ["", "0", "01", "-1", "+1", "1" * 19, "1.5", "1e3", "0x1"],
    "actor": ["", "human", "human:", "root:x", "Human:x", "human:a b", "human:" + "x" * 65],
    "kind_env": ["", "a", "1A", "_A", "A" * 65, "A-B", "A B"],
}


@pytest.mark.parametrize("field", sorted(VALID))
def test_documented_valid_values_pass(field: str) -> None:
    for text in VALID[field]:
        assert grammar.matches(field, text), (field, text)


@pytest.mark.parametrize("field", sorted(INVALID))
def test_documented_invalid_values_fail(field: str) -> None:
    for text in INVALID[field]:
        assert not grammar.matches(field, text), (field, text)


@pytest.mark.parametrize("field", sorted(VALID))
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", " ", "\x00", "\u2028", "\x85"])
def test_trailing_whitespace_and_control_is_rejected_by_every_grammar(
    field: str, suffix: str
) -> None:
    for text in VALID[field]:
        assert not grammar.matches(field, text + suffix), (field, text, suffix)
        assert not grammar.matches(field, suffix + text), (field, text, suffix)


@pytest.mark.parametrize("field", sorted(VALID))
def test_non_ascii_digits_are_rejected_in_place_of_ascii(field: str) -> None:
    for text in VALID[field]:
        for i, ch in enumerate(text):
            if ch.isascii() and ch.isdigit():
                for digit in NON_ASCII_DIGITS:
                    assert not grammar.matches(field, text[:i] + digit + text[i + 1 :])


def test_unknown_grammar_name_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        grammar.matches("nope", "x")


def test_non_string_is_not_a_match() -> None:
    assert not grammar.matches("topic", b"abc")  # type: ignore[arg-type]
    assert not grammar.matches("topic", None)  # type: ignore[arg-type]


# ---------- repo id cap on add ----------


def test_repo_cap_for_add_is_255_characters() -> None:
    host = "example.com/"
    ok = host + "a" * (255 - len(host))
    assert len(ok) == 255
    assert grammar.matches("repo", ok)
    assert grammar.is_add_repo_id(ok)
    too_long = ok + "a"
    assert grammar.matches("repo", too_long)  # the grammar itself has no cap
    assert not grammar.is_add_repo_id(too_long)
    assert not grammar.is_add_repo_id("A/b")


# ---------- labels ----------


def test_label_splits_at_the_first_equals() -> None:
    assert grammar.parse_label("k=v") == ("k", "v")
    assert grammar.parse_label("k=") == ("k", "")
    assert grammar.parse_label("k=a=b=c") == ("k", "a=b=c")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "k",
        "=v",
        "K=v",
        "k=v\n",
        "k\n=v",
        "k=v w",
        "k=é",
        "a" * 65 + "=v",
        "k=" + "v" * 257,
        "\nk=v",
    ],
)
def test_bad_labels_raise_without_echoing_the_value(text: str) -> None:
    with pytest.raises(GrammarError) as info:
        grammar.parse_label(text)
    assert info.value.field == "label"
    assert str(info.value) == "invalid label"


# ---------- durations ----------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("0", 0),
        ("0.0s", 0),
        ("1", 1),
        ("60", 60),
        ("59.5s", 60),
        ("59.000000001s", 60),
        ("59.0s", 59),
        ("1s", 1),
        ("1m", 60),
        ("1h", 3600),
        ("1d", 86400),
        ("30d", 2592000),
        ("0.5m", 30),
        ("0.5h", 1800),
        ("1.5d", 129600),
        ("0.000000001d", 1),  # 86400e-9 s rounds up
        ("0.000000001s", 1),
        ("0.9", 1),
        ("0000000001", 1),
        ("9999999999", 9999999999),
        ("9999999999d", 9999999999 * 86400),
        ("9999999999.999999999d", 9999999999 * 86400 + 86400),
    ],
)
def test_duration_values_use_integer_arithmetic_and_round_up(text: str, seconds: int) -> None:
    assert grammar.parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "5\n",
        "\n5",
        " 5",
        "5 ",
        "5 s",
        "59.5s\n",
        "12345678901",  # eleven digits
        "12345678901s",
        "99999999999999999999999999999999",
        "0.",
        ".5",
        "5.",
        "5..5",
        "1.1234567891",  # ten fraction digits
        "1e3",
        "1E3",
        "+5",
        "-5",
        "1_000",
        "5ms",
        "5S",
        "5D",
        "5w",
        "5y",
        "s",
        "m",
        "5ss",
        "5.s",
        "nan",
        "inf",
        "none",
        "0x10",
        "٥",
        "５",
        "5٥",
        "1.٥",
        "5\u200b",
        "5\x00",
        "𝟓s",
    ],
)
def test_bad_durations_are_refused_with_the_field_and_without_the_value(text: str) -> None:
    with pytest.raises(GrammarError) as info:
        grammar.parse_duration(text)
    assert info.value.field == "expires"
    assert repr(text) not in str(info.value)
    assert str(info.value) == "invalid expires"


def test_duration_error_names_the_field_the_caller_gives() -> None:
    with pytest.raises(GrammarError) as info:
        grammar.parse_duration("x", field="retention.max_age")
    assert info.value.field == "retention.max_age"


def test_duration_result_is_always_an_int() -> None:
    for text in ["1", "1.5s", "1d", "0.5m"]:
        assert type(grammar.parse_duration(text)) is int


def test_duration_non_string_is_refused() -> None:
    for bad in (5, 5.5, None, b"5"):
        with pytest.raises(GrammarError):
            grammar.parse_duration(bad)  # type: ignore[arg-type]


# ---------- hostile size and linear time ----------

HUGE = 2_000_000
LIMIT_SECONDS = 3.0  # a quadratic scan of 2 MB would take far longer


def _timed(fn: Callable[..., object], *args: object) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


@pytest.mark.parametrize("field", sorted(VALID))
def test_huge_inputs_are_rejected_in_linear_time(field: str) -> None:
    bases = ["a", "a.", "a/", "a-", "a/a.", "a:1/", "/.", "A", "é", "a=", "human:a", "a\n"]
    for unit in bases:
        text = (unit * (HUGE // len(unit)))[:HUGE]
        for variant in (text, text + "!", text + "\n", "!" + text):
            assert _timed(grammar.matches, field, variant) < LIMIT_SECONDS, (field, unit)


@pytest.mark.parametrize("unit", ["1", "1.", "1.1", "0", "٥", "1s", ".", "s"])
def test_huge_duration_inputs_are_rejected_in_linear_time(unit: str) -> None:
    text = (unit * (HUGE // len(unit)))[:HUGE]
    for variant in (text, text + "!", text + "\n"):
        start = time.perf_counter()
        with pytest.raises(GrammarError):
            grammar.parse_duration(variant)
        assert time.perf_counter() - start < LIMIT_SECONDS


def test_huge_label_is_rejected_in_linear_time() -> None:
    for text in ("k=" + "v" * HUGE, "=" * HUGE, "k" * HUGE + "=v", "k" + "=" * HUGE):
        start = time.perf_counter()
        with pytest.raises(GrammarError):
            grammar.parse_label(text)
        assert time.perf_counter() - start < LIMIT_SECONDS
