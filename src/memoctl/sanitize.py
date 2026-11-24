"""The read-time sanitizer (docs/interface.md, Sanitizer).

``sanitize(text)`` is a pure function: no I/O, no clock, no environment, no import from the
gateway. ``context`` applies it to every text that came from a note, and nothing else does
(``list``, ``show`` and ``search`` show the stored text).

The contract, checked by the differential test against claude-session's pinned sanitizer
(tests/test_sanitizer_differential.py): for a text a note can hold (no character that body rules
item 3 refuses, no run of more than 30 combining marks) the output is the NFKC form of the text
with tab turned into four spaces and every ``<`` written as ``&lt;``. For any other text
(only possible in a database written by something else) the output is the same after removing
the refused characters.

``context`` applies it to every note text through ``selection.select_context``; the rendered
block is checked to be unchanged by it (``memoctl.context_render``).

Two properties go beyond the three documented steps, because the interface promises that the
output is unchanged by a later sanitizer and that its cost is linear:

* Idempotence. Removing a character can expose a composition (``e``, a zero-width space and a
  combining acute become ``e`` and the acute, which NFKC composes), so NFKC and removal repeat
  until the text stops changing. Every repeat after the first removes at least one character,
  so the loop ends. The ``<`` escape runs once, last; the escaped text is a fixed point too.
* Cost. NFKC is quadratic in a run of combining marks of mixed classes (65,536 of them take
  seconds). A stored note holds at most 30 in a row, so only foreign rows are affected: a code
  point that would extend a run past ``MAX_MARK_RUN`` is dropped before NFKC. The run is
  counted in the NFKD form, as body rules item 4 counts it.
"""

from __future__ import annotations

import unicodedata

MAX_MARK_RUN = 30

# Default-ignorable code points outside category Cf that body rules item 3 refuses.
_INVISIBLE_NON_CF = frozenset(
    {0x34F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x180E))
    | {0x180F}
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)

_CATEGORY_CLASS = {
    "Cc": "control",
    "Zl": "control",
    "Zp": "control",
    "Cf": "invisible",
    "Co": "private_use",
    "Cn": "unassigned",
    "Cs": "surrogate",
}


def refused_class(c: str) -> str | None:
    """The body-rules class of a code point the sanitizer removes, else ``None``.

    ``control``, ``invisible``, ``private_use`` and ``unassigned`` are the classes of body
    rules item 3. ``surrogate`` (an unpaired surrogate) cannot be stored, since strict UTF-8
    refuses it, and is removed here so the output is always encodable. LF and tab are not
    refused (the sanitizer rewrites the tab). Unassigned is judged by the running
    interpreter's ``unicodedata``.
    """
    if c == "\n" or c == "\t":
        return None
    cls = _CATEGORY_CLASS.get(unicodedata.category(c))
    if cls is not None:
        return cls
    if ord(c) in _INVISIBLE_NON_CF:
        return "invisible"
    return None


_CACHE_LIMIT = 1 << 16


class _Table(dict[int, str | None]):
    """A ``str.translate`` table that decides each distinct code point once.

    ``str.translate`` runs in C; Python is called only for a code point not seen before, so
    there is no per-character Python loop over the text. The cache stops growing at
    ``_CACHE_LIMIT`` entries, so an input of every code point cannot grow it without bound.
    """

    def __missing__(self, key: int) -> str | None:
        c = chr(key)
        if c == "\t":
            value: str | None = "    "
        elif refused_class(c) is not None:
            value = None
        else:
            value = c
        if len(self) < _CACHE_LIMIT:
            self[key] = value
        return value


_TABLE = _Table()


def _strip(text: str) -> str:
    return text.translate(_TABLE)


# (leading marks, True when every code point is a mark, trailing marks) of a code point's NFKD.
_MarkInfo = tuple[int, bool, int]
_MARK_CACHE: dict[str, _MarkInfo] = {}


def _mark_info(c: str) -> _MarkInfo:
    info = _MARK_CACHE.get(c)
    if info is None:
        d = unicodedata.normalize("NFKD", c)
        flags = [unicodedata.combining(x) != 0 for x in d]
        lead = 0
        while lead < len(flags) and flags[lead]:
            lead += 1
        trail = 0
        while trail < len(flags) and flags[len(flags) - 1 - trail]:
            trail += 1
        info = (lead, lead == len(flags), trail)
        if len(_MARK_CACHE) < _CACHE_LIMIT:
            _MARK_CACHE[c] = info
    return info


class _MarkTable(dict[int, str]):
    """A ``str.translate`` table that weighs each code point for the mark-run check.

    A code point maps to one ``"M"`` per combining mark it can add to a run (leading plus
    trailing marks of its NFKD form) and to a space when it can neither extend nor start a run.
    Like ``_Table`` it decides each distinct code point once, only when first seen (nothing is
    built at import), and stops growing at ``_CACHE_LIMIT`` entries.
    """

    def __missing__(self, key: int) -> str:
        lead, _all_marks, trail = _mark_info(chr(key))
        value = "M" * (lead + trail) if lead + trail else " "
        if len(self) < _CACHE_LIMIT:
            self[key] = value
        return value


_MARKS = _MarkTable()
_RUN_PROBE = "M" * (MAX_MARK_RUN + 1)


def _drop_mark_runs(text: str) -> str:
    """The per-code-point loop: drop each code point that would extend a run past the limit."""
    run = 0
    out: list[str] = []
    changed = False
    for c in text:
        if c < "\u0300" and c.isascii():
            run = 0
            out.append(c)
            continue
        lead, all_marks, trail = _mark_info(c)
        if run + lead > MAX_MARK_RUN:
            changed = True
            continue
        run = run + lead if all_marks else trail
        out.append(c)
    return "".join(out) if changed else text


def _limit_mark_runs(text: str) -> str:
    """Drop each code point that would make a mark run longer than ``MAX_MARK_RUN``."""
    if text.isascii():
        return text
    # Screen 1: a text with no distinct code point that can touch a run (CJK, Cyrillic, ...) is
    # cleared by the set of its code points alone.
    if not any(_MARKS[ord(c)] != " " for c in set(text)):
        return text
    # Screen 2 (str.translate, C speed): each code point becomes as many "M" as marks it can add
    # to a run, or a space. A run of more than MAX_MARK_RUN marks needs more than that many "M"
    # in a row, so without them nothing is dropped. The Python loop runs only when they are there.
    if _RUN_PROBE not in text.translate(_MARKS):
        return text
    return _drop_mark_runs(text)


def sanitize(text: str) -> str:
    """Return ``text`` made safe to print in a context block. Pure, idempotent, linear.

    In order: NFKC, remove every character refused by body rules item 3 (and every other
    control character except LF, and unpaired surrogates), tab to four spaces, and last every
    ``<`` to ``&lt;``. See the module docstring for the repeat and the run limit.
    """
    if type(text) is not str:
        raise TypeError(f"sanitize needs a str, got {type(text).__name__}")
    cur = text
    while True:
        nxt = _strip(unicodedata.normalize("NFKC", _limit_mark_runs(cur)))
        if nxt == cur:
            break
        cur = nxt
    return cur.replace("<", "&lt;")
