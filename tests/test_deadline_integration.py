"""The deadline with the store, the CLI and the error table."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from memoctl import deadline as dl
from memoctl.cli import main
from memoctl.cli_args import Command
from memoctl.cli_errors import failure_for
from memoctl.deadline import Deadline
from memoctl.gateway import Gateway
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import Mode, SchemaNewerError, StoreError, open_store
from tests.fake_gateway import FakeExit, FakeGateway
from tests.test_statedir import HomeGateway

ENVELOPE = (
    '{"error": {"code": "failed", "message": "deadline passed", "reason": "timeout", '
    '"details": null, "retryable": true}}\n'
)


class TickingGateway(FakeGateway):
    """The monotonic clock moves ``step`` on every read, so a progress handler that asks 'is
    it time yet' sees time pass without any real waiting."""

    step = 0.01

    def monotonic(self) -> float:
        self.mono += self.step
        return self.mono


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    home = tmp_path / "h"
    home.mkdir()
    return open_state_dir(HomeGateway(home), create=True)


@pytest.fixture
def gw(tmp_path: Path) -> TickingGateway:
    return TickingGateway(tmp_path / "gw")


def init(state: StateDir) -> None:
    s = open_store(state, Mode.INIT)
    assert s is not None
    s.close()


def test_a_long_statement_is_interrupted_as_a_timeout(state: StateDir, gw: TickingGateway) -> None:
    init(state)
    with Deadline(gw) as d:
        d.arm(0.05)
        s = open_store(state, Mode.READ, configure=d.configure_store)
        assert s is not None
        with pytest.raises(StoreError) as e:
            s.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                "SELECT count(*) FROM c"
            ).fetchone()
        assert e.value.reason == "timeout" and e.value.retryable
        s.close()
        gw.alarms[0].at = float("inf")  # the fake timer must not fire at the block's exit


def test_a_planted_huge_schema_is_cut_off_at_the_first_read(
    state: StateDir, gw: TickingGateway
) -> None:
    """The schema parse runs under the progress handler: a timeout, not db_corrupt after
    seconds of parsing."""
    init(state)
    conn = sqlite3.connect(str(state.db_path), isolation_level=None)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO sqlite_schema VALUES ('table', ?, ?, 2, ?)",
        ((f"t{i}", f"t{i}", f"CREATE TABLE t{i}(a)") for i in range(200_000)),
    )
    conn.execute("COMMIT")
    conn.close()
    with Deadline(gw) as d:
        d.arm(0.05)
        with pytest.raises(StoreError) as e:
            open_store(state, Mode.READ, configure=d.configure_store)
        assert e.value.reason == "timeout"
        gw.alarms[0].at = float("inf")


def test_the_progress_handler_is_inert_after_the_disarm(
    state: StateDir, gw: TickingGateway
) -> None:
    """After the disarm the log append and output are not cut off: statements run on."""
    init(state)
    with Deadline(gw) as d:
        d.arm(30)
        s = open_store(state, Mode.WRITE, configure=d.configure_store)
        assert s is not None
        with s.write():
            s.execute("INSERT INTO rate (repo, at) VALUES ('x', 1)")
        d.disarm()
        gw.mono += 1000
        assert s.execute("SELECT count(*) FROM rate").fetchone()[0] == 1
        s.close()


def test_a_deadline_that_passes_during_the_commit_leaves_the_new_state_and_exits_12(
    state: StateDir, gw: TickingGateway
) -> None:
    init(state)
    with pytest.raises(FakeExit) as info, Deadline(gw) as d:
        d.arm(30)
        s = open_store(state, Mode.WRITE)  # no progress handler: the commit cannot be cut
        assert s is not None
        with s.write():
            s.execute("INSERT INTO rate (repo, at) VALUES ('x', 1)")
        gw.alarms[0].fired = True  # the timer ran out while the commit ran
        gw.mono += 100
        s.close()
        d.disarm()  # the writer's disarm point right after the commit
    assert info.value.code == 12
    again = open_store(state, Mode.READ)
    assert again is not None
    assert again.execute("SELECT count(*) FROM rate").fetchone()[0] == 1  # stored, not a mix
    again.close()


# -- the CLI ---------------------------------------------------------------------------------


def _command(run: object) -> Command:
    return Command("probe", "probe", run)  # type: ignore[arg-type]


def test_main_arms_the_deadline_after_parsing_and_disarms_on_return(gw: TickingGateway) -> None:
    seen: list[bool] = []

    def run(args: argparse.Namespace, g: Gateway) -> int:
        seen.append(dl.current().armed)  # type: ignore[union-attr]
        assert len(gw.alarms) == 1 and gw.alarms[0].at - gw.mono <= 30
        return 0

    assert main(["probe"], gw, (_command(run),)) == 0
    assert seen == [True] and gw.alarms == []


def test_main_does_not_arm_for_a_parse_error(gw: TickingGateway) -> None:
    assert main(["--json", "nope"], gw, (_command(lambda a, g: 0),)) == 2
    assert gw.alarms == []


def test_main_arms_context_for_two_seconds(gw: TickingGateway) -> None:
    def run(args: argparse.Namespace, g: Gateway) -> int:
        assert abs(gw.alarms[0].at - gw.mono - 2) < 0.5
        return 0

    cmd = Command("context", "context", run)
    assert main(["context"], gw, (cmd,)) == 0


def test_the_deadline_wins_over_a_command_that_returns_late(gw: TickingGateway) -> None:
    def run(args: argparse.Namespace, g: Gateway) -> int:
        gw.alarms[0].fired = True
        gw.mono += 31
        return 0

    with pytest.raises(FakeExit) as info:
        main(["probe", "--json"], gw, (_command(run),))
    assert info.value.code == 12
    assert gw.terminal.stdout == ENVELOPE


def test_a_command_that_disarms_early_finishes_normally_even_if_time_passes(
    gw: TickingGateway,
) -> None:
    def run(args: argparse.Namespace, g: Gateway) -> int:
        dl.current().disarm()
        gw.mono += 1000
        g.write_out("output\n")
        return 0

    assert main(["probe"], gw, (_command(run),)) == 0
    assert gw.terminal.stdout == "output\n"


def test_main_releases_the_deadline_when_a_command_raises(gw: TickingGateway) -> None:
    def run(args: argparse.Namespace, g: Gateway) -> int:
        raise RuntimeError("bug")

    assert main(["probe"], gw, (_command(run),)) == 1
    assert gw.alarms == [] and dl._CURRENT is None  # pyright: ignore[reportPrivateUsage]


# -- the error table -------------------------------------------------------------------------


def test_store_timeout_maps_to_the_same_failure_as_the_timer() -> None:
    f = failure_for(StoreError("timeout"))
    assert (f.exit_code, f.reason, f.retryable) == (12, "timeout", True)
    assert json.loads(f.envelope()) == json.loads(ENVELOPE)


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [("db_unreadable", False), ("db_locked", True), ("db_corrupt", False)],
)
def test_other_store_failures_map_to_exit_12(reason: str, retryable: bool) -> None:
    f = failure_for(StoreError(reason))
    assert (f.exit_code, f.reason, f.retryable, f.details) == (12, reason, retryable, None)


def test_schema_newer_carries_found_and_supported() -> None:
    f = failure_for(SchemaNewerError(9, 1))
    assert (f.exit_code, f.reason, f.details) == (12, "schema_newer", {"found": 9, "supported": 1})


def test_a_store_error_with_an_unknown_reason_is_internal() -> None:
    assert failure_for(StoreError("bogus")).exit_code == 1


def test_main_reports_a_progress_interrupt_with_the_timeout_envelope(gw: TickingGateway) -> None:
    def run(args: argparse.Namespace, g: Gateway) -> int:
        raise StoreError("timeout")

    assert main(["probe", "--json"], gw, (_command(run),)) == 12
    assert gw.terminal.stdout == ENVELOPE
    assert gw.terminal.stderr == "failed (timeout): deadline passed\n"
