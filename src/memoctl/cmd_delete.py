"""``memoctl delete <id>... [--yes]`` (interface.md, ``delete``).

Order, and nothing is deleted before the last refusal of the list has passed:

1. parse: each id against the ``id`` grammar, repeats collapse, 1 to 500 distinct ids (exit 2
   ``usage``, field ``id``); the actor;
2. look up every id in a read transaction (no state directory, no ``state.db`` or an unknown id:
   exit 10 ``not_found`` with the smallest unknown id);
3. without ``--yes``, ask on ``/dev/tty`` through the gateway, with the deadline paused; only
   ``y`` or ``yes`` (any case) goes on, anything else (no terminal, end of input, another answer)
   is exit 13 ``needs_human``;
4. one write transaction: every id is looked up again (gone: exit 10, nothing deleted), the
   notes and their labels are deleted and the chain is repointed (``memoctl.notes``); rate rows
   are never touched;
5. the deadline is disarmed right after the commit, then one log record per deleted note, then
   the output. A log failure is the warning ``log_failed`` and never changes the exit code.

A failure at any point before the commit leaves every note in place (the transaction rolls
back); a kill during it leaves the old state or the new one (SQLite).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from memoctl import deadline, phases
from memoctl.author import resolve_actor
from memoctl.cli_args import Command
from memoctl.cli_errors import NeedsHumanError, NoteNotFoundError, UsageError
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError, matches
from memoctl.logfile import LogRecord, append_record
from memoctl.notes import (
    DeletePlan,
    apply_delete,
    find_targets,
    plan_delete,
    smallest_missing,
)
from memoctl.notes_read import open_read_store
from memoctl.render import DELETE_LOG_FAILED, Rendered, emit, format_delete
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import DeleteTarget, Mode, open_store

__all__ = ["COMMAND", "MAX_IDS", "run"]

MAX_IDS = 500
ACCEPTED = frozenset({"y", "yes"})
UNKNOWN = "?"  # a field of the prompt that failed its row check


def _id(text: str) -> int:
    if not matches("id", text):
        raise GrammarError("id")
    return int(text)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ids", nargs="*", type=_id, metavar="id", help="the note ids to delete")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")


def prompt_for(targets: dict[int, DeleteTarget]) -> str:
    """The question: one note names its repo and topic; several name their ids."""
    if len(targets) == 1:
        [target] = targets.values()
        where = f"{target.repo or UNKNOWN}, {target.topic or UNKNOWN}"
        return f"Delete note {target.id} ({where})? [y/N] "
    listed = ", ".join(str(i) for i in sorted(targets))
    return f"Delete {len(targets)} notes ({listed})? [y/N] "


def confirmed(answer: str | None) -> bool:
    """Only ``y`` or ``yes`` in any case, with nothing else on the line."""
    return answer is not None and answer.lower() in ACCEPTED


def lookup(gateway: Gateway, ids: list[int]) -> dict[int, DeleteTarget]:
    """Step 2: every id, in one read snapshot; the smallest unknown id is exit 10."""
    store = open_read_store(gateway)
    if store is None:
        raise NoteNotFoundError(ids[0])
    with store:
        with store.read():
            found = find_targets(store, ids)
    missing = smallest_missing(ids, found)
    if missing is not None:
        raise NoteNotFoundError(missing)
    return found


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    ids = sorted(set(args.ids))
    if not 1 <= len(ids) <= MAX_IDS:
        raise UsageError("id")
    actor = resolve_actor(gateway, args.actor)
    phases.mark("parse")
    targets = lookup(gateway, ids)
    phases.mark("lookup")
    if not args.yes:
        with deadline.current().paused():
            answer = gateway.ask_tty(prompt_for(targets))
        if not confirmed(answer):
            raise NeedsHumanError
        phases.mark("prompt")
    now = gateway.now()
    seconds = int(now.timestamp())
    state = open_state_dir(gateway, create=False)
    if state is None:
        raise NoteNotFoundError(ids[0])
    with state:
        store = open_store(state, Mode.WRITE, configure=deadline.current().configure_store)
        if store is None:
            raise NoteNotFoundError(ids[0])
        try:
            with store.write():
                found = find_targets(store, ids)
                missing = smallest_missing(ids, found)
                if missing is not None:
                    raise NoteNotFoundError(missing)
                plan = plan_delete(store, found, seconds)
                apply_delete(store, plan)
            phases.mark("transaction")
            # Right after the commit: a timeout is never reported once the change is durable
            # (when the timer ran out during the commit this call exits 12, with no log record).
            deadline.current().disarm()
        finally:
            store.close()
        log_failed = append_records(state, plan, actor.actor, actor.source.value, now)
        phases.mark("log")
    print_result(args, gateway, plan, log_failed=log_failed)
    phases.mark("output")
    return 0


def append_records(
    state: StateDir, plan: DeletePlan, actor: str, source: str, now: datetime
) -> bool:
    """One record per deleted note, ascending by id; the record of the note a reinstated note
    pointed to carries that note's id. True when any append failed."""
    cleared: dict[int, int] = {}
    for item in plan.reinstated:
        cleared.setdefault(item.via, item.id)
    failed = False
    for target in plan.deleted:
        record = LogRecord(
            event="delete",
            note_id=target.id,
            actor=actor,
            actor_source=source,
            repo=target.repo,
            topic=target.topic,
            kind=target.kind,
            size=target.size,
            body_sha256=target.body_sha256,
            reinstated=cleared.get(target.id),
        )
        try:
            append_record(state, record, now)
        except Exception:  # noqa: BLE001 - the change is durable: any failure is a warning
            failed = True
    return failed


def print_result(
    args: argparse.Namespace, gateway: Gateway, plan: DeletePlan, *, log_failed: bool
) -> None:
    deleted = [t.id for t in plan.deleted]
    warnings = DELETE_LOG_FAILED if log_failed else ""
    if args.json:
        document = {"deleted": deleted, "reinstated": [r.id for r in plan.reinstated]}
        line = json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
        emit(gateway, Rendered(line, warnings=warnings))
        return
    by_deleted: dict[int, tuple[int, bool]] = {}
    for item in plan.reinstated:
        by_deleted.setdefault(item.via, (item.id, item.expired))
    shown = format_delete(deleted, by_deleted)
    emit(gateway, Rendered("" if args.quiet else shown.out, warnings=warnings))


COMMAND = Command("delete", "delete notes (asks for confirmation)", run, configure)
