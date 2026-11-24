"""The rules of ``add``: dedupe (step 10) and the ``--supersedes`` checks (step 11).

The rules live here; every query is a :class:`~memoctl.store.Store` method (only ``store.py``
touches SQLite). Every function runs inside the caller's ``Store.write()`` transaction, so what
is checked is what is committed: no other writer can change a note between a check and the
insert. todo(#18): the limit rules; todo(#30), todo(#31): ``delete`` and ``prune``.
"""

from __future__ import annotations

from dataclasses import dataclass

from memoctl.cli_errors import (
    AlreadySupersededError,
    NoteNotFoundError,
    SupersedeMismatchError,
)
from memoctl.notes_read import LIVE, note_status
from memoctl.store import NoteRow, Store

__all__ = ["NoteKey", "check_supersede_target", "find_duplicate"]


@dataclass(frozen=True)
class NoteKey:
    """The dedupe key: repo, topic, resolved kind, label set and ``body_sha256`` (the hash of
    the exact body bytes), so ``a`` and ``a\\n`` are different keys."""

    repo: str
    topic: str
    kind: str
    labels: frozenset[tuple[str, str]]
    body_sha256: str

    def matches(self, row: NoteRow) -> bool:
        return (
            row.repo == self.repo
            and row.topic == self.topic
            and row.kind == self.kind
            and row.body_sha256 == self.body_sha256
            and frozenset(row.labels) == self.labels
        )


def find_duplicate(store: Store, key: NoteKey, now: int, supersedes: int | None) -> NoteRow | None:
    """The stored note a retry of this call would return, or ``None`` (interface.md, ``add``).

    Without ``supersedes``: the live note with the key that has the highest id. With it: the
    note named by the target's ``superseded_by``, when it has the key and is live (the retry of
    an earlier identical call); otherwise no dedupe applies, even when a live note with the key
    exists. A hit is always live. A target row that fails its row checks raises
    ``StoreError("db_corrupt")``, as the supersede checks would.
    """
    if supersedes is None:
        for row in store.live_with_key(key.repo, key.topic, key.kind, key.body_sha256, now):
            if key.matches(row):
                return row
        return None
    target = store.get_note(supersedes)
    if target is None or target.repo != key.repo or target.superseded_by is None:
        return None  # another repo's target is step 11's refusal; its replacement is never shown
    found = next(
        store.notes("WHERE id = ?", (target.superseded_by,), labels=True),
        None,
    )
    if found is not None and key.matches(found) and note_status(found, now) == LIVE:
        return found
    return None


def check_supersede_target(store: Store, target_id: int, repo: str) -> NoteRow:
    """The ``--supersedes`` checks, in the interface's order: the target exists (exit 10), is in
    the same repo (exit 20 ``supersede_mismatch``) and is not already superseded (exit 20
    ``already_superseded``; an expired note may be superseded). Returns the target row."""
    target = store.get_note(target_id)
    if target is None:
        raise NoteNotFoundError(target_id)
    if target.repo != repo:
        raise SupersedeMismatchError
    if target.superseded_by is not None:
        raise AlreadySupersededError
    return target
