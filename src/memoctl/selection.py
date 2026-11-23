"""Context selection: the candidates, their order and the kind-share walk (interface.md,
Context selection).

A pure selection API for ``context``: it reads one snapshot of the store through
:class:`memoctl.store.Store` (this module never imports ``sqlite3``) and returns which notes to
print, with each note's rendered text. Printing the block, ``--fence`` and ``--json`` are #26.

How it works:

1. One read transaction (joined when the caller already opened one). One statement streams the
   repo's live notes of the allowed kinds, newest ``id`` first, through ``Store.notes`` (a fixed
   ``WHERE`` fragment, every value bound, the strict decoder and the row checks of the store;
   a failing row is skipped and counted in ``Store.bad_rows``). Each row is then filtered by
   the subset label rule and by "the sanitized body has a non-whitespace character", and
   reduced to a small :class:`Candidate` (its id, topic, kind and the size of its note text).
   Bodies are never kept, so memory is a few dozen bytes per candidate whatever the stored size.
2. ``eligible`` is the number of candidates, which fixes the reserved lines and so the budgets.
   The walk is a transcription of the interface's pseudocode over the candidate list
   (:func:`walk`), integer arithmetic only.
3. The chosen notes (at most ``max_notes``) are read again by id in the same snapshot and
   rendered.

Counting rule (what selection measures): the ``u16`` and ``b8`` of ``note_text`` (header, kept
body lines, cut line), computed from the sanitizer's output, never from NFKC of the stored text
(a foreign row's sanitized text can differ from NFKC of it). ``u16`` is UTF-16 code units and
``b8`` UTF-8 bytes. If #26 renders a note differently from :func:`note_text`, it must change
:func:`note_text`, the one place both read, so the sizes stay what is printed.

``max_kind_share`` is read as the decimal the config holds (the shortest ``repr`` of the float,
so ``0.3`` is exactly 3/10), never as a binary float product.

todo(#26): the sanitizer (#24) is passed in as ``sanitize``; #26 passes the real one and prints
the block from :class:`Selection`.
todo(#27): the CLI wraps this in its exit paths and deadline; the progress handler of the store
interrupts a long scan.
todo(#28): flood and performance fixtures (the scan is linear in the repo's live notes).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import lru_cache
from typing import Any, Final, NamedTuple

from memoctl.config import Context
from memoctl.store import NoteRow, Store, StoreError

__all__ = [
    "Candidate",
    "SelectedNote",
    "Selection",
    "SelectionConfig",
    "b8",
    "head_line",
    "note_text",
    "omit_line",
    "select_context",
    "u16",
    "walk",
]

MAX_KINDS: Final = 64  # config: author_kinds has at most 64 entries
_INT64_MAX = 2**63 - 1

# One fixed statement; every value is bound. The kind list is padded with NULL (which matches
# nothing) so the text stays a literal.
_KIND_SLOTS = "?, " * (MAX_KINDS - 1) + "?"
_CANDIDATE_WHERE = (
    "WHERE repo = ? AND superseded_by IS NULL AND expires > ? AND kind IN (" + _KIND_SLOTS + ")"
)


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    """The ``context`` settings selection reads (the effective config for the repo).

    The config model range-checks the real values; this only refuses a value the walk is not
    defined for (a non-integer, a negative count, a share outside 0 to 1) with ``ValueError``.
    """

    author_kinds: tuple[str, ...]
    max_chars: int
    max_bytes: int
    max_notes: int
    max_per_topic: int
    max_kind_share: Fraction
    note_max_chars: int

    def __post_init__(self) -> None:
        if not isinstance(self.author_kinds, tuple) or not all(
            type(k) is str for k in self.author_kinds
        ):
            raise ValueError("author_kinds must be a tuple of strings")
        counts = {
            "max_chars": self.max_chars,
            "max_bytes": self.max_bytes,
            "max_notes": self.max_notes,
            "max_per_topic": self.max_per_topic,
            "note_max_chars": self.note_max_chars,
        }
        for name, value in counts.items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        share = self.max_kind_share
        if not isinstance(share, Fraction) or not 0 <= share <= 1:
            raise ValueError("max_kind_share must be a Fraction from 0 to 1")

    @classmethod
    def from_config(cls, ctx: Context) -> SelectionConfig:
        return cls(
            author_kinds=tuple(ctx.author_kinds),
            max_chars=ctx.max_chars,
            max_bytes=ctx.max_bytes,
            max_notes=ctx.max_notes,
            max_per_topic=ctx.max_per_topic,
            max_kind_share=share_fraction(ctx.max_kind_share),
            note_max_chars=ctx.note_max_chars,
        )


def share_fraction(value: int | float) -> Fraction:
    """``max_kind_share`` as the exact decimal it was written as (no binary float error)."""
    if isinstance(value, bool):
        raise ValueError("max_kind_share must be a number")
    return Fraction(value) if isinstance(value, int) else Fraction(repr(value))


class Candidate(NamedTuple):
    """A note that passed every candidate rule, reduced to what the walk reads."""

    id: int
    topic: str
    kind: str
    c: int  # u16(note_text)
    b: int  # b8(note_text)


@dataclass(frozen=True, slots=True)
class SelectedNote:
    id: int
    topic: str
    kind: str
    text: str  # note_text: header, kept lines and the cut line, every line ending in LF
    u16: int
    b8: int
    cut: bool


@dataclass(frozen=True, slots=True)
class Selection:
    repo: str
    eligible: int
    notes: tuple[SelectedNote, ...]  # display order: id, highest first
    # Rows the store skipped (row checks) during this selection, among the rows the SQL repo,
    # kind and liveness filter yields: a row of a kind outside author_kinds is excluded before
    # the row checks and never counted.
    bad_rows: int

    @property
    def shown(self) -> int:
        return len(self.notes)

    @property
    def omitted(self) -> int:
        return self.eligible - len(self.notes)

    @property
    def head(self) -> str:
        return head_line(self.repo, self.shown, self.eligible)

    @property
    def omit(self) -> str:
        """The omitted-notes line ("" when nothing is omitted)."""
        return omit_line(self.repo, self.omitted) if self.omitted > 0 else ""


# --- counting ---------------------------------------------------------------------------------


def u16(text: str) -> int:
    """UTF-16 code units of ``text``."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le")) // 2


def b8(text: str) -> int:
    """UTF-8 bytes of ``text``."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-8"))


def head_line(repo: str, shown: int, eligible: int) -> str:
    return f"memoctl notes for {repo} ({shown} of {eligible} shown)\n"


def omit_line(repo: str, omitted: int) -> str:
    return f"[memoctl: {omitted} more notes not shown; use memoctl list --repo {repo}]\n"


# --- rendering one note -----------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _date(created: int) -> str:
    d = datetime.fromtimestamp(created, tz=UTC)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _header(row: NoteRow) -> str:
    labels = ", ".join(f"{k}={v}" for k, v in sorted(row.labels))
    tail = f" | {labels}" if labels else ""
    return (
        f"--- note {row.id} | topic: {row.topic} | kind: {row.kind}"
        f" | {_date(row.created)}{tail} ---\n"
    )


def _cp16(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def _kept_lines(sanitized: str, cap: int) -> tuple[list[str], bool]:
    """The body lines to print and whether the note was cut (interface: Rendering a note)."""
    pieces = sanitized.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    lines = [f"| {p}\n" if p else "|\n" for p in pieces]
    kept: list[str] = []
    total = 0
    for line in lines:
        n = u16(line)
        if total + n > cap:
            if not kept:  # the first line alone is over the cap: a prefix of it, whole code points
                first = line[:-1]  # without its LF
                acc = 0
                out: list[str] = []
                for ch in first:
                    w = _cp16(ch)
                    if acc + w + 1 > cap:
                        break
                    acc += w
                    out.append(ch)
                return ["".join(out) + "\n"], True
            return kept, True
        total += n
        kept.append(line)
    return kept, False


def note_text(row: NoteRow, sanitized: str, cap: int) -> tuple[str, bool]:
    """``(note_text, cut)`` for ``row`` whose sanitized body is ``sanitized``; ``cap`` is
    ``context.note_max_chars``."""
    kept, cut = _kept_lines(sanitized, cap)
    text = _header(row) + "".join(kept)
    if cut:
        text += f"[memoctl: note {row.id} cut]\n"
    return text, cut


# --- the walk -----------------------------------------------------------------------------------


def budgets(repo: str, eligible: int, cfg: SelectionConfig) -> tuple[int, int]:
    """``(Bc, Bb)``: the character and byte budgets after the two reserved lines."""
    head = head_line(repo, eligible, eligible)
    omit = omit_line(repo, eligible)
    return (
        cfg.max_chars - u16(head) - u16(omit),
        cfg.max_bytes - b8(head) - b8(omit),
    )


def walk(
    candidates: Iterable[Candidate], bc: int, bb: int, cfg: SelectionConfig
) -> list[Candidate]:
    """The interface's two-pass walk over ``candidates`` (already in order, newest id first).

    Returns the selection in display order (``id`` highest first).
    """
    share_c = (cfg.max_kind_share * cfg.max_chars).__floor__()
    share_n = max(1, (cfg.max_kind_share * cfg.max_notes).__floor__())
    used_c = used_b = 0
    topic_n: dict[str, int] = {}
    kind_c: dict[str, int] = {}
    kind_n: dict[str, int] = {}
    sel: list[Candidate] = []
    deferred: list[Candidate] = []

    def fits(n: Candidate) -> bool:
        return used_c + n.c <= bc and used_b + n.b <= bb

    def take(n: Candidate) -> None:
        nonlocal used_c, used_b
        sel.append(n)
        used_c += n.c
        used_b += n.b
        topic_n[n.topic] = topic_n.get(n.topic, 0) + 1
        kind_c[n.kind] = kind_c.get(n.kind, 0) + n.c
        kind_n[n.kind] = kind_n.get(n.kind, 0) + 1

    for n in candidates:  # pass 1
        if len(sel) == cfg.max_notes:
            break
        if topic_n.get(n.topic, 0) == cfg.max_per_topic:
            continue
        if not fits(n):
            continue  # never fits later either
        if kind_c.get(n.kind, 0) + n.c > share_c or kind_n.get(n.kind, 0) + 1 > share_n:
            deferred.append(n)
            continue
        take(n)
    for n in deferred:  # pass 2, same order, same counters
        if len(sel) == cfg.max_notes:
            break
        if topic_n.get(n.topic, 0) == cfg.max_per_topic:
            continue
        if fits(n):
            take(n)
    sel.sort(key=lambda n: n.id, reverse=True)
    return sel


# --- the store ----------------------------------------------------------------------------------


def _check_now(now: int) -> None:
    if type(now) is not int or not 0 <= now <= _INT64_MAX:
        raise ValueError("now must be a non-negative 64-bit integer")


def select_context(
    store: Store,
    *,
    repo: str,
    session_labels: Iterable[tuple[str, str]],
    config: SelectionConfig,
    now: int,
    sanitize: Callable[[str], str],
) -> Selection:
    """Select the notes ``context`` prints for ``repo``.

    ``now`` is the command's single clock read (UTC seconds). ``session_labels`` are the
    session's ``(key, value)`` pairs (a repeated pair counts once). ``sanitize`` is the
    interface's sanitizer. Raises ``StoreError`` for a database failure (a value over 1 MiB is
    ``db_corrupt``).
    """
    _check_now(now)
    if len(config.author_kinds) > MAX_KINDS:
        raise ValueError("too many author kinds")
    session = frozenset(session_labels)
    kinds: list[Any] = sorted(set(config.author_kinds))
    kinds += [None] * (MAX_KINDS - len(kinds))
    params = (repo, now, *kinds)
    if store.in_transaction:
        return _select(store, repo, session, config, params, sanitize)
    with store.read():
        return _select(store, repo, session, config, params, sanitize)


def _select(
    store: Store,
    repo: str,
    session: frozenset[tuple[str, str]],
    config: SelectionConfig,
    params: tuple[Any, ...],
    sanitize: Callable[[str], str],
) -> Selection:
    bad_before = store.bad_rows
    candidates: list[Candidate] = []
    for row in store.notes(_CANDIDATE_WHERE, params, labels=True, descending=True):
        if not all(pair in session for pair in row.labels):
            continue
        sanitized = sanitize(row.body)
        if not sanitized.strip():  # no non-whitespace character
            continue
        text, _ = note_text(row, sanitized, config.note_max_chars)
        candidates.append(Candidate(row.id, row.topic, row.kind, u16(text), b8(text)))
    eligible = len(candidates)
    bad_rows = store.bad_rows - bad_before
    if eligible == 0:
        return Selection(repo, 0, (), bad_rows)
    bc, bb = budgets(repo, eligible, config)
    chosen = walk(candidates, bc, bb, config)
    notes: list[SelectedNote] = []
    for cand in chosen:
        rows = list(store.notes("WHERE id = ?", (cand.id,), labels=True))
        if len(rows) != 1:  # only possible outside one snapshot
            raise StoreError("db_corrupt")
        row = rows[0]
        text, cut = note_text(row, sanitize(row.body), config.note_max_chars)
        if (u16(text), b8(text)) != (cand.c, cand.b):
            raise StoreError("db_corrupt")
        notes.append(SelectedNote(row.id, row.topic, row.kind, text, cand.c, cand.b, cut))
    return Selection(repo, eligible, tuple(notes), bad_rows)
