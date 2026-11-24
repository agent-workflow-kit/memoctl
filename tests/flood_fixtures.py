"""Flood and performance fixtures for ``context`` (deterministic, bulk-built, small on disk).

Raw writers: they bypass every write limit on purpose, because the point is what the read side
does with stores that ``add`` would refuse (a foreign or hostile database, or one near the
maximum configuration). Everything is seeded (``random.Random(seed)``, no clock, no environment)
so a failure reproduces; rows go in with ``executemany`` inside one transaction.

Bodies (``body_*``) are the pathological texts the tests use; ``Note`` is one row to insert.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

REPO = "example.com/a/b"
CREATED = 1_700_000_000
EXPIRES = 4_000_000_000
SHA = "a" * 64

# Marks of mixed combining classes (ccc 230, 220, 232, 202, ...): the worst case for NFKC's
# canonical reordering.
_MARKS = "̧̨̖̣̱́̀̂̈̊" * 3


@dataclass(frozen=True, slots=True)
class Note:
    topic: str = "t"
    kind: str = "implement"
    body: str = "hello"
    repo: str = REPO
    labels: tuple[tuple[str, str], ...] = field(default=())
    created: int = CREATED
    expires: int = EXPIRES
    superseded_by: int | None = None


def insert(conn: sqlite3.Connection, notes: Iterable[Note]) -> list[int]:
    """Insert ``notes`` (ids ascending in the given order) in one transaction; return the ids."""
    ids: list[int] = []
    label_rows: list[tuple[int, str, str]] = []
    conn.execute("BEGIN")
    try:
        for n in notes:
            raw = n.body.encode("utf-8")
            cur = conn.execute(
                "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created,"
                " expires, superseded_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (n.repo, n.topic, n.kind, n.body, SHA, len(raw), n.created, n.expires,
                 n.superseded_by),
            )  # fmt: skip
            nid = cur.lastrowid
            assert nid is not None
            ids.append(nid)
            label_rows.extend((nid, k, v) for k, v in n.labels)
        conn.executemany("INSERT INTO note_labels VALUES (?,?,?)", label_rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ids


# --- bodies -------------------------------------------------------------------------------------


def body_ascii(size: int) -> str:
    """ASCII words, LF-separated lines, exactly ``size`` bytes."""
    line = "word " * 15 + "end\n"
    return (line * (size // len(line) + 1))[:size]


def body_mark_runs(max_bytes: int, run: int = 30) -> str:
    """``e`` plus ``run`` mixed-class combining marks, repeated, at most ``max_bytes`` UTF-8."""
    unit = "e" + _MARKS[:run]
    unit_bytes = len(unit.encode())
    return unit * (max_bytes // unit_bytes)


def body_single_mark_run(marks: int) -> str:
    """One ``a`` followed by ``marks`` mixed-class combining marks (over 30: a foreign row)."""
    return "a" + (_MARKS * (marks // len(_MARKS) + 1))[:marks]


def body_expanding(char: str, max_bytes: int) -> str:
    """``char`` repeated to at most ``max_bytes`` UTF-8 bytes."""
    return char * (max_bytes // len(char.encode()))


# One code point and what it does to the text under NFKC (the tests do not depend on the
# numbers; they are for the reader).
EXPANDING = {
    "u+fdfa": "ﷺ",  # 18 characters (3 bytes in, 33 code points out)
    "ligature-ffi": "ﬃ",  # 3 characters
    "squared-hz": "㏿",  # a CJK square: 'gal'
    "roman-12": "Ⅻ",  # XII: 3 characters
    "paren-digit": "⑴",  # (1): 3 characters
    "astral-math": "\U0001d7ce",  # a digit: one astral code point (2 UTF-16 units, 4 bytes)
    "astral-emoji": "\U0001f600",  # 2 UTF-16 units, 4 bytes, 1 character
}

BIDI_ZERO_WIDTH = "a‮b​c⁦d‍e﻿" * 40  # all refused by body rules; foreign rows only


def body_mixed(rng: random.Random, max_bytes: int) -> str:
    """A seeded mix of ASCII, CJK, astral characters, ligatures, marks and LF."""
    pieces = ["abc ", "中文", "\U0001f600", "ﬁ", "é", "\n", "x" * 20, "ﷺ"]
    out: list[str] = []
    size = 0
    while size < max_bytes - 8:
        p = rng.choice(pieces)
        out.append(p)
        size += len(p.encode())
    return "".join(out)


# --- store shapes -------------------------------------------------------------------------------


def flood_notes(
    *, real_kind: str, real: int, real_topics: int, flood: list[tuple[str, int, int]]
) -> tuple[list[Note], list[Note]]:
    """``real`` older notes of ``real_kind`` in ``real_topics`` topics, then the flood (newer).

    ``flood`` is a list of ``(kind, notes, topics)``. Returns ``(real_notes, flood_notes)``; the
    caller inserts them in that order so the flood has the newer ids.
    """
    real_list = [
        Note(topic=f"real-{i % real_topics}", kind=real_kind, body=f"real note {i}")
        for i in range(real)
    ]
    flood_list: list[Note] = []
    for kind, count, topics in flood:
        flood_list.extend(
            Note(topic=f"bogus-{kind}-{i % topics}", kind=kind, body=f"bogus {kind} {i}")
            for i in range(count)
        )
    return real_list, flood_list
