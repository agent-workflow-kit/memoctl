"""``memoctl add``: the 14 ordered steps of interface.md (``add``).

Each step is one function, called in order by :func:`run`; a step takes what the earlier steps
produced and returns the next stage, so the order is the order of the calls and nothing later
can run before an earlier refusal. The rules of the order:

- the first refusal wins: a step that fails raises, and no later step has started;
- nothing touches the state directory, ``state.db`` or the log before steps 1 to 7 have passed
  (steps 8 and 9 may create the directory and an empty database, the only writes before the
  commit of step 13);
- the clock is read once, at step 7, and that value is passed down (never read again);
- ``check_secrets`` runs at step 6, after every field passed its grammar and length bound and
  after the body passed its rules, because its NFKC pass is unbounded on its own;
- the deadline is armed by ``cli.main`` (step 1) and disarmed right after the commit (step 13),
  before the log append and the output, so a timeout is never reported after the change is
  durable.

``--dry-run`` stops after step 7: it never reaches step 8, so it never looks at the state
directory or ``state.db``, and it runs no dedupe, supersede check or limit. The deadline is
disarmed before its output, like a real add.

Seams owned by later issues are marked with ``TODO(#N)`` at the exact step: #20 (concurrency).

A crash (or a signal) between the commit of step 13 and the log append of step 14 leaves a stored
note with no log record, as the interface documents; the retry then finds the note and
writes no record either (a dedupe hit).

Nothing here renders an exception: failures leave as typed exceptions that ``cli_errors`` maps
to fixed text, and no value from argv, stdin or the environment is put in a message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from memoctl import deadline
from memoctl.author import (
    ActorResult,
    KindResult,
    kind_is_shown,
    resolve_actor,
    resolve_kind,
)
from memoctl.body import validate_body
from memoctl.bodyread import STDIN, read_body
from memoctl.cli_args import Command
from memoctl.cli_errors import UsageError
from memoctl.config import Config, load_config
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError, is_add_repo_id, matches, parse_label
from memoctl.lifetime import (
    Lifetime,
    compute_lifetime,
    format_timestamp,
    parse_expires,
    seconds_since_epoch,
)
from memoctl.logfile import LogRecord, append_record
from memoctl.notes import NoteKey, check_supersede_target, evaluate_limits, find_duplicate
from memoctl.notes_read import LIVE, note_status
from memoctl.render import (
    WARNING_MESSAGES,
    Rendered,
    emit,
    format_add,
    format_add_dry_run,
    format_warnings,
)
from memoctl.secretscan import check_secrets
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import Mode, Store, open_store

__all__ = ["COMMAND", "run"]

LOG_FAILED_WARNING = f"warning (log_failed): {WARNING_MESSAGES['log_failed']}"


# -- the stages a step hands to the next --------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """Step 1: the arguments, each checked against its grammar."""

    repo: str
    topic: str
    labels: tuple[tuple[str, str], ...]  # distinct, sorted by key then value
    kind_flag: str | None
    supersedes: int | None
    actor: ActorResult
    requested: int | None  # --expires in seconds
    body_source: Path | str  # a path, or "-" for standard input
    json_mode: bool
    quiet: bool
    dry_run: bool


@dataclass(frozen=True)
class Plan:
    """Steps 2 to 7: everything the write needs, fixed before any state is touched."""

    call: Call
    config: Config  # the effective config of the repo
    kind: KindResult
    body: str
    body_sha256: str
    size: int
    now: datetime  # the one clock reading, kept for the log timestamp
    created: int  # ``now`` in epoch seconds
    lifetime: Lifetime


@dataclass(frozen=True)
class Result:
    """What the command prints: the stored note's fields (the schema ``cli/add``)."""

    id: int | None  # None exactly for a dry run
    created: bool
    repo: str
    topic: str
    kind: str
    labels: tuple[tuple[str, str], ...]
    expires: int
    supersedes: int | None
    size: int
    body_sha256: str
    warnings: tuple[str, ...]
    dry_run: bool = False

    def document(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created": self.created,
            "dry_run": self.dry_run,
            "repo": self.repo,
            "topic": self.topic,
            "kind": self.kind,
            "labels": [f"{k}={v}" for k, v in self.labels],
            "expires": format_timestamp(self.expires),
            "supersedes": self.supersedes,
            "size": self.size,
            "body_sha256": self.body_sha256,
            "warnings": list(self.warnings),
        }


# -- the steps ------------------------------------------------------------------------------------


def step1_parse(args: argparse.Namespace, gateway: Gateway) -> Call:
    """Step 1: every grammar that needs no config (exit 2 ``usage``), in the interface's order:
    repo (and its 255-character cap), topic, each label, ``--kind``, ``--supersedes``,
    ``--actor``, ``--expires`` (grammar and the 60-second minimum). The deadline is armed by
    ``cli.main`` before this runs."""
    if not is_add_repo_id(args.repo):
        raise GrammarError("repo")
    if not matches("topic", args.topic):
        raise GrammarError("topic")
    labels = tuple(sorted({parse_label(text) for text in args.label}))  # identical labels collapse
    if args.kind is not None and not (len(args.kind) <= 64 and matches("kind", args.kind)):
        raise GrammarError("kind")
    supersedes = None
    if args.supersedes is not None:
        if not matches("id", args.supersedes):
            raise GrammarError("supersedes")
        supersedes = int(args.supersedes)
    actor = resolve_actor(gateway, args.actor)  # GrammarError("actor") for a bad --actor
    requested = parse_expires(args.expires) if args.expires is not None else None
    # "-" is standard input; a file named "-" is written "./-".
    source: Path | str = STDIN if args.body_file == STDIN else Path(args.body_file)
    return Call(
        args.repo, args.topic, labels, args.kind, supersedes, actor, requested, source,
        bool(args.json), bool(args.quiet), bool(args.dry_run),
    )  # fmt: skip


def step2_load_config(call: Call, gateway: Gateway) -> Config:
    """Step 2: find the home, load the config (exit 2 ``config_invalid``) and return the
    global config. Runs before stdin or a body file is read."""
    return load_config(gateway)


def step3_check_label_count(call: Call, config: Config) -> None:
    """Step 3: the label count against the global ``limits.max_labels`` (exit 2 ``usage``,
    ``details.field`` ``label``, ``details.limit``)."""
    limit = config.limits.max_labels
    if len(call.labels) > limit:
        raise UsageError("label", limit)


def step4_resolve_kind(call: Call, effective: Config, gateway: Gateway) -> KindResult:
    """Step 4: ``--kind``, else the variable ``kind_env`` names, else ``unknown``."""
    return resolve_kind(gateway, effective, call.kind_flag)


def step5_read_body(call: Call, effective: Config, gateway: Gateway) -> tuple[bytes, str]:
    """Step 5: read the body with the effective ``limits.max_body_bytes`` and check Body rules
    items 1 to 6 (exit 2). The reader is bounded and returns raw bytes; nothing is rewritten."""
    limit = effective.limits.max_body_bytes
    data = read_body(gateway, call.body_source, limit)
    return data, validate_body(data, limit)


def step6_secret_check(
    call: Call, effective: Config, kind: KindResult, body: str, gateway: Gateway
) -> None:
    """Step 6: the secret check over the repo id, topic, every label, the resolved kind and the
    body (exit 20 ``secret_detected``). Every input is already bounded."""
    check_secrets(
        gateway,
        effective.mask_env,
        repo=call.repo,
        topic=call.topic,
        labels=[f"{k}={v}" for k, v in call.labels],
        kind=kind.kind,
        body=body,
    )


def step7_read_clock(
    call: Call, effective: Config, gateway: Gateway
) -> tuple[datetime, int, Lifetime]:
    """Step 7: the one clock read, then the lifetime. ``now`` is not read again."""
    now = gateway.now()
    created = seconds_since_epoch(now)
    lifetime = compute_lifetime(created, effective.retention.max_age, call.requested)
    return now, created, lifetime


def dry_run_result(plan: Plan) -> Result:
    """The output of ``--dry-run``: the call's own validated fields, ``id`` null, ``created``
    true, ``supersedes`` the requested target unchecked, ``expires`` the call's own lifetime
    (never raised by a target, so never ``expires_extended``)."""
    call = plan.call
    return Result(
        None, True, call.repo, call.topic, plan.kind.kind, call.labels, plan.lifetime.expires,
        call.supersedes, plan.size, plan.body_sha256,
        build_warnings(plan, plan.lifetime, hit=False), dry_run=True,
    )  # fmt: skip


def step8_open_state_dir(gateway: Gateway) -> StateDir:
    """Step 8: check the state directory (exit 20 ``unsafe_state``); create it (0700) when
    missing."""
    return open_state_dir(gateway, create=True)


def step9_open_store(state: StateDir) -> Store:
    """Step 9: open or create ``state.db`` and migrate it (exit 12 ``db_corrupt``,
    ``schema_newer``, ``db_locked``). The deadline interrupts long statements and caps the
    lock wait. The caller opens the write transaction (``Store.write``)."""
    store = open_store(state, Mode.INIT, configure=deadline.current().configure_store)
    assert store is not None  # INIT always opens a store
    return store


def step10_dedupe(store: Store, plan: Plan) -> Result | None:
    """Step 10: the retry and dedupe check; a stored live note with the same key is returned
    with ``created`` false and nothing is written (:func:`memoctl.notes.find_duplicate`).

    A hit reports the stored note's own ``expires`` and ``supersedes`` and the warnings of this
    call, never ``expires_extended`` (``build_warnings(..., hit=True)``)."""
    call = plan.call
    key = NoteKey(call.repo, call.topic, plan.kind.kind, frozenset(call.labels), plan.body_sha256)
    row = find_duplicate(store, key, plan.created, call.supersedes)
    if row is None:
        return None
    # Under (b) the target is the note this retry replaced; under (a) the pointer is looked up
    # in the hit's own repo only, so a planted row of another repo never shows its id.
    supersedes = (
        call.supersedes if call.supersedes is not None else store.replaced_note_id(row.id, row.repo)
    )
    return Result(
        row.id, False, row.repo, row.topic, row.kind, row.labels, row.expires,
        supersedes, row.size, row.body_sha256,
        build_warnings(plan, plan.lifetime, hit=True),
    )  # fmt: skip


@dataclass(frozen=True)
class Target:
    """The ``--supersedes`` target after the step 11 checks."""

    id: int
    live: bool  # live at ``now``: it frees its own slot for ``repo_full``


def step11_supersede(store: Store, plan: Plan) -> tuple[Lifetime, Target | None]:
    """Step 11: the ``--supersedes`` checks (exit 10, then 20 ``supersede_mismatch``, then 20
    ``already_superseded``); with a target the lifetime is raised to the target's ``expires``
    when that is later (``compute_lifetime(..., target_expires=...)``). The target is marked
    superseded by step 13, in the same transaction."""
    call = plan.call
    if call.supersedes is None:
        return plan.lifetime, None
    target = check_supersede_target(store, call.supersedes, call.repo)
    lifetime = compute_lifetime(
        plan.created, plan.config.retention.max_age, call.requested, target.expires
    )
    return lifetime, Target(target.id, note_status(target, plan.created) == LIVE)


def step12_limits(store: Store, plan: Plan, target: Target | None) -> None:
    """Step 12: ``repo_full``, ``store_full`` and the two ``rate_limited`` checks, in that order
    (exit 20), inside the write transaction, so the counts are what the insert commits on.
    ``now`` is ``plan.created``; the rules live in :func:`memoctl.notes.evaluate_limits`. The
    rate row itself is written by step 13."""
    refusal = evaluate_limits(
        store, plan.config.limits, plan.call.repo, plan.created,
        target_live=target is not None and target.live,
    )  # fmt: skip
    if refusal is not None:
        raise refusal.error()


def step13_insert(store: Store, plan: Plan, lifetime: Lifetime, target: Target | None) -> Result:
    """Step 13 (the insert part): the note, its labels and its rate row, and ``superseded_by`` of
    the ``--supersedes`` target, in the transaction the caller commits."""
    call = plan.call
    note_id = store.insert_note(
        repo=call.repo,
        topic=call.topic,
        kind=plan.kind.kind,
        body=plan.body,
        body_sha256=plan.body_sha256,
        size=plan.size,
        created=plan.created,
        expires=lifetime.expires,
        labels=call.labels,
    )
    supersedes = None if target is None else target.id
    if supersedes is not None:
        store.mark_superseded(supersedes, note_id)
    return Result(
        note_id, True, call.repo, call.topic, plan.kind.kind, call.labels, lifetime.expires,
        supersedes, plan.size, plan.body_sha256,
        build_warnings(plan, lifetime, hit=False),
    )  # fmt: skip


def build_warnings(plan: Plan, lifetime: Lifetime, *, hit: bool) -> tuple[str, ...]:
    """The ``warnings`` array: ``expires_capped``, ``expires_extended`` (never on a dedupe hit)
    and ``kind_not_shown``, in the schema's order."""
    out: list[str] = []
    if lifetime.capped:
        out.append("expires_capped")
    if lifetime.extended and not hit:
        out.append("expires_extended")
    if not kind_is_shown(plan.kind.kind, plan.config):
        out.append("kind_not_shown")
    return tuple(out)


def step14_append_log(state: StateDir, plan: Plan, result: Result) -> bool:
    """Step 14 (the log part): append the record after the commit. A failure never changes the
    exit code; it returns True (the ``log_failed`` warning, printed with the output) and puts no
    value in it. A dedupe hit writes no record."""
    assert result.id is not None  # a dry run never gets here
    if not result.created:
        return False
    call = plan.call
    record = LogRecord(
        event="add",
        note_id=result.id,
        actor=call.actor.actor,
        actor_source=call.actor.source.value,
        repo=result.repo,
        topic=result.topic,
        kind=result.kind,
        size=result.size,
        body_sha256=result.body_sha256[:16],
        supersedes=result.supersedes,
    )
    try:
        append_record(state, record, plan.now)
    except Exception:  # noqa: BLE001 - the note is durable: any failure here is only a warning
        return True
    return False


def print_result(call: Call, result: Result, gateway: Gateway, *, log_failed: bool = False) -> None:
    """Step 14 (the output part): the JSON document, or ``note <id> created|exists`` (the dry-run
    line for a dry run); the human line is not printed with ``--quiet``. The warning lines
    (``warning (<name>): <fixed message>``) follow on stderr in the order of the ``warnings``
    array, then ``log_failed``; ``--quiet`` never suppresses them. ``log_failed`` is not in
    the JSON ``warnings`` (it is about the log, not the note)."""
    names = (*result.warnings, *(("log_failed",) if log_failed else ()))
    if call.json_mode:
        line = json.dumps(result.document(), ensure_ascii=True, separators=(",", ":")) + "\n"
        emit(gateway, Rendered(line, warnings=format_warnings(names)))
        return
    if result.id is None:
        shown = format_add_dry_run(names)
    else:
        shown = format_add(result.id, created=result.created, warnings=names)
    emit(gateway, Rendered("" if call.quiet else shown.out, warnings=shown.warnings))


# -- the command ----------------------------------------------------------------------------------


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    call = step1_parse(args, gateway)
    config = step2_load_config(call, gateway)
    step3_check_label_count(call, config)
    effective = config.for_repo(call.repo)
    kind = step4_resolve_kind(call, effective, gateway)
    data, body = step5_read_body(call, effective, gateway)
    step6_secret_check(call, effective, kind, body, gateway)
    now, created, lifetime = step7_read_clock(call, effective, gateway)
    plan = Plan(
        call, effective, kind, body, hashlib.sha256(data).hexdigest(), len(data), now, created,
        lifetime,
    )  # fmt: skip
    if call.dry_run:
        # Stops after step 7: no state directory, no state.db, no dedupe, supersede check or
        # limit, no log. Disarm before the output, as after the commit of a real add.
        deadline.current().disarm()
        print_result(call, dry_run_result(plan), gateway)
        return 0
    with step8_open_state_dir(gateway) as state:
        store = step9_open_store(state)
        try:
            with store.write():
                result = step10_dedupe(store, plan)
                if result is None:
                    final, target = step11_supersede(store, plan)
                    step12_limits(store, plan, target)
                    result = step13_insert(store, plan, final, target)
            # Right after the commit and before the log append and the output: a timeout is
            # never reported once the change is durable. When the timer ran out during the
            # commit, this call ends the process with exit 12 (no log record).
            deadline.current().disarm()
        finally:
            store.close()
        log_failed = step14_append_log(state, plan, result)
    print_result(call, result, gateway, log_failed=log_failed)
    return 0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="the repo id the note is about")
    parser.add_argument("--topic", required=True, help="a short topic that groups notes")
    parser.add_argument(
        "--body-file", required=True, help="the body: a file path, or - for standard input"
    )
    parser.add_argument("--label", action="append", default=[], help="key=value (repeatable)")
    parser.add_argument("--kind", default=None, help="the claimed author kind")
    parser.add_argument("--supersedes", default=None, help="the id of the note this replaces")
    parser.add_argument("--expires", default=None, help="a lifetime such as 2h or 7d")


COMMAND = Command(
    "add",
    "leave a note for the next agent",
    run,
    configure,
    dry_run=True,
)
