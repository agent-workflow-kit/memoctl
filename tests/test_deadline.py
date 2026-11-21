"""The command deadline on the fake gateway: no real signal, no wall clock."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from memoctl import deadline as dl
from memoctl.deadline import Deadline, DeadlineError
from tests.fake_gateway import FakeExit, FakeGateway

ENVELOPE = (
    '{"error": {"code": "failed", "message": "deadline passed", "reason": "timeout", '
    '"details": null, "retryable": true}}\n'
)
STATUS = "failed (timeout): the deadline passed\n"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path)


@pytest.fixture(autouse=True)
def no_leftover_deadline() -> Iterator[None]:
    yield
    assert dl._CURRENT is None  # pyright: ignore[reportPrivateUsage]


def test_fires_with_the_prebuilt_lines_and_exit_12(gw: FakeGateway) -> None:
    with pytest.raises(FakeExit) as info, Deadline(gw, json_mode=True) as d:
        d.arm(5)
        gw.advance_mono(4.9)
        assert gw.exits == []
        gw.advance_mono(0.2)
    assert info.value.code == 12
    assert gw.terminal.stdout == ENVELOPE
    assert gw.terminal.stderr == STATUS
    assert json.loads(gw.terminal.stdout)["error"]["retryable"] is True


def test_plain_mode_prints_nothing_on_stdout(gw: FakeGateway) -> None:
    with pytest.raises(FakeExit), Deadline(gw) as d:
        d.arm(1)
        gw.advance_mono(1)
    assert gw.terminal.stdout == ""
    assert gw.terminal.stderr == STATUS


def test_disarm_before_expiry_never_fires(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(1)
        d.disarm()
        gw.advance_mono(100)
    assert gw.exits == [] and gw.terminal.stderr == ""


def test_disarm_after_the_time_ran_out_exits_12_before_any_output(gw: FakeGateway) -> None:
    """The commit outlasted the deadline: the disarm point reports the timeout (the signal may
    not have been dispatched yet), and nothing was written before it."""
    with pytest.raises(FakeExit) as info, Deadline(gw, json_mode=True) as d:
        d.arm(1)
        alarm = gw.alarms[0]
        alarm.fired = True  # ran out, handler not dispatched
        gw.mono += 2
        d.disarm()
        pytest.fail("a passed deadline must end the command at the disarm point")
    assert info.value.code == 12 and gw.terminal.stdout == ENVELOPE


def test_disarm_is_idempotent_and_exit_disarms(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(1)
        d.disarm()
        d.disarm()
    assert gw.alarms == []


def test_exception_inside_the_block_still_disarms(gw: FakeGateway) -> None:
    with pytest.raises(ValueError), Deadline(gw) as d:
        d.arm(1)
        raise ValueError("boom")
    assert gw.alarms == [] and gw.exits == []


def test_exit_of_the_block_after_expiry_fires_even_with_a_command_error(gw: FakeGateway) -> None:
    with pytest.raises(FakeExit), Deadline(gw) as d:
        d.arm(1)
        gw.alarms[0].fired = True
        gw.mono += 5
        raise ValueError("command failed after the deadline")


def test_double_arm_and_nesting_are_refused(gw: FakeGateway, tmp_path: Path) -> None:
    with Deadline(gw) as d:
        d.arm(1)
        with pytest.raises(DeadlineError):
            d.arm(1)
        with pytest.raises(DeadlineError), Deadline(FakeGateway(tmp_path / "b")):
            pass
        assert dl.current() is d


@pytest.mark.parametrize(
    "bad", [0, -1, -0.0, math.nan, math.inf, -math.inf, 3601, 10**9, True, None, "5", b"5"]
)
def test_bad_durations_are_refused_and_arm_nothing(gw: FakeGateway, bad: object) -> None:
    with Deadline(gw) as d:
        with pytest.raises(DeadlineError):
            d.arm(bad)  # type: ignore[arg-type]
        assert not d.armed and gw.alarms == []


def test_the_longest_and_a_tiny_duration_are_accepted(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(3600)
        d.disarm()
        d.arm(1e-6)


def test_rearm_counts_from_the_first_arm(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(2)
        gw.advance_mono(1.5)  # loading the config
        d.rearm(10)
        gw.advance_mono(8.4)  # 9.9 s after the start
        assert gw.exits == []
        with pytest.raises(FakeExit):
            gw.advance_mono(0.2)


def test_rearm_to_a_moment_that_has_passed_fires_at_once(gw: FakeGateway) -> None:
    with pytest.raises(FakeExit), Deadline(gw) as d:
        d.arm(2)
        gw.mono += 1.9
        gw.alarms[0].at = gw.mono + 5  # the short timer has not run out
        d.rearm(1)  # one second after the start has passed
    assert gw.terminal.stderr == STATUS


def test_rearm_needs_an_armed_deadline(gw: FakeGateway) -> None:
    with Deadline(gw) as d, pytest.raises(DeadlineError):
        d.rearm(1)


def test_paused_disarms_then_arms_again_for_the_given_time(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(30)
        with d.paused(then_seconds=30):
            assert not d.armed
            gw.advance_mono(1000)  # a person thinking: never a timeout
        assert d.armed
        with pytest.raises(FakeExit):
            gw.advance_mono(31)


def test_paused_when_not_armed_stays_disarmed(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        with d.paused():
            pass
        assert not d.armed


def test_paused_that_raises_stays_disarmed(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(30)
        with pytest.raises(ValueError), d.paused():
            raise ValueError("no")
        assert not d.armed


def test_expired_is_the_progress_callback(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        assert d.expired() is False  # nothing armed
        d.arm(2)
        assert d.expired() is False
        gw.mono += 2
        assert d.expired() is True
        gw.alarms[0].fired = True
        with pytest.raises(FakeExit):
            d.disarm()
        assert d.expired() is False


def test_a_failing_write_cannot_stop_the_exit(gw: FakeGateway) -> None:
    def broken(text: str) -> None:
        raise BrokenPipeError

    gw.write_err = broken  # type: ignore[method-assign]
    with pytest.raises(FakeExit) as info, Deadline(gw, json_mode=True) as d:
        d.arm(1)
        gw.advance_mono(1)
    assert info.value.code == 12  # the BrokenPipeError was replaced by the exit


def test_fire_twice_writes_once(gw: FakeGateway) -> None:
    with Deadline(gw) as d:
        d.arm(1)
        with pytest.raises(FakeExit):
            d.fire()
        d.fire()  # a second signal during the first handler: ignored
    assert gw.terminal.stderr == STATUS


def test_current_is_inert_without_a_deadline() -> None:
    inert = dl.current()
    inert.arm(1)
    inert.rearm(1)
    inert.disarm()
    assert inert.expired() is False
    with inert.paused():
        pass


@pytest.mark.parametrize(("command", "seconds"), [("context", 2.0), ("add", 30.0), ("cron", 30.0)])
def test_initial_seconds(command: str, seconds: float) -> None:
    assert dl.initial_seconds(command) == seconds
