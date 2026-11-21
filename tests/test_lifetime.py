"""Lifetime: integer arithmetic, caps, supersede inheritance, clock and overflow edges."""

from __future__ import annotations

import ast
import inspect
import random
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import IntEnum
from fractions import Fraction
from math import ceil

import pytest

from memoctl import lifetime
from memoctl.grammar import GrammarError, parse_duration
from memoctl.lifetime import (
    MAX_EPOCH,
    MIN_EXPIRES,
    ClockError,
    compute_lifetime,
    format_timestamp,
    parse_expires,
    seconds_since_epoch,
)

DAY = 86400
MAX_AGE = 30 * DAY


# ---------- the clock reading ----------


def test_epoch_is_zero() -> None:
    assert seconds_since_epoch(datetime(1970, 1, 1, tzinfo=UTC)) == 0


def test_fraction_is_floored() -> None:
    assert seconds_since_epoch(datetime(1970, 1, 1, 0, 0, 1, 999999, tzinfo=UTC)) == 1


def test_other_timezone_is_converted() -> None:
    zone = timezone(timedelta(hours=5))
    assert seconds_since_epoch(datetime(1970, 1, 1, 5, tzinfo=zone)) == 0


def test_naive_clock_refused() -> None:
    with pytest.raises(ClockError):
        seconds_since_epoch(datetime(2026, 1, 1))


def test_before_epoch_refused() -> None:
    with pytest.raises(ClockError):
        seconds_since_epoch(datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC))


def test_year_9999_edge() -> None:
    assert seconds_since_epoch(datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)) == MAX_EPOCH


def test_offset_overflow_is_clock_error() -> None:
    late = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=5)))
    early = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    for moment in (late, early):
        with pytest.raises(ClockError):
            seconds_since_epoch(moment)


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


def test_tzinfo_without_offset_is_clock_error() -> None:
    with pytest.raises(ClockError):
        seconds_since_epoch(datetime(2026, 1, 1, tzinfo=_NoOffset()))


class _Count(int):
    pass


class _Kind(IntEnum):
    ONE = 1000


def test_int_subclasses_are_normalized() -> None:
    result = compute_lifetime(_Kind.ONE, _Count(MAX_AGE), requested=_Count(3600))
    assert type(result.expires) is int
    assert result.expires == 4600
    later = compute_lifetime(_Kind.ONE, MAX_AGE, requested=3600, target_expires=_Count(9000))
    assert type(later.expires) is int
    assert later.expires == 9000


@pytest.mark.parametrize("max_age", [0, -1])
def test_non_positive_max_age_is_value_error(max_age: int) -> None:
    with pytest.raises(ValueError):
        compute_lifetime(0, max_age)


def test_non_datetime_refused() -> None:
    with pytest.raises(ClockError):
        seconds_since_epoch(1_700_000_000)  # type: ignore[arg-type]


# ---------- --expires ----------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("60", 60),
        ("59.5s", 60),
        ("59.000000001s", 60),
        ("0.999m", 60),
        ("1m", 60),
        ("1h", 3600),
        ("0.000694445d", 61),
        ("2d", 2 * DAY),
    ],
)
def test_parse_expires_accepts(text: str, seconds: int) -> None:
    assert parse_expires(text) == seconds


@pytest.mark.parametrize("text", ["0", "0.0s", "59", "58.9s", "0.98m", "1s"])
def test_parse_expires_below_minimum(text: str) -> None:
    with pytest.raises(GrammarError) as info:
        parse_expires(text)
    assert info.value.field == "expires"
    assert text not in str(info.value)


@pytest.mark.parametrize("text", ["", " 60", "60\n", "-60", "1e3", "60ms", "١٢٠", "9" * 11])
def test_parse_expires_grammar(text: str) -> None:
    with pytest.raises(GrammarError) as info:
        parse_expires(text)
    assert info.value.field == "expires"


def test_parse_expires_largest() -> None:
    assert parse_expires("9999999999.999999999d") == 9999999999 * DAY + DAY


def test_minimum_constant() -> None:
    assert MIN_EXPIRES == 60


# ---------- compute_lifetime ----------


def test_default_is_max_age_without_warning() -> None:
    result = compute_lifetime(1000, MAX_AGE)
    assert (result.expires, result.capped, result.extended) == (1000 + MAX_AGE, False, False)


def test_shorter_request_used() -> None:
    result = compute_lifetime(1000, MAX_AGE, requested=3600)
    assert (result.expires, result.capped) == (4600, False)


def test_equal_to_cap_is_not_capped() -> None:
    result = compute_lifetime(0, MAX_AGE, requested=MAX_AGE)
    assert (result.expires, result.capped) == (MAX_AGE, False)


def test_one_over_cap_is_capped() -> None:
    result = compute_lifetime(0, MAX_AGE, requested=MAX_AGE + 1)
    assert (result.expires, result.capped) == (MAX_AGE, True)


def test_huge_request_is_capped_without_overflow() -> None:
    result = compute_lifetime(0, MAX_AGE, requested=9999999999 * DAY + DAY)
    assert (result.expires, result.capped) == (MAX_AGE, True)


def test_below_minimum_refused_here_too() -> None:
    with pytest.raises(GrammarError):
        compute_lifetime(0, MAX_AGE, requested=59)


def test_target_later_raises_expiry() -> None:
    result = compute_lifetime(1000, MAX_AGE, requested=3600, target_expires=9000)
    assert (result.expires, result.extended) == (9000, True)


def test_target_earlier_changes_nothing() -> None:
    result = compute_lifetime(1000, MAX_AGE, requested=3600, target_expires=2000)
    assert (result.expires, result.extended) == (4600, False)


def test_target_equal_is_not_an_extension() -> None:
    result = compute_lifetime(1000, MAX_AGE, requested=3600, target_expires=4600)
    assert (result.expires, result.extended) == (4600, False)


def test_target_one_later_is_extension() -> None:
    result = compute_lifetime(1000, MAX_AGE, requested=3600, target_expires=4601)
    assert (result.expires, result.extended) == (4601, True)


def test_target_beyond_cap_after_cap_lowered_and_clock_set_back() -> None:
    target = 100 + 365 * DAY
    result = compute_lifetime(100, 3600, target_expires=target)
    assert result.expires == target and result.extended


def test_capped_and_extended_both_reported() -> None:
    result = compute_lifetime(0, 3600, requested=7200, target_expires=10_000)
    assert (result.expires, result.capped, result.extended) == (10_000, True, True)


def test_replacement_never_before_target_property() -> None:
    rng = random.Random(14)
    for _ in range(2000):
        now = rng.randrange(0, MAX_EPOCH - 400 * DAY)
        max_age = rng.randrange(3600, 365 * DAY + 1)
        requested = rng.choice([None, rng.randrange(60, 2 * 365 * DAY)])
        target = rng.randrange(0, MAX_EPOCH)
        result = compute_lifetime(now, max_age, requested, target)
        assert result.expires >= target
        assert result.expires >= now + MIN_EXPIRES
        own = now + (max_age if requested is None else min(requested, max_age))
        assert result.expires == max(own, target)


def test_expiry_past_year_9999_refused() -> None:
    with pytest.raises(ClockError):
        compute_lifetime(MAX_EPOCH, MAX_AGE)
    with pytest.raises(ClockError):
        compute_lifetime(MAX_EPOCH - MAX_AGE + 1, MAX_AGE)


def test_expiry_exactly_at_limit_is_allowed() -> None:
    assert compute_lifetime(MAX_EPOCH - MAX_AGE, MAX_AGE).expires == MAX_EPOCH


@pytest.mark.parametrize("now", [-1, 2**63, MAX_EPOCH + 1, 2**64])
def test_now_out_of_range_refused(now: int) -> None:
    with pytest.raises(ClockError):
        compute_lifetime(now, MAX_AGE)


def test_epoch_now_is_fine() -> None:
    assert compute_lifetime(0, MAX_AGE).expires == MAX_AGE


@pytest.mark.parametrize("bad", [1.5, True, "1", None])
def test_non_integer_inputs_refused(bad: object) -> None:
    with pytest.raises(TypeError):
        compute_lifetime(bad, MAX_AGE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_lifetime(0, bad)  # type: ignore[arg-type]
    if bad is not None:
        with pytest.raises(TypeError):
            compute_lifetime(0, MAX_AGE, requested=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            compute_lifetime(0, MAX_AGE, target_expires=bad)  # type: ignore[arg-type]


def test_target_out_of_range_refused() -> None:
    with pytest.raises(ClockError):
        compute_lifetime(0, MAX_AGE, target_expires=-1)
    with pytest.raises(ClockError):
        compute_lifetime(0, MAX_AGE, target_expires=MAX_EPOCH + 1)


# ---------- exact arithmetic ----------


def test_parse_expires_matches_exact_fractions() -> None:
    rng = random.Random(1414)
    units = {"": 1, "s": 1, "m": 60, "h": 3600, "d": DAY}
    for _ in range(5000):
        whole = str(rng.randrange(0, 10 ** rng.randrange(1, 11)))
        text = whole
        if rng.random() < 0.7:
            digits = rng.randrange(1, 10)
            frac = "".join(rng.choice("0123456789") for _ in range(digits))
            if rng.random() < 0.3:
                frac = "0" * (digits - 1) + "1"
            text += "." + frac
        unit = rng.choice(list(units))
        text += unit
        exact = Fraction(text[: len(text) - len(unit)] or "0") * units[unit]
        expected = ceil(exact)
        if expected < MIN_EXPIRES:
            with pytest.raises(GrammarError):
                parse_expires(text)
        else:
            assert parse_expires(text) == expected == parse_duration(text)


def test_rounding_boundaries() -> None:
    assert parse_expires("59.999999999s") == 60
    assert parse_expires("60.000000001s") == 61
    assert parse_expires("1.000000001m") == 61  # 60.00000006 seconds rounds up


# ---------- no floats in the module ----------


def test_module_has_no_float_arithmetic() -> None:
    tree = ast.parse(inspect.getsource(lifetime))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float)
        if isinstance(node, ast.BinOp):
            assert not isinstance(node.op, ast.Div)
        if isinstance(node, ast.Name):
            assert node.id not in {"float", "round"}
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"timestamp", "total_seconds"}


@pytest.mark.parametrize(
    ("epoch", "text"),
    [
        (0, "1970-01-01T00:00:00Z"),
        (1_700_000_000, "2023-11-14T22:13:20Z"),
        (MAX_EPOCH, "9999-12-31T23:59:59Z"),
    ],
)
def test_format_timestamp(epoch: int, text: str) -> None:
    assert format_timestamp(epoch) == text


@pytest.mark.parametrize("bad", [-1, MAX_EPOCH + 1, True, 1.5, "1"])
def test_format_timestamp_refuses_out_of_range(bad: object) -> None:
    with pytest.raises(ClockError):
        format_timestamp(bad)  # type: ignore[arg-type]
