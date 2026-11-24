"""``memoctl add`` under concurrency, as real processes (issue #20).

Private home throughout (the password entry is replaced in the child, see
``tests/add_child_wrapper.py``); every child gets a ``PATH`` that cannot find a real ``crontab``.
Every child is a ``Popen`` handle started and reaped here; a child still running at the end is
stopped through its own handle only. Process counts stay small (at most 12) and nothing loops.

Marked ``process`` (real child processes). The store-wide rate limit
(``adds_per_window_total``) is covered here too, by the same invariant as the per-repo one.

Already covered elsewhere (not repeated here): two same-body adds and two supersedes of one
target (``test_cmd_add_supersede.py``), two racers for the last slot
(``test_cmd_add_limits.py``), twelve-process log appends and rotation, a stuck log lock holder
(``test_logfile.py``), delete and prune against add and each other
(``test_cmd_delete_process.py``, ``test_cmd_prune_process.py``).
"""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from memoctl.logfile import LOG_NAME, ROTATE_BYTES, ROTATED_NAME
from tests.child_env import child_path

REPO = "example.com/team/app"
WRAPPER = Path(__file__).with_name("add_child_wrapper.py")
RESULT_TIMEOUT = 120

pytestmark = pytest.mark.process


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def env(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": child_path(home.parent),
        "LC_ALL": "C",
        **extra,
    }


def state(home: Path) -> Path:
    return home / ".local" / "state" / "memoctl"


def write_config(home: Path, text: str) -> None:
    cfg = home / ".config" / "memoctl"
    cfg.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = cfg / "config.yaml"
    path.write_text(text)
    path.chmod(0o600)


def spawn(home: Path, *args: str, body: str | None = None, **extra: str) -> subprocess.Popen[bytes]:
    """A ``memoctl`` child through the test wrapper; the body (when given) is on its stdin."""
    stdin: Any = subprocess.DEVNULL
    if body is not None:  # a file, so no pipe can stall the child or be left half open
        bodies = home.parent / "bodies"
        bodies.mkdir(exist_ok=True)
        path = bodies / f"{len(os.listdir(bodies))}.txt"
        path.write_text(body)
        stdin = path.open("rb")
    try:
        return subprocess.Popen(
            [sys.executable, "-I", str(WRAPPER), str(home), *args],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env(home, **extra),
        )
    finally:
        if body is not None:
            stdin.close()


def add_args(*extra: str, repo: str = REPO, topic: str = "t") -> tuple[str, ...]:
    common = ("--body-file", "-", "--json", "--kind", "implement")
    return ("add", "--repo", repo, "--topic", topic, *common, *extra)


class Procs:
    """Children started here; reaped through their handles, killed only through them."""

    def __init__(self) -> None:
        self.items: list[subprocess.Popen[bytes]] = []

    def __enter__(self) -> Procs:
        return self

    def __exit__(self, *exc: object) -> None:
        for proc in self.items:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None and not stream.closed:
                    stream.close()

    def start(self, *args: str, home: Path, body: str | None = None, **extra: str) -> int:
        self.items.append(spawn(home, *args, body=body, **extra))
        return len(self.items) - 1

    def release(self, sync: Path, count: int | None = None) -> None:
        """Wait until the children are imported and waiting, then let them all go at once."""
        want = len(self.items) if count is None else count
        end = time.monotonic() + 60
        while len(os.listdir(sync)) < want and time.monotonic() < end:
            time.sleep(0.01)
        assert len(os.listdir(sync)) >= want, "children never became ready"
        (sync / "go").touch()

    def results(self) -> list[tuple[int, bytes, bytes]]:
        out = []
        for proc in self.items:
            stdout, stderr = proc.communicate(timeout=RESULT_TIMEOUT)
            out.append((proc.returncode, stdout, stderr))
        return out


def db(home: Path, sql: str) -> list[Any]:
    conn = sqlite3.connect(state(home) / "state.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def log_lines(home: Path) -> list[dict[str, Any]]:
    out = []
    for name in (ROTATED_NAME, LOG_NAME):
        path = state(home) / name
        if path.exists():
            for raw in path.read_bytes().split(b"\n"):
                if raw.startswith(b'{"ts"'):
                    out.append(json.loads(raw))
    return out


def sync_dir(home: Path) -> Path:
    path = home.parent / "sync"
    path.mkdir(exist_ok=True)
    return path


def assert_intact(home: Path) -> None:
    assert db(home, "PRAGMA integrity_check") == [("ok",)]
    assert db(home, "PRAGMA foreign_key_check") == []


# -- first run: the state directory, database and log are created by racing writers -------------


@pytest.mark.parametrize("umask", [0o022, 0o077, 0o277, 0o777], ids=oct)
def test_eight_first_time_processes_create_the_state_directory_under_any_umask(
    home: Path, umask: int
) -> None:
    """The hostile umasks leave a just-made level without its owner bits until the creator sets
    0700; a rival must wait for that, not fail with a raw ``PermissionError`` or an
    ``unsafe_state`` refusal. Every creator sleeps 40 ms between ``mkdir`` and ``chmod``
    (``ADDTEST_CHMOD_DELAY_MS``), so the window is wide and a rival sees it reliably. The
    deterministic proof of the wait is in ``test_statedir.py``; this is the process-level check."""
    sync = sync_dir(home)
    with Procs() as procs:
        for _ in range(8):
            procs.start(
                home=home, ADDTEST_SYNC=str(sync), ADDTEST_STATEDIR_ONLY="1",
                ADDTEST_UMASK=format(umask, "o"), ADDTEST_CHMOD_DELAY_MS="40",
            )  # fmt: skip
        procs.release(sync)
        results = procs.results()
    assert [(code, err) for code, _, err in results] == [(0, b"")] * 8
    for level in (home / ".local", home / ".local" / "state", state(home)):
        assert stat.S_IMODE(level.lstat().st_mode) == 0o700


@pytest.mark.parametrize("umask", [0o022, 0o077], ids=oct)
def test_eight_first_time_writers_create_the_store_once(home: Path, umask: int) -> None:
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):
            procs.start(
                *add_args(), home=home, body=f"first {i}\n",
                ADDTEST_SYNC=str(sync), ADDTEST_UMASK=format(umask, "o"),
            )  # fmt: skip
        procs.release(sync)
        results = procs.results()
    assert [(code, err) for code, _, err in results] == [(0, b"")] * 8
    ids = sorted(json.loads(out)["id"] for _, out, _ in results)
    assert ids == list(range(1, 9))
    for level in (home / ".local", home / ".local" / "state", state(home)):
        assert stat.S_IMODE(level.lstat().st_mode) == 0o700
    for name in ("state.db", LOG_NAME):
        assert stat.S_IMODE((state(home) / name).lstat().st_mode) == 0o600
    assert db(home, "SELECT count(*) FROM notes") == [(8,)]
    assert len(log_lines(home)) == 8
    assert_intact(home)


# -- eight writers and four readers ---------------------------------------------------------------


def seed(home: Path, body: str = "seed", *extra: str, repo: str = REPO) -> int:
    done = subprocess.run(
        [sys.executable, "-I", str(WRAPPER), str(home), *add_args(*extra, repo=repo)],
        input=body.encode(), capture_output=True, env=env(home), timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return int(json.loads(done.stdout)["id"])


def test_eight_writers_and_four_readers_lose_no_add(home: Path) -> None:
    seed(home)
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):
            procs.start(*add_args(), home=home, body=f"note {i}\n", ADDTEST_SYNC=str(sync))
        for i in range(4):
            reader = ("list", "--json") if i % 2 else ("context", "--repo", REPO, "--json")
            procs.start(*reader, home=home, ADDTEST_SYNC=str(sync))
        procs.release(sync)
        results = procs.results()
    writers, readers = results[:8], results[8:]
    assert [(code, err) for code, _, err in writers] == [(0, b"")] * 8
    docs = [json.loads(out) for _, out, _ in writers]
    assert all(doc["created"] is True for doc in docs)
    assert sorted(doc["id"] for doc in docs) == list(range(2, 10))  # dense, unique
    for code, out, err in readers:  # a reader never waits on or fails because of a writer
        assert (code, err) == (0, b""), out
        doc = json.loads(out)
        if "notes" in doc:
            assert 1 <= doc["total"] <= 9
            assert len({n["id"] for n in doc["notes"]}) == doc["shown"]
        else:
            assert 0 <= doc["eligible"] <= 9
    assert db(home, "SELECT count(*), count(DISTINCT body_sha256) FROM notes") == [(9, 9)]
    assert db(home, "SELECT count(*) FROM rate") == [(9,)]
    records = log_lines(home)
    assert sorted(r["note_id"] for r in records) == list(range(1, 10))  # one per note, none torn
    assert_intact(home)


def test_eight_writers_of_one_body_insert_once(home: Path) -> None:
    sync = sync_dir(home)
    with Procs() as procs:
        for _ in range(8):
            procs.start(*add_args(), home=home, body="same\n", ADDTEST_SYNC=str(sync))
        procs.release(sync)
        results = procs.results()
    assert [(code, err) for code, _, err in results] == [(0, b"")] * 8
    docs = [json.loads(out) for _, out, _ in results]
    assert sorted(doc["created"] for doc in docs) == [False] * 7 + [True]
    assert {doc["id"] for doc in docs} == {1}
    assert db(home, "SELECT count(*) FROM notes") == [(1,)]
    assert len(log_lines(home)) == 1


# -- limits under concurrency ---------------------------------------------------------------------


def refusals(results: list[tuple[int, bytes, bytes]]) -> list[dict[str, Any]]:
    out = []
    for code, stdout, _ in results:
        if code != 0:
            assert code == 20, stdout
            out.append(json.loads(stdout)["error"])
    return out


def test_eight_racers_never_exceed_live_per_repo(home: Path) -> None:
    write_config(home, "limits:\n  live_per_repo: 5\n  total: 50\n")
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):
            procs.start(*add_args(), home=home, body=f"r {i}\n", ADDTEST_SYNC=str(sync))
        procs.release(sync)
        results = procs.results()
    assert sorted(code for code, _, _ in results) == [0] * 5 + [20] * 3
    assert {e["reason"] for e in refusals(results)} == {"repo_full"}
    assert db(home, "SELECT count(*) FROM notes") == [(5,)]
    assert db(home, "SELECT count(*) FROM rate") == [(5,)]
    assert len(log_lines(home)) == 5
    assert_intact(home)


def test_racers_never_exceed_total(home: Path) -> None:
    write_config(home, "limits:\n  live_per_repo: 6\n  total: 6\n")
    for i in range(3):
        seed(home, f"seed {i}", repo=f"example.com/team/seed{i}")
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(6):  # each in its own repo, so only the store limit can refuse
            procs.start(
                *add_args(repo=f"example.com/team/r{i}"), home=home, body=f"r {i}\n",
                ADDTEST_SYNC=str(sync),
            )  # fmt: skip
        procs.release(sync)
        results = procs.results()
    assert sorted(code for code, _, _ in results) == [0] * 3 + [20] * 3
    assert {e["reason"] for e in refusals(results)} == {"store_full"}
    assert db(home, "SELECT count(*) FROM notes") == [(6,)]
    assert_intact(home)


def test_a_store_at_exactly_total_refuses_every_racer_and_changes_nothing(home: Path) -> None:
    write_config(home, "limits:\n  live_per_repo: 3\n  total: 3\n")
    for i in range(3):
        seed(home, f"seed {i}", "--label", f"k=v{i}", repo=f"example.com/team/seed{i}")
    before = (
        db(home, "SELECT id, repo, body_sha256, superseded_by FROM notes ORDER BY id"),
        db(home, "SELECT * FROM note_labels ORDER BY 1, 2, 3"),
        db(home, "SELECT * FROM rate ORDER BY 1, 2"),
        (state(home) / LOG_NAME).read_bytes(),
    )
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(4):  # other repos than the seeds, so only the store limit can refuse
            procs.start(
                *add_args(repo=f"example.com/team/r{i}"), home=home, body=f"over {i}\n",
                ADDTEST_SYNC=str(sync),
            )  # fmt: skip
        procs.release(sync)
        results = procs.results()
    assert [code for code, _, _ in results] == [20] * 4
    for error in refusals(results):
        assert (error["reason"], error["details"], error["retryable"]) == (
            "store_full", {"limit": 3}, False,
        )  # fmt: skip
    after = (
        db(home, "SELECT id, repo, body_sha256, superseded_by FROM notes ORDER BY id"),
        db(home, "SELECT * FROM note_labels ORDER BY 1, 2, 3"),
        db(home, "SELECT * FROM rate ORDER BY 1, 2"),
        (state(home) / LOG_NAME).read_bytes(),
    )
    assert after == before
    assert_intact(home)


def assert_rate_rows_respect(
    home: Path, limit: int, *, per_repo: bool, window: int = 600, writers: int = 8
) -> int:
    """The real invariant, from the ``rate`` rows in commit order (rowid): a row was admitted
    only if fewer than ``limit`` earlier rows (of its repo when ``per_repo``) lay in the window
    ending at its own ``at`` (rows dated after it are not counted, as ``count_rate`` does). With
    every ``at`` equal nothing can straddle a second, so exactly ``limit`` rows exist; otherwise
    (the documented straddle, Residual risks, Clock) between ``limit`` and ``limit + writers``.
    Returns the number of rows."""
    rows = db(home, "SELECT repo, at FROM rate ORDER BY rowid")
    for k, (repo, at) in enumerate(rows):
        counted = [
            1
            for other_repo, other_at in rows[:k]
            if (not per_repo or other_repo == repo) and at - window < other_at <= at
        ]
        assert len(counted) < limit, f"row {k} was admitted with {len(counted)} counted before it"
    if len({at for _, at in rows}) == 1:
        assert len(rows) == limit
    else:
        assert limit <= len(rows) <= limit + writers
    return len(rows)


def check_rate_results(results: list[tuple[int, bytes, bytes]], limit: int, scope: str) -> int:
    ok = sum(1 for code, _, _ in results if code == 0)
    errors = refusals(results)
    assert len(errors) == len(results) - ok
    for error in errors:
        assert (error["reason"], error["retryable"]) == ("rate_limited", True)
        assert error["details"]["retry_after_s"] >= 1
        assert (error["details"]["limit"], error["details"]["scope"]) == (limit, scope)
    return ok


def test_racers_never_exceed_the_per_repo_rate_limit(home: Path) -> None:
    write_config(home, "limits:\n  adds_per_window: 3\n  live_per_repo: 50\n  total: 50\n")
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):
            procs.start(*add_args(), home=home, body=f"r {i}\n", ADDTEST_SYNC=str(sync))
        procs.release(sync)
        results = procs.results()
    ok = check_rate_results(results, 3, "repo")
    assert assert_rate_rows_respect(home, 3, per_repo=True) == ok
    assert db(home, "SELECT count(*) FROM notes") == [(ok,)]


def test_racers_never_exceed_the_store_wide_rate_limit(home: Path) -> None:
    write_config(
        home,
        "limits:\n  adds_per_window: 3\n  adds_per_window_total: 3\n"
        "  live_per_repo: 50\n  total: 50\n",
    )
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):  # each in its own repo, so only the store-wide rate can refuse
            procs.start(
                *add_args(repo=f"example.com/team/r{i}"), home=home, body=f"r {i}\n",
                ADDTEST_SYNC=str(sync),
            )  # fmt: skip
        procs.release(sync)
        results = procs.results()
    ok = check_rate_results(results, 3, "store")
    assert assert_rate_rows_respect(home, 3, per_repo=False) == ok
    assert db(home, "SELECT count(*) FROM notes") == [(ok,)]


# -- a writer holding the lock, and a writer killed ------------------------------------------------


def wait_ready(proc: subprocess.Popen[bytes]) -> None:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 60)
    assert ready and proc.stdout.readline() == b"READY\n"


def test_a_busy_writer_is_db_locked_and_retryable_and_a_retry_after_release_succeeds(
    home: Path,
) -> None:
    seed(home)
    with Procs() as procs:
        holder = procs.start(
            *add_args(), home=home, body="held\n", ADDTEST_HOLD="after_insert"
        )  # fmt: skip
        wait_ready(procs.items[holder])  # it holds the write lock now
        waiter = procs.start(*add_args(), home=home, body="waits\n", ADDTEST_BUSY_MS="100")
        stdout, _ = procs.items[waiter].communicate(timeout=RESULT_TIMEOUT)
        error = json.loads(stdout)["error"]
        assert procs.items[waiter].returncode == 12
        assert (error["code"], error["reason"], error["retryable"]) == ("failed", "db_locked", True)
        assert db(home, "SELECT count(*) FROM notes") == [(1,)]
        # the holder is killed (its own handle) inside the transaction: nothing is left of it
        procs.items[holder].send_signal(signal.SIGKILL)
        procs.items[holder].wait(timeout=30)
        assert procs.items[holder].returncode == -signal.SIGKILL
    assert db(home, "SELECT count(*) FROM notes") == [(1,)]
    assert db(home, "SELECT count(*) FROM note_labels") == [(0,)]
    assert db(home, "SELECT count(*) FROM rate") == [(1,)]
    assert len(log_lines(home)) == 1
    retry = subprocess.run(
        [sys.executable, "-I", str(WRAPPER), str(home), *add_args()],
        input=b"waits\n", capture_output=True, env=env(home), timeout=60,
    )  # fmt: skip
    doc = json.loads(retry.stdout)
    assert (retry.returncode, doc["id"], doc["created"]) == (0, 2, True)
    assert_intact(home)


def test_a_superseding_add_killed_after_the_commit_is_retried_to_the_same_id(home: Path) -> None:
    target = seed(home, "old")
    args = add_args("--supersedes", str(target))
    with Procs() as procs:
        first = procs.start(*args, home=home, body="new\n", ADDTEST_HOLD="after_commit")
        wait_ready(procs.items[first])  # committed; the output and the log record are lost
        procs.items[first].send_signal(signal.SIGKILL)
        procs.items[first].wait(timeout=30)
    assert db(home, "SELECT id, superseded_by FROM notes ORDER BY id") == [(1, 2), (2, None)]
    assert len(log_lines(home)) == 1  # the killed add never logged
    retry = subprocess.run(
        [sys.executable, "-I", str(WRAPPER), str(home), *args],
        input=b"new\n", capture_output=True, env=env(home), timeout=60,
    )  # fmt: skip
    doc = json.loads(retry.stdout)
    assert (retry.returncode, doc["id"], doc["created"], doc["supersedes"]) == (0, 2, False, 1)
    assert db(home, "SELECT count(*) FROM notes") == [(2,)]
    assert db(home, "SELECT count(*) FROM rate") == [(2,)]
    assert_intact(home)


# -- log rotation under concurrent adds ------------------------------------------------------------


def test_adds_racing_a_log_rotation_keep_every_record(home: Path) -> None:
    """A smoke test: the writers may or may not overlap at the rotation. The deterministic proof
    is the barrier test below."""
    seed(home)
    log = state(home) / LOG_NAME
    first = log.read_bytes()
    with log.open("wb") as f:  # a log at the cap: the next append rotates it
        f.write(first)
        f.truncate(ROTATE_BYTES)
    log.chmod(0o600)
    sync = sync_dir(home)
    with Procs() as procs:
        for i in range(8):
            procs.start(*add_args(), home=home, body=f"note {i}\n", ADDTEST_SYNC=str(sync))
        procs.release(sync)
        results = procs.results()
    assert [(code, err) for code, _, err in results] == [(0, b"")] * 8
    rotated = state(home) / ROTATED_NAME
    assert rotated.stat().st_size >= ROTATE_BYTES
    ids = sorted(r["note_id"] for r in log_lines(home))
    assert ids == list(range(1, 10))  # every record once, in the log or its one rotation
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert db(home, "SELECT count(*) FROM notes") == [(9,)]


def test_two_writers_that_both_decided_to_rotate_lose_no_record(home: Path) -> None:
    """Both writers see the log at the cap and decide to rotate; ``ADDTEST_ROTATE_BARRIER`` holds
    each until the other has decided too, before either takes the lock. The first rotates; the
    second must find the log small again under the lock (the size re-check) and not rename the
    fresh log over the one rotation, which would lose the seed record and the first writer's."""
    seed(home)
    log = state(home) / LOG_NAME
    first = log.read_bytes()
    with log.open("wb") as f:
        f.write(first)
        f.truncate(ROTATE_BYTES)
    log.chmod(0o600)
    barrier = home.parent / "rotate"
    barrier.mkdir()
    with Procs() as procs:
        for i in range(2):
            procs.start(
                *add_args(), home=home, body=f"note {i}\n",
                ADDTEST_ROTATE_BARRIER=str(barrier), ADDTEST_ROTATE_COUNT="2",
            )  # fmt: skip
        results = procs.results()
    assert [(code, err) for code, _, err in results] == [(0, b"")] * 2
    assert len(os.listdir(barrier)) == 2  # both reached the rotation decision
    assert (state(home) / ROTATED_NAME).stat().st_size >= ROTATE_BYTES
    assert sorted(r["note_id"] for r in log_lines(home)) == [1, 2, 3]
