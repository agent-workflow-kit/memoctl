"""``memoctl prune`` end to end through ``main`` on a fake gateway and a real store.

Chains are written as the store holds them: ``superseded_by`` of the REPLACED note names the
replacement. ``NOW`` is the fake clock; a note with ``expires <= NOW`` is expired.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memoctl.cli import main
from memoctl.logfile import ROTATE_BYTES
from memoctl.render import BUSY_WARNING, PRUNE_LOG_FAILED
from tests.fake_gateway import EPOCH, FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import validate
from tests.test_cmd_delete import log_records, note_ids, pointers, state
from tests.test_cmd_read import NOW, REPO, SHA, add_note, db_path, seed

DAY = 86400


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def run(gw: FakeGateway, *argv: str) -> int:
    gw.terminal.stdout = gw.terminal.stderr = ""
    return main(list(argv), gw)


def rate_rows(gw: FakeGateway) -> list[tuple[str, int]]:
    return [tuple(r) for r in state(gw)["rate"]]  # type: ignore[misc]


def put_rate(conn: sqlite3.Connection, *rows: tuple[str, int]) -> None:
    for repo, at in rows:
        conn.execute("INSERT INTO rate VALUES (?, ?)", (repo, at))


# --- the happy path ---------------------------------------------------------------------------


def test_prunes_expired_notes_labels_and_old_rate_rows(gw: FakeGateway) -> None:
    conn = seed(
        gw,
        [dict(id=1, expires=NOW - 1), dict(id=2, expires=NOW + 1), dict(id=3, expires=NOW)],
        labels=[(1, "a", "b"), (2, "a", "b"), (3, "c", "d")],
    )
    put_rate(conn, ("x", NOW - DAY - 1), ("x", NOW - DAY), ("x", NOW - DAY + 1), ("y", NOW))
    conn.close()
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":2,"remaining":1,"dry_run":false}\n'
    snap = state(gw)
    assert note_ids(gw) == [2]
    assert snap["labels"] == [(2, "a", "b")]
    assert rate_rows(gw) == [("x", NOW - DAY + 1), ("y", NOW)]


def test_boundary_expires_equal_now_is_removed_one_later_is_kept(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW), dict(id=2, expires=NOW + 1)]).close()
    assert run(gw, "prune", "--json") == 0
    assert note_ids(gw) == [2]


def test_rate_boundary_exact_day_is_removed(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    put_rate(conn, ("r", NOW - DAY), ("r", NOW - DAY + 1))
    conn.close()
    assert run(gw, "prune", "--json") == 0
    assert rate_rows(gw) == [("r", NOW - DAY + 1)]


def test_in_window_and_future_rate_rows_are_kept(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    put_rate(conn, ("r", NOW), ("r", NOW + 5), ("r", NOW + 10**12))
    conn.close()
    assert run(gw, "prune") == 0
    assert len(rate_rows(gw)) == 3


def test_planted_negative_and_huge_rate_rows_never_underflow(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    put_rate(conn, ("r", -(2**62)), ("r", -1), ("r", 2**62), ("r", 0))
    conn.close()
    assert run(gw, "prune") == 0
    assert rate_rows(gw) == [("r", 2**62)]


def test_human_output_quiet_and_json(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 5), dict(id=2)]).close()
    assert run(gw, "prune") == 0
    assert gw.terminal.stdout == "pruned 1 expired notes, 1 remain\n"
    assert gw.terminal.stderr == ""
    assert run(gw, "prune", "--quiet") == 0
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "")


def test_quiet_with_json_still_prints_the_document(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    assert run(gw, "prune", "--json", "--quiet") == 0
    assert gw.terminal.stdout == '{"expired":0,"remaining":1,"dry_run":false}\n'


def test_idempotent(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 5), dict(id=2)]).close()
    assert run(gw, "prune", "--json") == 0
    after_first = state(gw)
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":0,"remaining":1,"dry_run":false}\n'
    assert state(gw) == after_first  # not one more log line either


def test_the_clock_is_read_once(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1), dict(id=2, expires=NOW - 1)]).close()
    gw.now_calls = 0
    assert run(gw, "prune") == 0
    assert gw.now_calls == 1


def test_ids_are_never_reused(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1, expires=NOW - 5), dict(id=2, expires=NOW - 5)])
    conn.close()
    run(gw, "prune")
    conn = sqlite3.connect(db_path(gw), isolation_level=None)
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'notes'").fetchone()
    conn.close()
    assert seq == (2,)


# --- what is never removed ----------------------------------------------------------------------


def test_live_and_superseded_unexpired_notes_stay(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, superseded_by=2, expires=NOW + 50),
            dict(id=2, expires=NOW + 50),
            dict(id=3, expires=NOW + 2**40),
        ],
    ).close()
    assert run(gw, "prune") == 0
    assert note_ids(gw) == [1, 2, 3]
    assert pointers(gw) == {1: 2, 2: None, 3: None}


def test_a_replacement_never_expires_before_its_target_so_chains_go_whole(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, superseded_by=2, expires=NOW - 10),
            dict(id=2, superseded_by=3, expires=NOW - 5),
            dict(id=3, expires=NOW - 1),
            dict(id=4, superseded_by=5, expires=NOW + 9),
            dict(id=5, expires=NOW + 9),
        ],
    ).close()
    assert run(gw, "prune", "--json") == 0
    assert note_ids(gw) == [4, 5]
    assert pointers(gw) == {4: 5, 5: None}


def test_a_planted_pointer_to_a_pruned_note_stays_hidden_and_untouched(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, superseded_by=2, expires=NOW + 9), dict(id=2, expires=NOW - 1)]).close()
    assert run(gw, "prune") == 0
    assert note_ids(gw) == [1] and pointers(gw) == {1: 2}  # never reinstated


@pytest.mark.parametrize("offset", [-(2**40), -1, 0])
def test_planted_old_expiry_values_are_pruned(gw: FakeGateway, offset: int) -> None:
    seed(gw, [dict(id=1, expires=NOW + offset)]).close()
    run(gw, "prune")
    assert note_ids(gw) == []


@pytest.mark.parametrize("value", [NOW + 1, 253402300799, 2**62, 2**63 - 1])
def test_planted_far_future_expiry_is_never_pruned(gw: FakeGateway, value: int) -> None:
    seed(gw, [dict(id=1, expires=value)]).close()
    run(gw, "prune")
    assert note_ids(gw) == [1]


@pytest.mark.parametrize(
    "when",
    [
        EPOCH.replace(year=1970) + timedelta(seconds=1),
        EPOCH - timedelta(days=3650),
        EPOCH.replace(year=9999, month=12, day=31),
    ],
)
def test_clock_extremes_remove_only_what_the_clock_says(gw: FakeGateway, when: Any) -> None:
    seed(gw, [dict(id=1, expires=NOW + 1000), dict(id=2, expires=NOW + 100 * DAY)]).close()
    gw.clock.set(when)
    now = math.floor(when.timestamp())
    expected = [i for i, e in ((1, NOW + 1000), (2, NOW + 100 * DAY)) if e > now]
    assert run(gw, "prune") == 0
    assert note_ids(gw) == expected


def test_a_fractional_pre_1970_clock_is_floored_not_truncated(gw: FakeGateway) -> None:
    # -0.5 s floors to -1 (int() would give 0 and wrongly prune the note expiring at 0).
    seed(gw, [dict(id=1, expires=-1), dict(id=2, expires=0)]).close()
    gw.clock.set(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC))
    assert run(gw, "prune") == 0
    assert note_ids(gw) == [2]


def test_clock_one_second_before_expiry_keeps_the_note(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW + 10)]).close()
    gw.clock.set(EPOCH + timedelta(seconds=9, microseconds=999999))
    run(gw, "prune")
    assert note_ids(gw) == [1]
    gw.clock.set(EPOCH + timedelta(seconds=10))
    run(gw, "prune")
    assert note_ids(gw) == []


def test_rows_failing_their_checks_are_pruned_like_any_other(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1, expires=NOW - 1, repo="bad repo", topic="BAD TOPIC", size=3)])
    conn.close()
    assert run(gw, "prune", "--json") == 0
    assert note_ids(gw) == []
    [rec] = log_records(gw)
    assert (rec["repo"], rec["topic"], rec["size"]) == (None, None, None)
    assert rec["kind"] == "unknown"


# --- values over the 1 MiB length limit ---------------------------------------------------------

BIG = "x" * (1_048_576 + 10)


@pytest.mark.parametrize("which", ["key", "value"])
def test_an_oversized_label_does_not_stop_the_prune(gw: FakeGateway, which: str) -> None:
    key, value = (BIG, "v") if which == "key" else ("k", BIG)
    seed(gw, [dict(id=1, expires=NOW - 1), dict(id=2)], labels=[(1, key, value)]).close()
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":1,"remaining":1,"dry_run":false}\n'
    assert note_ids(gw) == [2]
    assert state(gw)["labels"] == []


def test_an_oversized_rate_repo_does_not_stop_the_prune(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    put_rate(conn, (BIG, 0), ("r", NOW))
    conn.close()
    assert run(gw, "prune", "--json") == 0
    assert rate_rows(gw) == [("r", NOW)]


# --- dry run ----------------------------------------------------------------------------------


def test_dry_run_changes_nothing_and_matches_the_real_run(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=i, expires=NOW - i + 3) for i in range(1, 8)])
    put_rate(conn, ("r", 0))
    conn.close()
    before = state(gw)
    assert run(gw, "prune", "--dry-run", "--json") == 0
    dry = json.loads(gw.terminal.stdout)
    assert dry == {"expired": 5, "remaining": 2, "dry_run": True}
    assert state(gw) == before
    assert run(gw, "prune", "--json") == 0
    real = json.loads(gw.terminal.stdout)
    assert (real["expired"], real["remaining"]) == (dry["expired"], dry["remaining"])
    assert real["dry_run"] is False


def test_dry_run_human_and_quiet(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1), dict(id=2)]).close()
    assert run(gw, "prune", "--dry-run") == 0
    assert gw.terminal.stdout == "dry run: would have pruned 1 expired notes, 1 would remain\n"
    assert run(gw, "prune", "--dry-run", "--quiet") == 0
    assert gw.terminal.stdout == ""


def test_dry_run_never_migrates_or_writes(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    path = db_path(gw)
    mtime = path.stat().st_mtime_ns
    raw = path.read_bytes()
    assert run(gw, "prune", "--dry-run") == 0
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == mtime
    assert not (path.parent / "log.jsonl").exists()


def test_dry_run_is_not_busy_while_a_writer_holds_the_lock(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    holder = sqlite3.connect(db_path(gw), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert run(gw, "prune", "--dry-run", "--json") == 0
        assert json.loads(gw.terminal.stdout) == {"expired": 1, "remaining": 0, "dry_run": True}
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# --- a missing or empty store -------------------------------------------------------------------


def test_missing_store_is_zero_counts_and_creates_nothing(gw: FakeGateway) -> None:
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":0,"remaining":0,"dry_run":false}\n'
    assert not (gw.home() / ".local").exists()
    assert run(gw, "prune", "--dry-run", "--json") == 0
    assert gw.terminal.stdout == '{"expired":0,"remaining":0,"dry_run":true}\n'
    assert not (gw.home() / ".local").exists()


def test_missing_database_in_an_existing_state_dir_creates_nothing(gw: FakeGateway) -> None:
    state_dir = gw.home() / ".local" / "state" / "memoctl"
    state_dir.mkdir(parents=True, mode=0o700)
    assert run(gw, "prune") == 0
    assert gw.terminal.stdout == "pruned 0 expired notes, 0 remain\n"
    assert sorted(p.name for p in state_dir.iterdir()) == []


def test_zero_length_database_is_an_empty_store(gw: FakeGateway) -> None:
    state_dir = gw.home() / ".local" / "state" / "memoctl"
    state_dir.mkdir(parents=True, mode=0o700)
    (state_dir / "state.db").touch(mode=0o600)
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":0,"remaining":0,"dry_run":false}\n'
    assert (state_dir / "state.db").stat().st_size == 0


# --- the log ----------------------------------------------------------------------------------


def test_one_log_record_per_pruned_note_without_bodies(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1, body="secret body"), dict(id=2, expires=NOW - 1)]).close()
    assert run(gw, "prune", "--actor", "auto:memoctl-cron") == 0
    recs = log_records(gw)
    assert [(r["event"], r["note_id"]) for r in recs] == [("prune", 1), ("prune", 2)]
    for r in recs:
        validate("log", r)
        assert (r["size"], r["body_sha256"]) == (11 if r["note_id"] == 1 else 5, SHA[:16])
        assert r["reinstated"] is None and r["supersedes"] is None
        assert (r["actor"], r["actor_source"]) == ("auto:memoctl-cron", "flag")
        assert r["repo"] == REPO and r["kind"] == "unknown" and r["topic"] == "t"
    assert "secret" not in json.dumps(recs)


def test_log_failure_is_a_warning_and_exit_0(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    (db_path(gw).parent / "log.jsonl").mkdir()
    assert run(gw, "prune", "--json") == 0
    assert gw.terminal.stdout == '{"expired":1,"remaining":0,"dry_run":false}\n'
    assert gw.terminal.stderr == PRUNE_LOG_FAILED
    assert note_ids(gw) == []
    assert run(gw, "prune", "--quiet") == 0  # a retry finds less to do
    assert gw.terminal.stderr == ""


def test_quiet_does_not_hide_the_log_warning(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    (db_path(gw).parent / "log.jsonl").mkdir()
    assert run(gw, "prune", "--quiet") == 0
    assert gw.terminal.stdout == "" and "log_failed" in gw.terminal.stderr


def test_log_failed_wording_is_in_the_interface_table() -> None:
    text = (Path(__file__).parent.parent / "docs" / "interface.md").read_text()
    line = PRUNE_LOG_FAILED.rstrip("\n")
    assert f"| `log_failed` | `prune` | `{line}` |" in text
    assert f"| `prune_busy` | `prune` | `{BUSY_WARNING}` |" in text


def test_rotation_is_the_logs_own(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1), dict(id=2, expires=NOW - 1)]).close()
    log = db_path(gw).parent / "log.jsonl"
    log.write_bytes(b"x" * ROTATE_BYTES)  # at the cap: the next append rotates
    log.chmod(0o600)
    assert run(gw, "prune") == 0
    assert gw.terminal.stderr == ""
    assert (db_path(gw).parent / "log.jsonl.1").stat().st_size == ROTATE_BYTES
    assert [(r["event"], r["note_id"]) for r in log_records(gw)] == [("prune", 1), ("prune", 2)]


# --- arguments ---------------------------------------------------------------------------------

BAD_ARGS = [
    ["--dry-run=x"], ["--dry"], ["--dry-ru"], ["--dryrun"], ["--actor"], ["--actor", "bad actor"],
    ["extra"], ["--", "x"], ["1"], ["--yes"], ["--limit", "3"], ["--json=1"],
    ["--quiet", "--verbose"],
]  # fmt: skip


@pytest.mark.parametrize("extra", BAD_ARGS)
def test_bad_arguments_are_usage_and_prune_nothing(gw: FakeGateway, extra: list[str]) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    before = state(gw)
    assert run(gw, "prune", *extra) == 2
    assert state(gw) == before


def test_global_flag_before_the_subcommand_is_usage(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    assert run(gw, "--dry-run", "prune") == 2
    assert note_ids(gw) == [1]


def test_bad_actor_exits_2_before_touching_the_store(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    assert run(gw, "prune", "--actor", "Bad Actor", "--json") == 2
    assert json.loads(gw.terminal.stdout)["error"]["details"]["field"] == "actor"
    assert note_ids(gw) == [1]


def test_actor_is_validated_with_dry_run_too(gw: FakeGateway) -> None:
    assert run(gw, "prune", "--dry-run", "--actor", "x y") == 2


# --- errors ------------------------------------------------------------------------------------


def test_unsafe_state_is_refused_and_nothing_pruned(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    db_path(gw).parent.chmod(0o755)
    assert run(gw, "prune", "--json") == 20
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "unsafe_state"
    assert note_ids(gw) == [1]


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_newer_schema_exits_12(gw: FakeGateway, flags: list[str]) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    conn = sqlite3.connect(db_path(gw))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    assert run(gw, "prune", "--json", *flags) == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "schema_newer"
    assert note_ids(gw) == [1]


def test_foreign_database_is_corrupt_not_busy(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    conn = sqlite3.connect(db_path(gw))
    conn.execute("PRAGMA application_id = 5")
    conn.close()
    assert run(gw, "prune", "--json") == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "db_corrupt"
    assert note_ids(gw) == [1]


# --- ordering: an earlier failing step wins and nothing is pruned ----------------------------

ORDER = [
    # (argv tail, setup, expected exit)
    (["--actor", "x y", "--dry-run"], "ok", 2),
    (["--actor", "x y"], "ok", 2),
    (["--actor", "x y"], "unsafe", 2),
    ([], "unsafe", 20),
    (["--dry-run"], "unsafe", 20),
    ([], "newer", 12),
]


@pytest.mark.parametrize(("tail", "setup", "code"), ORDER)
def test_order_earlier_failure_wins_and_nothing_is_pruned(
    gw: FakeGateway, tail: list[str], setup: str, code: int
) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    if setup == "unsafe":
        db_path(gw).parent.chmod(0o755)
    if setup == "newer":
        conn = sqlite3.connect(db_path(gw))
        conn.execute("PRAGMA user_version = 99")
        conn.close()
    assert run(gw, "prune", "--json", *tail) == code
    if setup == "unsafe":
        db_path(gw).parent.chmod(0o700)
    assert note_ids(gw) == [1]
    assert not (db_path(gw).parent / "log.jsonl").exists()


# --- busy --------------------------------------------------------------------------------------


@pytest.fixture
def short_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memoctl.store.WRITE_BUSY_MS", 50)


def test_busy_outcome_when_a_writer_holds_the_lock(gw: FakeGateway, short_busy: None) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    holder = sqlite3.connect(db_path(gw), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        before = state(gw)
        assert run(gw, "prune", "--json") == 0
        assert gw.terminal.stdout == '{"outcome":"busy"}\n'
        assert gw.terminal.stderr == BUSY_WARNING + "\n"
        assert state(gw) == before
        assert run(gw, "prune") == 0
        assert gw.terminal.stdout == "prune skipped: store busy\n"
        assert gw.terminal.stderr == BUSY_WARNING + "\n"
        assert run(gw, "prune", "--quiet") == 0
        assert gw.terminal.stdout == "" and gw.terminal.stderr == BUSY_WARNING + "\n"
        assert run(gw, "prune", "--quiet", "--json") == 0
        assert gw.terminal.stdout == '{"outcome":"busy"}\n'
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert run(gw, "prune", "--json") == 0  # the lock is free: it works at once
    assert note_ids(gw) == []


def test_busy_disarms_the_deadline_before_output(gw: FakeGateway, short_busy: None) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    holder = sqlite3.connect(db_path(gw), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    armed: list[int] = []
    original = gw.write_out

    def spy(text: str, **kw: Any) -> None:
        armed.append(len(gw.alarms))
        original(text, **kw)

    gw.write_out = spy  # type: ignore[method-assign]
    try:
        assert run(gw, "prune") == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert armed == [0]


def test_busy_is_golden(gw: FakeGateway, short_busy: None) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    holder = sqlite3.connect(db_path(gw), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        run(gw, "prune", "--json")
        check_golden("prune/busy.json", gw.terminal.stdout.encode(), schema="cli/prune")
        run(gw, "prune")
        check_golden("prune/busy.txt", gw.terminal.stdout.encode())
        check_golden("prune/busy-err.txt", gw.terminal.stderr.encode())
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_the_write_lock_is_held_when_the_expired_ids_are_selected(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memoctl.store import Store

    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    real = Store.expired_ids
    outcomes: list[str] = []

    def probe(self: Any, now: int) -> list[int]:
        other = sqlite3.connect(db_path(gw), timeout=0.05, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            outcomes.append("acquired")
            other.execute("ROLLBACK")
        except sqlite3.OperationalError as error:
            outcomes.append(str(error))
        finally:
            other.close()
        return real(self, now)

    monkeypatch.setattr(Store, "expired_ids", probe)
    assert run(gw, "prune") == 0
    assert outcomes == ["database is locked"]
    assert note_ids(gw) == []


# --- the deadline ------------------------------------------------------------------------------


def test_deadline_is_disarmed_before_output(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    armed: list[int] = []
    original = gw.write_out

    def spy(text: str, **kw: Any) -> None:
        armed.append(len(gw.alarms))
        original(text, **kw)

    gw.write_out = spy  # type: ignore[method-assign]
    assert run(gw, "prune") == 0
    assert run(gw, "prune", "--dry-run") == 0
    assert armed == [0, 0]


def test_a_failure_inside_the_transaction_rolls_everything_back(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memoctl import cmd_prune
    from memoctl.store import StoreError

    conn = seed(gw, [dict(id=i, expires=NOW - 1) for i in range(1, 6)], labels=[(1, "a", "b")])
    put_rate(conn, ("r", 0))
    conn.close()
    before = state(gw)
    real = cmd_prune.apply_prune

    def apply_then_time_out(store: Any, now: int) -> Any:
        real(store, now)  # everything is deleted, the commit is not
        raise StoreError("timeout")

    monkeypatch.setattr(cmd_prune, "apply_prune", apply_then_time_out)
    assert run(gw, "prune", "--json") == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "timeout"
    assert state(gw) == before


def test_a_deadline_that_runs_out_during_the_commit_exits_12_with_no_log_record(
    gw: FakeGateway,
) -> None:
    from contextlib import contextmanager

    import memoctl.store as store_mod

    seed(gw, [dict(id=1, expires=NOW - 1)]).close()
    orig = gw.alarm_arm
    holder: dict[str, Any] = {}

    def arm(seconds: float, on_alarm: Any) -> Any:
        holder["alarm"] = orig(seconds, on_alarm)
        return holder["alarm"]

    gw.alarm_arm = arm  # type: ignore[method-assign]
    real_write = store_mod.Store.write

    @contextmanager
    def write(self: Any) -> Any:
        with real_write(self):
            yield
        holder["alarm"].fired = True  # ran out while it committed
        gw.mono += 100

    store_mod.Store.write = write  # type: ignore[method-assign]
    try:
        with pytest.raises(FakeExit) as info:
            run(gw, "prune", "--json")
    finally:
        store_mod.Store.write = real_write  # type: ignore[method-assign]
    assert info.value.code == 12
    assert note_ids(gw) == []  # stored: the new state, never a mix
    assert not (db_path(gw).parent / "log.jsonl").exists()
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "timeout"
    assert run(gw, "prune", "--json") == 0  # a retry finds less to do
    assert gw.terminal.stdout == '{"expired":0,"remaining":0,"dry_run":false}\n'


# --- goldens ----------------------------------------------------------------------------------


def test_goldens(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1, expires=NOW - 1), dict(id=2), dict(id=3, expires=NOW)])
    conn.close()
    run(gw, "prune", "--dry-run", "--json")
    check_golden("prune/dry-run.json", gw.terminal.stdout.encode(), schema="cli/prune")
    run(gw, "prune", "--dry-run")
    check_golden("prune/dry-run.txt", gw.terminal.stdout.encode())
    run(gw, "prune", "--json")
    check_golden("prune/pruned.json", gw.terminal.stdout.encode(), schema="cli/prune")
    seed_more = sqlite3.connect(db_path(gw))
    add_note(seed_more, id=9, expires=NOW - 1)
    seed_more.commit()
    seed_more.close()
    run(gw, "prune")
    check_golden("prune/pruned.txt", gw.terminal.stdout.encode())
    empty = FakeGateway(gw.root.parent / "gw2")
    run(empty, "prune", "--json")
    check_golden("prune/empty.json", empty.terminal.stdout.encode(), schema="cli/prune")
