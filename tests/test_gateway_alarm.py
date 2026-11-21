"""The real alarm (SIGALRM + ITIMER_REAL) and the process-level deadline.

In-process tests use short real timers with generous bounds and always release them; the
fixture fails a test that leaks a timer, a handler or a changed signal mask. Tests that must
see the process end start one, record its pid at launch and kill exactly that pid on teardown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from memoctl.gateway import GatewayError, RealGateway

WAIT = 10.0  # generous: only an upper bound on waiting for a signal that must arrive


def wait_until(predicate, limit: float = WAIT) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


@pytest.fixture(autouse=True)
def pristine_signal_state() -> Iterator[None]:
    before = (signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL))
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    assert before[1] == (0.0, 0.0)
    yield
    leaked = signal.getitimer(signal.ITIMER_REAL)
    handler = signal.getsignal(signal.SIGALRM)
    now_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, before[0])
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    assert leaked == (0.0, 0.0), "a timer leaked"
    assert handler == before[0], "the SIGALRM handler was not restored"
    assert now_mask == mask, "the signal mask changed"


@pytest.fixture
def host() -> Iterator[None]:
    """For a test that plays the host process: it sets its own handler and timer, and the
    fixture puts the defaults back before the leak check runs."""
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)


@pytest.fixture
def gw() -> RealGateway:
    return RealGateway()


def test_the_handler_runs_in_the_main_thread_and_release_restores(gw: RealGateway) -> None:
    hits: list[str] = []
    alarm = gw.alarm_arm(0.05, lambda: hits.append(threading.current_thread().name))
    assert wait_until(lambda: bool(hits))
    assert hits == ["MainThread"]
    assert alarm.release() is True  # it had run out
    assert alarm.release() is False  # idempotent


def test_release_before_expiry_returns_false_and_nothing_fires(gw: RealGateway) -> None:
    hits: list[int] = []
    alarm = gw.alarm_arm(30, lambda: hits.append(1))
    assert alarm.release() is False
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert hits == []


def test_reset_restarts_the_timer(gw: RealGateway) -> None:
    hits: list[int] = []
    alarm = gw.alarm_arm(30, lambda: hits.append(1))
    alarm.reset(0.05)
    assert wait_until(lambda: bool(hits))
    alarm.release()


def test_previous_handler_and_host_timer_are_restored(gw: RealGateway, host: None) -> None:
    seen: list[int] = []

    def host_handler(signum: int, frame: object) -> None:
        seen.append(signum)

    signal.signal(signal.SIGALRM, host_handler)
    signal.setitimer(signal.ITIMER_REAL, 30)
    alarm = gw.alarm_arm(0.05, lambda: None)
    assert signal.getsignal(signal.SIGALRM) is not host_handler
    assert wait_until(lambda: signal.getitimer(signal.ITIMER_REAL)[0] == 0.0)
    assert alarm.release() is True
    assert signal.getsignal(signal.SIGALRM) is host_handler
    left, interval = signal.getitimer(signal.ITIMER_REAL)
    assert 0 < left <= 30 and interval == 0.0  # the host's remaining time, not ours
    assert seen == []
    signal.setitimer(signal.ITIMER_REAL, 0)


def test_a_host_timer_that_ran_out_meanwhile_still_fires_after_release(
    gw: RealGateway, host: None
) -> None:
    seen: list[int] = []
    signal.signal(signal.SIGALRM, lambda s, f: seen.append(s))
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    alarm = gw.alarm_arm(30, lambda: None)
    time.sleep(0.2)  # the host's timer would have fired during ours
    alarm.release()
    assert wait_until(lambda: bool(seen))


def test_sigalrm_ignored_by_the_parent_is_restored_as_ignored(gw: RealGateway, host: None) -> None:
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    hits: list[int] = []
    alarm = gw.alarm_arm(0.05, lambda: hits.append(1))
    assert wait_until(lambda: bool(hits))
    alarm.release()
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_IGN


def test_pending_signal_is_discarded_not_delivered_to_the_restored_handler(
    gw: RealGateway, host: None
) -> None:
    """With the signal blocked and pending, release must not let it reach the previous handler
    (the default action would end the process)."""
    seen: list[int] = []
    signal.signal(signal.SIGALRM, lambda s, f: seen.append(s))
    alarm = gw.alarm_arm(30, lambda: None)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    signal.raise_signal(signal.SIGALRM)  # pending, blocked
    assert alarm.release() is False  # the timer itself had not run out
    time.sleep(0.05)
    assert seen == []
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    time.sleep(0.05)
    assert seen == []


def test_a_blocked_sigalrm_is_unblocked_while_armed_and_the_mask_restored(
    gw: RealGateway,
) -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    hits: list[int] = []
    alarm = gw.alarm_arm(0.05, lambda: hits.append(1))
    assert wait_until(lambda: bool(hits)), "a parent-blocked SIGALRM silenced the deadline"
    alarm.release()
    assert signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})


def test_a_handler_that_raises_propagates_and_release_still_restores(gw: RealGateway) -> None:
    def bad() -> None:
        raise RuntimeError("handler failed")

    alarm = gw.alarm_arm(0.02, bad)
    with pytest.raises(RuntimeError):
        wait_until(lambda: False, limit=5)
    alarm.release()  # the fixture checks the restore


def test_a_signal_dispatched_after_release_is_ignored(gw: RealGateway) -> None:
    hits: list[int] = []
    alarm = gw.alarm_arm(30, lambda: hits.append(1))
    handler = signal.getsignal(signal.SIGALRM)
    alarm.release()
    assert callable(handler)
    handler(signal.SIGALRM, None)  # a trip flagged just before the release
    assert hits == []


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), 3601, True, None, "1"])
def test_bad_durations_are_refused_and_nothing_is_changed(gw: RealGateway, bad: object) -> None:
    with pytest.raises(ValueError):
        gw.alarm_arm(bad, lambda: None)  # type: ignore[arg-type]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_arming_off_the_main_thread_is_refused(gw: RealGateway) -> None:
    errors: list[BaseException] = []

    def work() -> None:
        try:
            gw.alarm_arm(1, lambda: None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(WAIT)
    assert len(errors) == 1 and isinstance(errors[0], GatewayError)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_monotonic_never_goes_back(gw: RealGateway) -> None:
    a = gw.monotonic()
    assert gw.monotonic() >= a


# -- the process level: exit_now and the deadline end to end -------------------------------

HARNESS = """\
import os, sys
from memoctl.deadline import Deadline
from memoctl.gateway import RealGateway
gw = RealGateway()
json_mode = sys.argv[1] == "json"
with Deadline(gw, json_mode=json_mode) as d:
    d.arm(float(sys.argv[2]))
{body}
"""


def _program(body: str) -> str:
    """The harness with ``body`` (any indentation) as the block run under the deadline."""
    inner = textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 4)
    return HARNESS.replace("{body}", inner)


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }


@pytest.fixture
def launched() -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for p in procs:  # exactly the processes this test started
        if p.poll() is None:
            p.kill()
        p.communicate()


def run_harness(
    tmp_path: Path,
    launched: list[subprocess.Popen[bytes]],
    body: str,
    *,
    mode: str = "json",
    seconds: float = 0.3,
    stdin: int | None = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, bytes, bytes]:
    code = _program(body)
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", code, mode, str(seconds)],
        stdin=subprocess.DEVNULL if stdin is None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path),
        cwd=tmp_path,
        pass_fds=pass_fds,
    )
    launched.append(p)
    out, err = p.communicate(timeout=60)
    return p.returncode, out, err


ENVELOPE = (
    b'{"error": {"code": "failed", "message": "deadline passed", "reason": "timeout", '
    b'"details": null, "retryable": true}}\n'
)
STATUS = b"failed (timeout): the deadline passed\n"


def test_a_busy_loop_is_ended_with_the_envelope_and_exit_12(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    rc, out, err = run_harness(tmp_path, launched, "while True: pass")
    assert (rc, out, err) == (12, ENVELOPE, STATUS)


def test_plain_mode_leaves_stdout_empty(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    rc, out, err = run_harness(tmp_path, launched, "while True: pass", mode="plain")
    assert (rc, out, err) == (12, b"", STATUS)


def test_a_finally_and_an_except_cannot_swallow_the_deadline(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    body = """
        try:
            try:
                while True: pass
            finally:
                while True: pass   # cleanup running when the signal arrives
        except BaseException:
            os._exit(99)
    """
    rc, out, err = run_harness(tmp_path, launched, body)
    assert (rc, out) == (12, ENVELOPE)


def test_a_silent_never_closing_stdin_pipe_is_cut_off(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    """A blocking os.read on a pipe whose writer stays open and silent is interrupted (EINTR
    runs the handler, which exits)."""
    r, w = os.pipe()
    try:
        rc, out, err = run_harness(
            tmp_path, launched, "os.read(0, 10)", stdin=r, mode="json", seconds=0.3
        )
    finally:
        os.close(r)
        os.close(w)
    assert (rc, out, err) == (12, ENVELOPE, STATUS)


def test_sigalrm_ignored_by_the_parent_does_not_disable_the_deadline(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    code = _program("while True: pass")
    p = subprocess.Popen(
        [
            "sh",
            "-c",
            'trap "" ALRM; exec "$0" "$@"',
            sys.executable,
            "-I",
            "-c",
            code,
            "json",
            "0.3",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path),
        cwd=tmp_path,
    )
    launched.append(p)
    out, _err = p.communicate(timeout=60)
    assert (p.returncode, out) == (12, ENVELOPE)


def test_a_closed_stdout_still_ends_in_exit_12(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    rc, _out, _err = run_harness(tmp_path, launched, "os.close(1)\nwhile True: pass")
    assert rc == 12


def test_a_broken_stdout_pipe_still_ends_in_exit_12(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    code = _program("while True: pass")
    r, w = os.pipe()
    os.close(r)  # the reader is gone: writing gives EPIPE
    try:
        p = subprocess.Popen(
            [sys.executable, "-I", "-c", code, "json", "0.3"],
            stdin=subprocess.DEVNULL,
            stdout=w,
            stderr=subprocess.PIPE,
            env=_env(tmp_path),
            cwd=tmp_path,
        )
    finally:
        os.close(w)
    launched.append(p)
    _out, err = p.communicate(timeout=60)
    assert (p.returncode, err) == (12, STATUS)


def test_the_child_process_the_gateway_started_dies_with_the_deadline(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    pidfile = tmp_path / "child.pid"
    body = f"""
        gw.run(["sh", "-c", 'echo $$ > {pidfile}; exec sleep 60'])
    """
    rc, out, _err = run_harness(tmp_path, launched, body, seconds=1.0)
    assert (rc, out) == (12, ENVELOPE)
    pid = int(pidfile.read_text())
    try:
        assert wait_until(lambda: not _alive(pid)), "the child outlived the deadline"
    finally:
        if _alive(pid):  # the exact pid the child wrote, nothing matched by name
            os.kill(pid, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b") ", 1)[1][:1] != b"Z"
    except OSError:
        return False


def test_disarm_before_the_deadline_lets_the_command_finish(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    body = """
        d.disarm()
        import time
        time.sleep(0.6)
        gw.write_out("done\\n")
    """
    rc, out, err = run_harness(tmp_path, launched, body)
    assert (rc, out, err) == (0, b"done\n", b"")


def test_a_deadline_that_passes_before_the_disarm_exits_12_with_no_other_output(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    """The handler cannot run inside a C call; the disarm point still reports the timeout."""
    body = """
        import signal
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        import time
        time.sleep(0.6)           # the timer ran out while the signal was held
        d.disarm()
        gw.write_out("must not print\\n")
    """
    rc, out, _err = run_harness(tmp_path, launched, body)
    assert (rc, out) == (12, ENVELOPE)


MAIN_HARNESS = """\
import os, sys
import memoctl.deadline as dl
from memoctl.cli import main
from memoctl.cli_args import Command
dl.COMMAND_SECONDS = 0.3
def run(args, gw):
    os.read(0, 10)  # a silent stdin that never closes
    return 0
sys.exit(main(sys.argv[1:], commands=(Command("probe", "probe", run),)))
"""


@pytest.mark.parametrize(
    ("flags", "stdout"), [([], b""), (["--json"], ENVELOPE)], ids=["plain", "json"]
)
def test_main_ends_a_command_blocked_on_a_silent_stdin(
    tmp_path: Path,
    launched: list[subprocess.Popen[bytes]],
    flags: list[str],
    stdout: bytes,
) -> None:
    r, w = os.pipe()
    try:
        p = subprocess.Popen(
            [sys.executable, "-I", "-c", MAIN_HARNESS, "probe", *flags],
            stdin=r,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(tmp_path),
            cwd=tmp_path,
        )
    finally:
        os.close(r)
    launched.append(p)
    try:
        out, err = p.communicate(timeout=60)
    finally:
        os.close(w)
    assert (p.returncode, out, err) == (12, stdout, STATUS)
