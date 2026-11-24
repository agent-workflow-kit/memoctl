"""``memoctl prune`` as real processes: kills inside the transaction, a real writer holding the
lock (the ``busy`` outcome), concurrent writers, a large store, closed descriptors. Private home
throughout (the password entry is replaced in the child; see ``tests/private_home.py``)."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests import private_home
from tests.schema_check import validate
from tests.test_cmd_delete_process import (
    REPO,
    add,
    env,
    finish,
    log_lines,
    memoctl,
    rows,
    spawn,
)

HOLD = Path(__file__).with_name("prune_hold_wrapper.py")
DB = ".local/state/memoctl/state.db"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def expire(home: Path, *ids: int, at: int = 1) -> None:
    """Make notes expired by editing the row (a note cannot be added with a past expiry)."""
    conn = sqlite3.connect(home / DB)
    try:
        conn.executemany("UPDATE notes SET expires = ? WHERE id = ?", [(at, i) for i in ids])
        conn.commit()
    finally:
        conn.close()


def table(home: Path, name: str) -> list[Any]:
    conn = sqlite3.connect(home / DB)
    try:
        return conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall()  # noqa: S608
    finally:
        conn.close()


def prune_log(home: Path) -> list[dict[str, Any]]:
    return [r for r in log_lines(home) if r["event"] == "prune"]


# -- the real thing -------------------------------------------------------------------------


def test_prune_end_to_end_and_never_touches_live_notes(home: Path) -> None:
    ids = [add(home, f"note {i}") for i in range(5)]
    expire(home, ids[0], ids[3])
    done = memoctl(home, "prune", "--json", "--actor", "auto:memoctl-cron")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"expired": 2, "remaining": 3, "dry_run": False}
    assert [r[0] for r in rows(home)] == [ids[1], ids[2], ids[4]]
    assert [r["note_id"] for r in prune_log(home)] == [ids[0], ids[3]]
    again = memoctl(home, "prune", "--json")
    assert json.loads(again.stdout) == {"expired": 0, "remaining": 3, "dry_run": False}


def test_dry_run_process_matches_and_changes_nothing(home: Path) -> None:
    ids = [add(home, f"note {i}") for i in range(3)]
    expire(home, ids[1])
    before = (table(home, "notes"), table(home, "rate"), log_lines(home))
    dry = memoctl(home, "prune", "--dry-run", "--json")
    assert json.loads(dry.stdout) == {"expired": 1, "remaining": 2, "dry_run": True}
    assert (table(home, "notes"), table(home, "rate"), log_lines(home)) == before


def test_missing_store_creates_nothing(home: Path) -> None:
    done = memoctl(home, "prune", "--json")
    assert done.returncode == 0
    assert json.loads(done.stdout) == {"expired": 0, "remaining": 0, "dry_run": False}
    assert not (home / ".local").exists()


# -- kill inside the transaction ------------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_a_kill_before_the_commit_prunes_nothing(home: Path, sig: int) -> None:
    ids = [add(home, f"note {i}") for i in range(5)]
    expire(home, ids[0], ids[2], ids[4])
    conn = sqlite3.connect(home / DB)
    conn.execute("INSERT INTO rate VALUES ('old', 5)")
    conn.commit()
    conn.close()
    before = (rows(home), table(home, "note_labels"), table(home, "rate"), log_lines(home))
    proc = subprocess.Popen(
        [sys.executable, "-I", str(HOLD), str(home), "prune"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env(home),
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        assert ready and proc.stdout.readline() == b"READY\n"
        proc.send_signal(sig)  # exactly the handle started here
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == -sig
    after = (rows(home), table(home, "note_labels"), table(home, "rate"), log_lines(home))
    assert after == before  # the uncommitted deletes are gone
    done = memoctl(home, "prune", "--json")  # no stale lock; the same command now works
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"expired": 3, "remaining": 2, "dry_run": False}
    assert table(home, "rate") != before[2]


# -- busy ------------------------------------------------------------------------------------


def test_a_real_writer_holding_the_lock_is_the_busy_outcome(home: Path) -> None:
    ids = [add(home, f"note {i}") for i in range(2)]
    expire(home, ids[0])
    holder = sqlite3.connect(home / DB, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        done = memoctl(home, "prune", "--json", "--quiet")
        waited = time.monotonic() - started
        assert done.returncode == 0, done.stderr
        assert done.stdout == b'{"outcome":"busy"}\n'
        assert done.stderr == (
            b"warning (prune_busy): another writer held the lock; nothing was pruned\n"
        )
        assert 4 <= waited < 20  # the 5 s busy timeout, not the 30 s deadline
        dry = memoctl(home, "prune", "--dry-run", "--json")  # a reader never waits
        assert json.loads(dry.stdout) == {"expired": 1, "remaining": 1, "dry_run": True}
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert [r[0] for r in rows(home)] == ids
    assert prune_log(home) == []
    done = memoctl(home, "prune", "--json")
    assert json.loads(done.stdout) == {"expired": 1, "remaining": 1, "dry_run": False}


# -- concurrent writers ---------------------------------------------------------------------


def test_concurrent_prunes_remove_each_note_once(home: Path) -> None:
    ids = [add(home, f"note {i}") for i in range(6)]
    expire(home, *ids[:4])
    procs = [spawn(home, "prune", "--json") for _ in range(3)]
    results = finish(procs)
    assert all(code == 0 for code, _ in results)
    total = sum(json.loads(out)["expired"] for _, out in results)
    assert total == 4
    assert [r[0] for r in rows(home)] == ids[4:]
    assert sorted(r["note_id"] for r in prune_log(home)) == ids[:4]


def test_prune_against_adds_never_loses_a_live_note(home: Path) -> None:
    keep = [add(home, f"keep {i}") for i in range(3)]
    old = [add(home, f"old {i}") for i in range(3)]
    expire(home, *old)
    for round_ in range(3):
        body = home.parent / f"body{round_}.txt"
        body.write_text(f"fresh {round_}\n")
        p = spawn(home, "prune", "--json")
        a = spawn(
            home, "add", "--repo", REPO, "--topic", "u", "--body-file", str(body), "--json"
        )  # fmt: skip
        (cp, _), (ca, out) = finish([p, a])
        assert cp == 0 and ca == 0
        assert json.loads(out)["created"] is True
    present = {r[0] for r in rows(home)}
    assert set(keep) <= present and not set(old) & present
    assert len(present) == 3 + 3  # keeps plus the three fresh notes


def test_prune_against_delete_is_all_or_nothing_per_command(home: Path) -> None:
    ids = [add(home, f"note {i}") for i in range(4)]
    expire(home, ids[0], ids[1])
    p = spawn(home, "prune", "--json")
    d = spawn(home, "delete", "--yes", "--json", str(ids[0]), str(ids[3]))
    (cp, _), (cd, _) = finish([p, d])
    assert cp == 0 and cd in (0, 10)
    present = [r[0] for r in rows(home)]
    assert ids[2] in present and ids[0] not in present and ids[1] not in present
    assert (ids[3] in present) == (cd == 10)  # a delete that failed deleted nothing at all


# -- a large store --------------------------------------------------------------------------


def test_a_large_store_prunes_within_the_deadline(home: Path) -> None:
    add(home, "seed")  # creates and initializes the store
    conn = sqlite3.connect(home / DB, isolation_level=None)
    conn.execute("BEGIN")
    sha = "a" * 64
    conn.executemany(
        "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (?, 't', 'unknown', 'hello', ?, 5, 1, ?)",
        ((REPO, sha, 2 if i % 2 else 2**40) for i in range(100_000)),
    )
    conn.executemany("INSERT INTO rate VALUES ('r', ?)", ((i,) for i in range(50_000)))
    conn.execute("COMMIT")
    conn.close()
    pruned_ids = [r[0] for r in table(home, "notes") if r[8] == 2]
    assert len(pruned_ids) == 50_000
    dry = memoctl(home, "prune", "--dry-run", "--json")
    assert json.loads(dry.stdout) == {"expired": 50_000, "remaining": 50_001, "dry_run": True}
    started = time.monotonic()
    done = memoctl(home, "prune", "--json", "--quiet")
    elapsed = time.monotonic() - started
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"expired": 50_000, "remaining": 50_001, "dry_run": False}
    assert len(table(home, "rate")) == 1  # the seed add's own row, from today
    assert done.stderr == b""  # every append worked; the log rotated on its own size cap
    state_dir = (home / DB).parent
    kept = [  # both kept files
        json.loads(line)
        for name in ("log.jsonl.1", "log.jsonl")
        if (state_dir / name).exists()
        for line in (state_dir / name).read_text().splitlines()
        if json.loads(line)["event"] == "prune"
    ]
    # older records may have rotated away; the newest must be there
    assert kept
    assert all(r["event"] == "prune" and r["repo"] == REPO for r in kept)
    for r in kept:
        validate("log", r)
    assert max(r["note_id"] for r in kept) == max(pruned_ids)
    assert elapsed < 30, elapsed  # the command's own deadline is 30 s


# -- descriptors ----------------------------------------------------------------------------


def test_closed_standard_descriptors(home: Path) -> None:
    ids = [add(home, "x"), add(home, "y")]
    expire(home, ids[0])
    close_all = (
        "import os, sys\n"
        "for fd in (0, 1, 2):\n"
        "    os.close(fd)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", close_all, *private_home.argv(home, "prune")],
        env=env(home),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    assert proc.wait(timeout=60) == 0
    assert [r[0] for r in rows(home)] == [ids[1]]


def test_closed_stdout_pipe_still_prunes_and_exits_0(home: Path) -> None:
    ids = [add(home, "x"), add(home, "y")]
    expire(home, ids[0])
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        proc = subprocess.Popen(
            private_home.argv(home, "prune"), env=env(home),
            stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        assert proc.wait(timeout=60) == 0
    finally:
        os.close(write_end)
    assert [r[0] for r in rows(home)] == [ids[1]]
    assert len(prune_log(home)) == 1
