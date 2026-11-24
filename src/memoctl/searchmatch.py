"""The search text and the substring match of ``memoctl search`` (interface.md, ``search``).

Pure functions over text: no I/O, no SQL, no regular expression built from input. The search
text is folded (NFKC, then ``str.casefold``) and split with ``str.split()`` into 1 to 8 distinct
terms of at most 64 code points; a note matches when every term occurs, as a code-point
substring, in its folded topic or in its folded body (each term in either; the two are never
joined, so a term cannot match across the boundary). Both sides use the same :func:`fold`, so a
stored text and a term are compared in one normal form whatever the writer used.

Bounded work, because a Python signal handler (the deadline) cannot run during one long C call:

- ``str.__contains__`` is at worst linear times the term length (at most 64) per body, and a body
  is at most 64 KiB (the row checks), so one note costs well under a millisecond;
- NFKC is quadratic in a long run of combining marks (3 s for one 64 KiB body of marks measured),
  and only a database written by something else can hold one (Body rules item 4 refuses a run
  over 30 on ``add``). :meth:`Matcher.matches` therefore checks a non-ASCII text for such a run
  first (one cheap pass over its distinct characters, an exact linear check only when a run of
  8 marks or more is there) and never normalizes a text that fails: the note does not match and
  is counted in :attr:`Matcher.skipped`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from functools import lru_cache

from memoctl.body import has_long_combining_run

__all__ = ["MAX_TERMS", "MAX_TERM_CHARS", "TermError", "Matcher", "fold", "parse_terms"]

MAX_TERMS = 8
MAX_TERM_CHARS = 64
_MARK_RUN = 8  # a run of this many marks starts the exact check


class TermError(ValueError):
    """The search text has no term, too many terms or a term that is too long. Carries no text."""

    def __init__(self) -> None:
        super().__init__("invalid search terms")


def fold(text: str) -> str:
    """NFKC, then ``str.casefold``. ASCII is already NFKC, and its casefold is its lower case."""
    if text.isascii():
        return text.lower()
    return unicodedata.normalize("NFKC", text).casefold()


def parse_terms(text: str) -> tuple[str, ...]:
    """The distinct terms of ``text`` in order of first appearance. Raises :class:`TermError`
    for none, more than :data:`MAX_TERMS` (counted after repeated terms collapse) or a term of
    more than :data:`MAX_TERM_CHARS` code points. ``text`` has passed Body rules items 2 to 4."""
    terms = tuple(dict.fromkeys(fold(text).split()))
    if not 1 <= len(terms) <= MAX_TERMS or any(len(t) > MAX_TERM_CHARS for t in terms):
        raise TermError
    return terms


@lru_cache(maxsize=65536)
def _starts_with_mark(char: str) -> bool:
    """Whether ``char`` decomposes (NFKD) to combining marks only, so it can extend a run."""
    parts = unicodedata.normalize("NFKD", char)
    return bool(parts) and all(unicodedata.combining(p) for p in parts)


def _may_blow_up(text: str) -> bool:
    """Whether normalizing ``text`` could be slow: it holds a run over the Body rules item 4
    limit. Non-ASCII only (ASCII holds no marks and is never normalized)."""
    marks = {c for c in set(text) if c >= "\x80" and _starts_with_mark(c)}
    if not marks:
        return False
    # Every mark becomes NUL; a NUL run is found by a C substring search. A NUL in the text
    # itself can only make this say "maybe", and the exact check below decides.
    marked = text.translate(dict.fromkeys(map(ord, marks), 0))
    if "\x00" * _MARK_RUN not in marked:
        return False
    return has_long_combining_run(text)


class Matcher:
    """``matches(topic, body)`` for a fixed term tuple. Keeps no text between calls."""

    def __init__(self, terms: Sequence[str]) -> None:
        self.terms = tuple(terms)
        self.skipped = 0  # notes not tested because a text held a combining run over the limit

    def matches(self, topic: str, body: str) -> bool:
        # The topic is ASCII (the row checks apply its grammar), so only the body can blow up.
        if not body.isascii() and _may_blow_up(body):
            self.skipped += 1
            return False
        folded_body = fold(body)
        return all(t in topic or t in folded_body for t in self.terms)
