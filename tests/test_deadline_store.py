"""The deadline against a real store: lock waits, an interrupted COMMIT, a raising callback.

Real signals and real locks, in a child process or in this one with a gateway whose exit raises
(so the test process survives). Bounds are generous: they tell a wait that ends at the deadline
from one that runs to the 5 s busy timeout, nothing finer.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

from memoctl import store as store_module
from memoctl.deadline import Deadline
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import Mode, StoreError, open_store
from tests.fake_gateway import FakeGateway
from tests.test_statedir import HomeGateway

STATUS = b"failed (timeout): deadline passed\n"


class Exit(BaseException):
    def __init__(self, code: int) -> None:
        self.code = code


class RaisingGateway(HomeGateway):
    """Real alarm and clock; the exit raises instead of leaving, and the report is recorded."""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.err: list[str] = []

    def exit_now(self, code: int) -> NoReturn:
        raise Exit(code)

    def write_out(self, text: str, *, bound: float | None = None) -> None:
        pass

    def write_err(self, text: str, *, bound: float | None = None) -> None:
        self.err.append(text)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "h"
    path.mkdir()
    return path


@pytest.fixture
def state(home: Path) -> StateDir:
    st = open_state_dir(HomeGateway(home), create=True)
    s = open_store(st, Mode.INIT)
    assert s is not None
    s.close()
    return st


@pytest.fixture
def hold(state: StateDir) -> Iterator[sqlite3.Connection]:
    """Another connection that holds the write lock until the test ends."""
    conn = sqlite3.connect(str(state.db_path), isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 0")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    finally:
        conn.close()


# -- R9: a lock wait ends with the deadline --------------------------------------------------


def test_a_lock_wait_ends_at_the_deadline_not_at_the_busy_timeout(
    state: StateDir, home: Path, hold: sqlite3.Connection
) -> None:
    gw = RaisingGateway(home)
    start = time.monotonic()
    with pytest.raises(Exit) as info, Deadline(gw) as d:
        d.arm(0.5)
        s = open_store(state, Mode.WRITE, configure=d.configure_store)
        assert s is not None
        with s.write():  # BEGIN IMMEDIATE waits on the held lock
            pytest.fail("the lock is held")
    assert info.value.code == 12 and gw.err == [STATUS.decode()]
    assert time.monotonic() - start < 0.5 + 3  # not the 5 s busy timeout


def test_the_busy_timeout_is_capped_to_the_time_left_and_restored_without_a_deadline(
    state: StateDir, tmp_path: Path
) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with Deadline(gw) as d:
        d.arm(1)
        gw.advance_mono(0.5)
        s = open_store(state, Mode.WRITE, configure=d.configure_store)
        assert s is not None
        with s.write():
            assert s.execute("PRAGMA busy_timeout").fetchone()[0] == 500 + 10
        gw.advance_mono(0.25)
        with s.write():
            assert s.execute("PRAGMA busy_timeout").fetchone()[0] == 250 + 10
        d.disarm()
        with s.write():
            assert s.execute("PRAGMA busy_timeout").fetchone()[0] == store_module.WRITE_BUSY_MS
        s.close()


def test_a_configured_timeout_below_the_time_left_wins(state: StateDir, tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with Deadline(gw) as d:
        d.arm(30)
        s = open_store(state, Mode.READ, configure=d.configure_store)  # 200 ms
        assert s is not None
        with s.read():
            assert s.execute("PRAGMA busy_timeout").fetchone()[0] == store_module.READ_BUSY_MS
        s.close()


def test_the_wal_switch_waits_are_capped_too(state: StateDir) -> None:
    """Each attempt's wait is the smaller of its share and the time left."""
    seen: list[str] = []

    class Conn:
        def execute(self, sql: str) -> object:
            seen.append(sql)
            raise sqlite3.OperationalError("stop")  # ends the first attempt's PRAGMA

    with pytest.raises(sqlite3.OperationalError):
        store_module._ensure_wal(Conn(), 5000, lambda ms: min(ms, 30))  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    assert seen[0] == "PRAGMA busy_timeout = 30"


# -- R6: acceptance criterion 1, with a real lock holder and a real child ----------------------

CHILD = """\
import os, sys
from pathlib import Path
from memoctl.cli_errors import failure_for
from memoctl.deadline import Deadline
from memoctl.gateway import RealGateway
from memoctl.statedir import open_state_dir
from memoctl.store import Mode, StoreError, open_store
class GW(RealGateway):
    def home(self):
        return Path(sys.argv[1])
gw = GW()
state = open_state_dir(gw, create=False)
with Deadline(gw) as d:
    d.arm(1.0)
    try:
        s = open_store(state, Mode.WRITE, configure=d.configure_store)
        with s.write():
            pass
    except StoreError as exc:
        f = failure_for(exc)
        gw.write_err(f.status_line())
        os._exit(f.exit_code)
"""


def test_a_writer_waiting_on_a_held_lock_ends_with_exit_12_timeout(
    state: StateDir, home: Path, tmp_path: Path, hold: sqlite3.Connection
) -> None:
    """Without the cap the write waits the 5 s busy timeout and reports ``db_locked``."""
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(home)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    try:
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    assert (p.returncode, out, err) == (12, b"", STATUS)


# -- R7: an interrupted COMMIT rolls back -------------------------------------------------------


def test_an_interrupted_commit_is_rolled_back_and_reported_as_a_timeout(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)  # every VM step asks
    s = open_store(state, Mode.WRITE)
    assert s is not None
    expired = [False]
    s.set_progress_handler(lambda: expired[0])
    with pytest.raises(StoreError) as e, s.write():
        s.execute("INSERT INTO rate (repo, at) VALUES ('x', 1)")
        expired[0] = True  # the deadline passes before the COMMIT
    assert e.value.reason == "timeout"
    # The transaction is really gone: the write lock is free for another connection at once.
    other = sqlite3.connect(str(state.db_path), isolation_level=None, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()
    s.close()
    again = open_store(state, Mode.READ)
    assert again is not None
    assert again.execute("SELECT count(*) FROM rate").fetchone()[0] == 0  # the old state
    again.close()


# -- R10: a bug in the callback is not a timeout -----------------------------------------------


def test_a_raising_progress_callback_surfaces_as_that_error_not_a_timeout(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)
    s = open_store(state, Mode.READ)
    assert s is not None

    def broken() -> bool:
        raise ZeroDivisionError("bug in the callback")

    s.set_progress_handler(broken)
    with pytest.raises(ZeroDivisionError):
        s.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 1000) "
            "SELECT count(*) FROM c"
        ).fetchone()
    s.close()


def test_a_raising_callback_during_open_surfaces_as_that_error(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)

    def broken() -> bool:
        raise ZeroDivisionError("bug in the callback")

    with pytest.raises(ZeroDivisionError):
        open_store(state, Mode.READ, configure=lambda store: store.set_progress_handler(broken))


def test_the_timeout_still_maps_when_the_callback_is_fine(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)
    s = open_store(state, Mode.READ)
    assert s is not None
    s.set_progress_handler(lambda: True)
    with pytest.raises(StoreError) as e:
        s.execute("SELECT 1 UNION ALL SELECT 2").fetchall()
    assert e.value.reason == "timeout"
    s.close()


# -- the handler outlives a failed transaction; a stale callback error does not leak ----------

LONG = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 1000) "
    "SELECT count(*) FROM c"
)


def test_the_progress_handler_stays_installed_after_a_failed_transaction(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)
    s = open_store(state, Mode.WRITE)
    assert s is not None
    expired = [False]
    s.set_progress_handler(lambda: expired[0])
    with pytest.raises(StoreError), s.write():
        s.execute("INSERT INTO rate (repo, at) VALUES ('x', 1)")
        raise StoreError("limit")
    expired[0] = True  # the deadline passes after the failed transaction
    with pytest.raises(StoreError) as e:
        s.execute(LONG).fetchall()
    assert e.value.reason == "timeout"
    s.close()


def test_a_stale_callback_error_is_not_attributed_to_a_later_interrupt(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PROGRESS_STEPS", 1)
    s = open_store(state, Mode.READ)
    assert s is not None
    s.set_progress_handler(lambda: True)
    s._callback_error = ZeroDivisionError("stale")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(StoreError) as e:
        s.execute(LONG).fetchall()
    assert e.value.reason == "timeout"
    s.close()
