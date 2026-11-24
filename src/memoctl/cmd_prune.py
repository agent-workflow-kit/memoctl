"""``memoctl prune [--dry-run]`` (interface.md, ``prune``).

One clock read, floored to seconds, is ``now`` for everything. In one write transaction the
notes with ``expires`` <= ``now`` and their labels are deleted and the rate rows with ``at`` <=
``now`` - 86400 are deleted. Nothing else is touched: a replacement never expires before the
note it replaces, so a store the tool wrote has no live note left pointing at a pruned one, and
``prune`` never repoints or reinstates (``delete`` does). A planted pointer to a pruned note
leaves its holder hidden, as ``delete`` leaves a pointer it cannot follow.

Order:

1. parse: the actor (``--dry-run`` takes no other argument);
2. no state directory, no ``state.db`` or an uninitialized one: zero counts, nothing created;
3. ``--dry-run``: one read transaction (no write lock, no migration, never ``busy``);
4. otherwise one write transaction (``BEGIN IMMEDIATE``, 5 s busy timeout capped by the
   deadline). The write lock not obtained in time is the ``busy`` outcome: exit 0, nothing
   pruned;
5. the deadline is disarmed right after the commit (or before the busy output), then one log
   record per pruned note, then the output. A log failure is the warning ``log_failed`` and
   never changes the exit code.

A failure at any point before the commit leaves every note in place (the transaction rolls
back); a kill during it leaves the old state or the new one (SQLite).
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from memoctl import deadline
from memoctl.author import resolve_actor
from memoctl.cli_args import Command
from memoctl.gateway import Gateway
from memoctl.logfile import LogRecord, append_record
from memoctl.notes import apply_prune, count_prune
from memoctl.notes_read import open_read_store
from memoctl.render import PRUNE_LOG_FAILED, Rendered, emit, format_prune
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import DB_LOCKED, DeleteTarget, Mode, StoreError, open_store

__all__ = ["COMMAND", "run"]


def configure(parser: argparse.ArgumentParser) -> None:
    """``prune`` has no arguments of its own."""


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    actor = resolve_actor(gateway, args.actor)
    now = gateway.now()
    seconds = math.floor(now.timestamp())
    if args.dry_run:
        counts = dry_run(gateway, seconds)
        deadline.current().disarm()
        print_result(args, gateway, {"expired": counts[0], "remaining": counts[1], "dry_run": True})
        return 0
    state = open_state_dir(gateway, create=False)
    if state is None:
        return finish(args, gateway, {"expired": 0, "remaining": 0, "dry_run": False})
    with state:
        outcome = write_run(state, seconds)
        if outcome is None:  # the write lock was not obtained: nothing was pruned
            deadline.current().disarm()
            print_result(args, gateway, {"outcome": "busy"})
            return 0
        deadline.current().disarm()  # right after the commit (see cmd_delete)
        targets, remaining = outcome
        failed = append_records(state, targets, actor.actor, actor.source.value, now)
    print_result(
        args,
        gateway,
        {"expired": len(targets), "remaining": remaining, "dry_run": False},
        log_failed=failed,
    )
    return 0


def finish(args: argparse.Namespace, gateway: Gateway, result: dict[str, Any]) -> int:
    deadline.current().disarm()
    print_result(args, gateway, result)
    return 0


def dry_run(gateway: Gateway, seconds: int) -> tuple[int, int]:
    store = open_read_store(gateway)
    if store is None:
        return 0, 0
    with store:
        return count_prune(store, seconds)


def write_run(state: StateDir, seconds: int) -> tuple[list[DeleteTarget], int] | None:
    """The write transaction; ``None`` when the lock was not obtained. An empty store (no
    ``state.db``, or an uninitialized one) is ``([], 0)``: nothing is created or migrated."""
    try:
        store = open_store(state, Mode.WRITE, configure=deadline.current().configure_store)
        if store is None:
            return [], 0
        try:
            with store.write():
                targets = apply_prune(store, seconds)
                remaining = store.count_all_notes()
        finally:
            store.close()
    except StoreError as exc:
        if exc.reason == DB_LOCKED:
            return None
        raise
    return targets, remaining


def append_records(
    state: StateDir, targets: Sequence[DeleteTarget], actor: str, source: str, now: datetime
) -> bool:
    """One record per pruned note, ascending by id. True when any append failed."""
    failed = False
    for target in targets:
        record = LogRecord(
            event="prune",
            note_id=target.id,
            actor=actor,
            actor_source=source,
            repo=target.repo,
            topic=target.topic,
            kind=target.kind,
            size=target.size,
            body_sha256=target.body_sha256,
        )
        try:
            append_record(state, record, now)
        except Exception:  # noqa: BLE001 - the change is durable: any failure is a warning
            failed = True
    return failed


def print_result(
    args: argparse.Namespace,
    gateway: Gateway,
    result: dict[str, Any],
    *,
    log_failed: bool = False,
) -> None:
    shown = format_prune(result)
    warnings = shown.warnings + (PRUNE_LOG_FAILED if log_failed else "")
    if args.json:
        line = json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n"
        emit(gateway, Rendered(line, warnings=warnings))
        return
    emit(gateway, Rendered("" if args.quiet else shown.out, warnings=warnings))


COMMAND = Command("prune", "delete expired notes and old rate rows", run, configure, dry_run=True)
