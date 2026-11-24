"""The rules of ``add``: dedupe (step 10) and the ``--supersedes`` checks (step 11).

The rules live here; every query is a :class:`~memoctl.store.Store` method (only ``store.py``
touches SQLite). Every function runs inside the caller's ``Store.write()`` transaction, so what
is checked is what is committed: no other writer can change a note between a check and the
insert. The limit rules (``check_limits``, ``evaluate_limits``) are here too.
``delete``'s rules (lookup, chain repointing) are at the end; todo(#31): ``prune``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from memoctl.cli_errors import (
    AlreadySupersededError,
    NoteNotFoundError,
    RateLimitedError,
    RepoFullError,
    StoreFullError,
    SupersedeMismatchError,
)
from memoctl.config import Limits
from memoctl.notes_read import LIVE, note_status
from memoctl.store import DeleteTarget, NoteRow, Store

__all__ = [
    "DeletePlan",
    "Reinstated",
    "LimitCounts",
    "LimitRefusal",
    "NoteKey",
    "check_limits",
    "check_supersede_target",
    "evaluate_limits",
    "apply_delete",
    "find_duplicate",
    "find_targets",
    "plan_delete",
    "smallest_missing",
    "rate_retry_after",
]


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


# -- limits (interface.md, Limits) -----------------------------------------------------------------


@dataclass(frozen=True)
class LimitCounts:
    """What the four limits compare: live notes of the repo, notes of any status in the store,
    and the rate rows inside the window (of the repo, of every repo)."""

    live_in_repo: int
    notes_in_store: int
    rate_in_repo: int
    rate_in_store: int


@dataclass(frozen=True)
class LimitRefusal:
    """The first limit that fails. ``scope`` and ``rate_count`` are set for ``rate_limited``."""

    reason: Literal["repo_full", "store_full", "rate_limited"]
    limit: int
    scope: Literal["repo", "store"] | None = None
    rate_count: int = 0
    retry_after_s: int = 0  # set by evaluate_limits for a rate refusal

    def error(self) -> Exception:
        if self.reason == "repo_full":
            return RepoFullError(self.limit)
        if self.reason == "store_full":
            return StoreFullError(self.limit)
        assert self.scope is not None
        return RateLimitedError(self.retry_after_s, self.limit, self.scope)


def check_limits(counts: LimitCounts, limits: Limits, *, target_live: bool) -> LimitRefusal | None:
    """The four limits in the interface's order; the first that fails, else ``None``.

    Each compares ``count + 1 > limit`` (the note about to be added included). ``target_live``
    is true when ``--supersedes`` names a live note: it frees its own slot for ``repo_full``
    only (it still adds a row to the store and a rate row)."""
    freed = 1 if target_live else 0
    if counts.live_in_repo + 1 - freed > limits.live_per_repo:
        return LimitRefusal("repo_full", limits.live_per_repo)
    if counts.notes_in_store + 1 > limits.total:
        return LimitRefusal("store_full", limits.total)
    if counts.rate_in_repo + 1 > limits.adds_per_window:
        return LimitRefusal("rate_limited", limits.adds_per_window, "repo", counts.rate_in_repo)
    if counts.rate_in_store + 1 > limits.adds_per_window_total:
        return LimitRefusal(
            "rate_limited", limits.adds_per_window_total, "store", counts.rate_in_store
        )
    return None


def rate_retry_after(pivot_at: int, window: int, now: int) -> int:
    """Seconds until the counted row at ``pivot_at`` leaves the window (``at > now - window``):
    ``max(1, pivot_at + window - now)``, integer arithmetic only."""
    return max(1, pivot_at + window - now)


def evaluate_limits(
    store: Store, limits: Limits, repo: str, now: int, *, target_live: bool
) -> LimitRefusal | None:
    """Count what the limits need (lazily, in the documented order, so a refusal skips the
    later counts) and return the first refusal with its ``retry_after_s``, else ``None``. Reads
    only; call it inside the caller's transaction.

    The note counts are capped one above their limit; the live count visits the repo's rows via
    the index, so its work is bounded by the repo's rows, at most ``limits.total``. The
    rate counts are exact because ``retry_after_s`` needs the position of the row that frees a
    slot (the rows counted are the ones inside the window, which the limits bound)."""
    window = limits.window
    live = store.count_live(repo, now, limits.live_per_repo + 1)
    found = check_limits(LimitCounts(live, 0, 0, 0), limits, target_live=target_live)
    if found is not None:
        return found
    total = store.count_notes(limits.total + 1)
    found = check_limits(LimitCounts(live, total, 0, 0), limits, target_live=target_live)
    if found is not None:
        return found
    in_repo = store.count_rate(repo, now, window)
    found = check_limits(LimitCounts(live, total, in_repo, 0), limits, target_live=target_live)
    if found is None:
        in_store = store.count_rate(None, now, window)
        found = check_limits(
            LimitCounts(live, total, in_repo, in_store), limits, target_live=target_live
        )
    if found is None or found.scope is None:
        return found
    # k counted rows, limit L (k >= L): the slot frees when a_(k-L+1) leaves the window.
    index = found.rate_count - found.limit
    pivot = store.rate_instant(repo if found.scope == "repo" else None, now, window, index)
    return replace(found, retry_after_s=rate_retry_after(pivot, window, now))


# -- delete (interface.md, ``delete``) ---------------------------------------------------------


def find_targets(store: Store, ids: Sequence[int]) -> dict[int, DeleteTarget]:
    """Every id of ``ids`` (distinct, ascending) that names a note. A row that fails its row
    checks is found like any other (its bad fields are ``None``): ``delete`` is the way to
    remove it. The caller raises ``NoteNotFoundError`` for the smallest id that is missing."""
    found: dict[int, DeleteTarget] = {}
    for note_id in ids:
        target = store.delete_target(note_id)
        if target is not None:
            found[note_id] = target
    return found


def smallest_missing(ids: Sequence[int], found: dict[int, DeleteTarget]) -> int | None:
    return next((i for i in ids if i not in found), None)


@dataclass(frozen=True)
class Reinstated:
    """A note whose ``superseded_by`` is cleared: ``via`` is the deleted note it pointed to."""

    id: int
    via: int
    expired: bool


@dataclass(frozen=True)
class DeletePlan:
    deleted: tuple[DeleteTarget, ...]  # ascending by id
    repoints: tuple[tuple[int, int], ...]  # (note, its new superseded_by)
    reinstated: tuple[Reinstated, ...]  # ascending by id


def plan_delete(store: Store, targets: dict[int, DeleteTarget], now: int) -> DeletePlan:
    """The chain rule, computed before anything is deleted (reads only).

    For each note P that is not deleted and whose ``superseded_by`` names a deleted note D,
    follow ``superseded_by`` upward from D to the first note that is not deleted: P points to
    it, or, when D's chain ends (no pointer) among deleted notes, P is cleared and reinstated.
    Ids rise along a chain. A pointer that does not rise (a planted cycle), that names a note
    that does not exist, or an invalid one leaves P untouched: it stays hidden, as ``add``
    already treats a dangling pointer (``already_superseded``). Never a guess that shows more.
    """
    resolved: dict[int, int | None | bool] = {}  # deleted id -> note above, None, or False

    def above(start: int) -> int | None | bool:
        """The first note above ``start`` that is not deleted; ``None`` when the chain ends in
        deleted notes; ``False`` when it cannot be followed."""
        path: list[int] = []
        cur = start
        while True:
            if cur in resolved:
                answer = resolved[cur]
                break
            path.append(cur)
            if targets[cur].pointer_invalid:
                answer = False
                break
            nxt = targets[cur].superseded_by
            if nxt is None:
                answer = None
                break
            if nxt <= cur:
                answer = False
                break
            if nxt in targets:
                cur = nxt
                continue
            answer = nxt if store.note_exists(nxt) else False
            break
        for node in path:
            resolved[node] = answer
        return answer

    repoints: list[tuple[int, int]] = []
    reinstated: list[Reinstated] = []
    for d in sorted(targets):
        for p, expires in store.pointing_at(d):
            if p in targets or p >= d:
                continue
            answer = above(d)
            if answer is False:
                continue
            if answer is None:
                # an unreadable ``expires`` counts as expired: never claim a live note
                reinstated.append(Reinstated(p, d, expires is None or expires <= now))
            else:
                assert isinstance(answer, int)
                repoints.append((p, answer))
    return DeletePlan(
        tuple(targets[i] for i in sorted(targets)),
        tuple(sorted(repoints)),
        tuple(sorted(reinstated, key=lambda r: r.id)),
    )


def apply_delete(store: Store, plan: DeletePlan) -> None:
    """The writes of the plan, in the caller's ``Store.write()`` transaction."""
    for target in plan.deleted:
        store.delete_note(target.id)
    for note_id, by in plan.repoints:
        store.set_pointer(note_id, by)
    for item in plan.reinstated:
        store.set_pointer(item.id, None)
