"""A note's lifetime (interface.md, Lifetime): integer seconds only, no float anywhere.

``now`` is the one clock reading of a command (``add`` step 7), floored to whole seconds, and
is passed in as a parameter: nothing here reads a clock. Every duration is whole seconds, so
the cap, the minimum and the supersede comparison are exact integer comparisons.

The interface has no "never expires": every note gets an ``expires``, at most the effective
``retention.max_age`` (365 days at most) after ``now``, or a superseded note's later expiry.

Bounds: ``expires`` is printed as ``YYYY-MM-DDTHH:MM:SSZ``, so it must stay at or below the
last second of year 9999 (:data:`MAX_EPOCH`). A clock before the epoch or an expiry past that
second is refused (:class:`ClockError`) instead of being stored. Python integers do not
overflow; the bound exists so the stored value can be formatted and compared.

TODO(#16): ``add`` calls these at steps 1, 7 and 11. TODO(#6): map GrammarError and ClockError
to the exit 2 usage envelope (``expires``) and a fixed clock error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memoctl.grammar import GrammarError, parse_duration

__all__ = [
    "MAX_EPOCH",
    "MIN_EXPIRES",
    "ClockError",
    "Lifetime",
    "compute_lifetime",
    "parse_expires",
    "seconds_since_epoch",
]

MIN_EXPIRES = 60
# 9999-12-31T23:59:59Z, the last second the timestamp format can print.
MAX_EPOCH = 253402300799

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SECOND = timedelta(seconds=1)


class ClockError(ValueError):
    """The clock or an expiry is outside 1970-01-01 to 9999-12-31. The message is fixed text."""

    def __init__(self) -> None:
        super().__init__("clock out of range")

    def __reduce__(self) -> tuple[type[ClockError], tuple[()]]:
        return (ClockError, ())


@dataclass(frozen=True)
class Lifetime:
    """``expires`` in epoch seconds; ``capped`` and ``extended`` drive the warnings."""

    expires: int
    capped: bool
    extended: bool


def seconds_since_epoch(moment: datetime) -> int:
    """The gateway clock reading as whole epoch seconds, floored, by timedelta division.

    Raises :class:`ClockError` for a naive datetime, a non-datetime or a time before 1970.
    """
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ClockError
    seconds = (moment.astimezone(UTC) - _EPOCH) // _SECOND
    if seconds < 0:
        raise ClockError
    return seconds


def parse_expires(text: str) -> int:
    """``--expires`` as whole seconds (rounded up), at least :data:`MIN_EXPIRES`.

    Raises :class:`GrammarError` (field ``expires``, never the value) for a bad grammar or a
    rounded value under the minimum.
    """
    seconds = parse_duration(text, "expires")
    if seconds < MIN_EXPIRES:
        raise GrammarError("expires")
    return seconds


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("lifetime inputs are integers")
    return value


def _in_range(value: int) -> int:
    if not 0 <= value <= MAX_EPOCH:
        raise ClockError
    return value


def compute_lifetime(
    now: int,
    max_age: int,
    requested: int | None = None,
    target_expires: int | None = None,
) -> Lifetime:
    """The note's ``expires`` and which warnings apply.

    ``max_age`` is the effective ``retention.max_age`` in seconds, ``requested`` the parsed
    ``--expires`` (seconds, at least 60) and ``target_expires`` the ``--supersedes`` target's
    stored ``expires`` (omit it for a dry run). A request above ``max_age`` is cut to it
    (``capped``; equal is not capped). The result is the later of its own expiry and the
    target's (``extended`` only when the target's is strictly later).
    """
    now = _in_range(_integer(now))
    max_age = _integer(max_age)
    if max_age < 1:
        raise ValueError("max_age must be positive")
    capped = False
    span = max_age
    if requested is not None:
        requested = _integer(requested)
        if requested < MIN_EXPIRES:
            raise GrammarError("expires")
        if requested > max_age:
            capped = True
        else:
            span = requested
    expires = _in_range(now + span)
    extended = False
    if target_expires is not None:
        target = _in_range(_integer(target_expires))
        if target > expires:
            expires = target
            extended = True
    return Lifetime(expires, capped, extended)
