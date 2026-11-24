"""Read-side queries of ``list`` and ``show`` (interface.md, ``list``, ``show``, Database).

This module sits next to the store and never imports ``sqlite3``: every read goes through
``Store.execute`` (strict text decoding, deadline interrupts, translated failures) and every row
through ``check_note_row`` and ``check_label_row``, so a hostile row can never reach an output.

``scan`` is one pass over the notes that match the SQL filters (repo, topic, kind), newest id
first, one row at a time: memory is bounded by ``--limit`` (at most 500 summaries) whatever the
size of the store. The label filter is applied in Python on the checked labels, so the SQL never
grows with the number of ``--label`` flags. The whole pass runs in one read transaction (one
snapshot). A row that fails its checks is skipped and counted in ``Store.bad_rows``; it is in no
count of the output.

Every SQL fragment here is a literal; every value is a bound parameter.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any, LiteralString

from memoctl.deadline import current
from memoctl.gateway import Gateway
from memoctl.lifetime import format_timestamp
from memoctl.statedir import open_state_dir
from memoctl.store import (
    MAX_LABELS,
    NOTE_COLUMNS,
    Mode,
    NoteRow,
    Store,
    check_label_row,
    check_note_row,
    open_store,
)

__all__ = [
    "Filters",
    "ListResult",
    "fetch_note",
    "note_status",
    "open_read_store",
    "scan",
    "show_document",
    "summary",
]

LIVE, SUPERSEDED, EXPIRED = "live", "superseded", "expired"
_DESCENDING = (
    f"SELECT {NOTE_COLUMNS}, key, value FROM notes"
    " LEFT JOIN note_labels ON note_labels.note_id = notes.id"
)


@dataclass(frozen=True)
class Filters:
    """What ``list`` was asked for. Every text is already grammar-checked by the caller."""

    repo: str | None = None
    topic: str | None = None
    kind: str | None = None
    labels: frozenset[tuple[str, str]] = frozenset()
    include_all: bool = False
    limit: int = 50


@dataclass(frozen=True)
class ListResult:
    notes: list[dict[str, Any]]
    total: int
    hidden: int

    def document(self) -> dict[str, Any]:
        return {
            "notes": self.notes,
            "total": self.total,
            "shown": len(self.notes),
            "hidden": self.hidden,
        }


def open_read_store(gateway: Gateway) -> Store | None:
    """The store in ``READ`` mode with the deadline's hooks installed; ``None`` for an empty
    store (no state directory, no ``state.db`` or an uninitialized one). No state directory,
    ``state.db`` or row is created or modified (SQLite may add ``-wal``/``-shm`` sidecars on a
    read-only WAL open)."""
    state = open_state_dir(gateway, create=False)
    if state is None:
        return None
    with state:
        return open_store(state, Mode.READ, configure=current().configure_store)


def note_status(row: NoteRow, now: int) -> str:
    """Derived status: superseded when ``superseded_by`` is set, else expired when
    ``expires`` <= ``now``, else live."""
    if row.superseded_by is not None:
        return SUPERSEDED
    return EXPIRED if row.expires <= now else LIVE


def _label_texts(row: NoteRow) -> list[str]:
    return [f"{key}={value}" for key, value in row.labels]


def summary(row: NoteRow, status: str) -> dict[str, Any]:
    """One ``list`` entry (schemas/defs.json ``note_summary``), keys in schema order."""
    return {
        "id": row.id,
        "repo": row.repo,
        "topic": row.topic,
        "kind": row.kind,
        "labels": _label_texts(row),
        "created": format_timestamp(row.created),
        "expires": format_timestamp(row.expires),
        "status": status,
        "superseded_by": row.superseded_by,
        "size": row.size,
    }


def show_document(row: NoteRow, status: str, supersedes: int | None) -> dict[str, Any]:
    """The ``show`` document (schemas/cli/show.json), keys in schema order."""
    return {
        "id": row.id,
        "repo": row.repo,
        "topic": row.topic,
        "kind": row.kind,
        "labels": _label_texts(row),
        "body": row.body,
        "size": row.size,
        "created": format_timestamp(row.created),
        "expires": format_timestamp(row.expires),
        "status": status,
        "superseded_by": row.superseded_by,
        "body_sha256": row.body_sha256,
        "supersedes": supersedes,
    }


def _where(filters: Filters) -> tuple[LiteralString, list[str]]:
    clauses: list[LiteralString] = []
    params: list[str] = []
    if filters.repo is not None:
        clauses.append("notes.repo = ?")
        params.append(filters.repo)
    if filters.topic is not None:
        clauses.append("notes.topic = ?")
        params.append(filters.topic)
    if filters.kind is not None:
        clauses.append("notes.kind = ?")
        params.append(filters.kind)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _checked_notes(store: Store, filters: Filters) -> Iterator[NoteRow | None]:
    """Every note matching the SQL filters, newest first, with its checked labels, one at a
    time. ``None`` stands for a row that failed a check (the note row, a label row, or more than
    ``MAX_LABELS`` labels); it is counted in ``store.bad_rows``. The statement returns one line
    per label (``LEFT JOIN``, ordered by id descending then key and value), so a note is complete
    when the id changes."""
    where, params = _where(filters)
    cursor = store.execute(
        f"{_DESCENDING} {where} ORDER BY notes.id DESC, note_labels.key, note_labels.value",
        params,
    )
    try:
        row: NoteRow | None = None
        labels: list[tuple[str, str]] = []
        bad = False
        last_id: object = None
        started = False
        for raw in cursor:
            if not started or raw[0] != last_id:
                if started:
                    yield _finish(store, row, labels, bad)
                started, last_id = True, raw[0]
                row = check_note_row(raw[:10])
                labels, bad = [], row is None
            key, value = raw[10], raw[11]
            if bad or (key is None and value is None):
                continue
            pair = check_label_row(key, value)
            if pair is None or len(labels) >= MAX_LABELS:
                bad = True
                continue
            labels.append(pair)
        if started:
            yield _finish(store, row, labels, bad)
    finally:
        cursor.close()


def _finish(
    store: Store, row: NoteRow | None, labels: list[tuple[str, str]], bad: bool
) -> NoteRow | None:
    if bad or row is None:
        store.bad_rows += 1
        return None
    return replace(row, labels=tuple(labels))


def scan(store: Store, filters: Filters, now: int) -> ListResult:
    """The ``list`` result: ``total`` counts the matching notes that pass the status filter,
    ``hidden`` those that match every other filter and are superseded or expired (0 with
    ``--all``), and at most ``filters.limit`` summaries, newest first, are kept."""
    wanted = filters.labels
    notes: list[dict[str, Any]] = []
    total = hidden = 0
    with store.read():
        for row in _checked_notes(store, filters):
            if row is None or (wanted and not wanted <= set(row.labels)):
                continue
            status = note_status(row, now)
            if status != LIVE and not filters.include_all:
                hidden += 1
                continue
            total += 1
            if len(notes) < filters.limit:
                notes.append(summary(row, status))
    return ListResult(notes, total, hidden)


def fetch_note(store: Store, note_id: int, now: int) -> dict[str, Any] | None:
    """The ``show`` document of ``note_id``, or ``None`` when no such note exists. A row that
    fails its checks raises ``StoreError("db_corrupt")``. ``supersedes`` is the smallest valid id
    of a note whose ``superseded_by`` is this id (one at most in a store the tool wrote). Both
    reads share one snapshot."""
    with store.read():
        found = list(store.notes("WHERE id = ?", (note_id,), labels=True, on_bad="raise"))
        if not found:
            return None
        row = found[0]
        supersedes = store.replaced_note_id(note_id)
    return show_document(row, note_status(row, now), supersedes)
