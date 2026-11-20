"""Every metadata grammar and the duration parser (interface.md, Identifiers and grammars).

Pure string logic: no I/O, so nothing here goes through the gateway.

Rules every function follows:

- A pattern is applied with ``re.fullmatch``, never ``match`` with ``$`` (``$`` accepts a
  trailing newline).
- Character classes are explicit ASCII ranges. ``\\d`` and ``\\w`` are never used, so
  non-ASCII digits and letters cannot match, and no flag (``IGNORECASE``, ``UNICODE``) widens
  a class.
- No pattern has nested or overlapping quantifiers, so a match attempt is linear in the input.
- A failure names the field, never the value (:class:`GrammarError`).

Callers: ``cli`` (usage errors, exit 2 ``usage`` with ``details.field``), ``config``
(``config_invalid``), ``notes`` and the row checks of the store. Range checks that follow a
successful parse (``--expires`` at least 60 seconds, the ``retention.max_age`` cap) belong to
the caller and apply to the value :func:`parse_duration` returns.
"""

from __future__ import annotations

import re

__all__ = [
    "ADD_MAX_REPO_ID_LENGTH",
    "GRAMMARS",
    "GrammarError",
    "is_add_repo_id",
    "matches",
    "parse_duration",
    "parse_label",
]

# Each entry is the interface's grammar for one field. The repo pattern is claude-session's
# with ``\Z`` in the lookahead (same strings accepted under fullmatch).
GRAMMARS: dict[str, re.Pattern[str]] = {
    "repo": re.compile(
        r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/(?!\.\.?(/|\Z))[A-Za-z0-9._-]+)+"
    ),
    "topic": re.compile(r"[a-z0-9][a-z0-9._-]{0,63}"),
    "label_key": re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}"),
    "label_value": re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}"),
    "kind": re.compile(r"[a-z0-9][a-z0-9_-]{0,63}"),
    "id": re.compile(r"[1-9][0-9]{0,17}"),
    "actor": re.compile(r"(human|auto):[A-Za-z0-9._@/-]{1,64}"),
    "kind_env": re.compile(r"[A-Z][A-Z0-9_]{0,63}"),
}

# The cap `add` applies on top of the repo grammar (exit 2, field ``repo``).
ADD_MAX_REPO_ID_LENGTH = 255

_DURATION = re.compile(r"([0-9]{1,10})(?:\.([0-9]{1,9}))?([smhd])?")
_UNIT_SECONDS = {None: 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class GrammarError(ValueError):
    """A value failed its grammar. ``field`` is the usage field (``expires``, ``label``, ...).

    The message is fixed text built from the field alone: it never contains the input.
    """

    def __init__(self, field: str) -> None:
        super().__init__(f"invalid {field}")
        self.field = field


def matches(field: str, text: str) -> bool:
    """True when ``text`` is wholly a valid ``field`` (a key of :data:`GRAMMARS`).

    A non-string is not a match. An unknown ``field`` raises ``KeyError`` (a programming error).
    """
    pattern = GRAMMARS[field]
    return isinstance(text, str) and pattern.fullmatch(text) is not None


def is_add_repo_id(text: str) -> bool:
    """The repo id grammar plus the 255-character cap that only ``add`` applies."""
    return matches("repo", text) and len(text) <= ADD_MAX_REPO_ID_LENGTH


def parse_label(text: str) -> tuple[str, str]:
    """Split ``key=value`` at the first ``=`` and check both halves.

    Raises :class:`GrammarError` (field ``label``) for a missing ``=`` or a bad key or value.
    """
    if not isinstance(text, str):
        raise GrammarError("label")
    key, sep, value = text.partition("=")
    if not sep or not matches("label_key", key) or not matches("label_value", value):
        raise GrammarError("label")
    return key, value


def parse_duration(text: str, field: str = "expires") -> int:
    """Parse ``<digits>[.<digits>][s|m|h|d]`` into whole seconds, rounding up.

    Grammar: 1 to 10 ASCII digits, an optional fraction of 1 to 9 digits, an optional unit
    (none means seconds). Integer arithmetic only: the whole part times the unit, plus the
    fraction times the unit rounded up, so ``59.5s`` is 60 and ``0.000000001d`` is 1. The
    result is at most 9999999999 * 86400 + 86400 and ``0`` is returned for ``0``: callers apply
    their own range checks to the rounded value.

    Raises :class:`GrammarError` naming ``field`` (never the value) for anything else: empty,
    whitespace, a trailing newline, a sign, an exponent, ``ms``, non-ASCII digits, eleven
    integer digits or ten fraction digits.
    """
    if not isinstance(text, str):
        raise GrammarError(field)
    found = _DURATION.fullmatch(text)
    if found is None:
        raise GrammarError(field)
    whole, fraction, unit = found.groups()
    scale = _UNIT_SECONDS[unit]
    seconds = int(whole) * scale
    if fraction:
        denominator = 10 ** len(fraction)
        seconds += -(-int(fraction) * scale // denominator)  # ceiling division
    return seconds
