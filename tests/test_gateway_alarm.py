"""The real alarm (SIGALRM + ITIMER_REAL) and the process-level deadline.

In-process tests use short real timers with generous bounds and always release them; the
fixture fails a test that leaks a timer, a handler or a changed signal mask. Tests that must
see the process end start one, record its pid at launch and kill exactly that pid on teardown.
"""

from __future__ import annotations

import os
import random
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from memoctl import gateway as gateway_module
from memoctl.gateway import GatewayError, RealGateway
from tests.child_env import child_path

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


@pytest.mark.parametrize(
    "bad",
    [
        0,
        -1,
        float("nan"),
        float("inf"),
        3601,
        pytest.param(10**400, id="huge"),
        pytest.param(-(10**400), id="neg-huge"),
        True,
        None,
        "1",
    ],
)
def test_bad_durations_are_refused_and_nothing_is_changed(gw: RealGateway, bad: object) -> None:
    with pytest.raises(ValueError):
        gw.alarm_arm(bad, lambda: None)  # type: ignore[arg-type]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_sigalrm_pending_while_blocked_does_not_fire_the_new_alarm(
    gw: RealGateway, host: None
) -> None:
    """A SIGALRM left pending before the arm (blocked at startup, inherited across exec) must
    not hit the handler the moment the arm unblocks the signal."""
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    os.kill(os.getpid(), signal.SIGALRM)  # pending, blocked
    hits: list[int] = []
    alarm = gw.alarm_arm(30, lambda: hits.append(1))
    time.sleep(0.1)
    assert hits == []
    assert alarm.release() is False
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})


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
        "PATH": child_path(tmp_path, tools=["sh", "sleep"]),
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
STATUS = b"failed (timeout): deadline passed\n"


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


def test_a_pending_sigalrm_inherited_across_exec_does_not_fire_the_deadline(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    """The parent blocks SIGALRM, raises it and execs: the new process starts with it pending
    and blocked. Arming a 30 s deadline must not fire it at once."""
    child = _program('import time\ntime.sleep(0.5)\nd.disarm()\ngw.write_out("done\\n")')
    launcher = (
        "import os, signal, sys\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\n"
        "os.kill(os.getpid(), signal.SIGALRM)\n"
        "os.execv(sys.executable, [sys.executable, '-I', '-c', sys.argv[1], 'json', '30'])\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", launcher, child],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path),
        cwd=tmp_path,
    )
    launched.append(p)
    out, err = p.communicate(timeout=60)
    assert (p.returncode, out, err) == (0, b"done\n", b"")


# -- the handler's writes are bounded ------------------------------------------------------


def _full_pipe() -> tuple[int, int]:
    """A pipe with no room left. The reader end must stay open (else the write is EPIPE)."""
    r, w = os.pipe()
    os.set_blocking(w, False)
    try:
        while True:
            os.write(w, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(w, True)  # the child shares the description: leave it blocking
    return r, w


def test_a_full_stdout_pipe_does_not_keep_the_process_from_exiting_12(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    code = _program("while True: pass")
    r, w = _full_pipe()
    try:
        p = subprocess.Popen(
            [sys.executable, "-I", "-c", code, "json", "0.3"],
            stdin=subprocess.DEVNULL,
            stdout=w,
            stderr=w,
            env=_env(tmp_path),
            cwd=tmp_path,
        )
        launched.append(p)
        assert p.wait(timeout=30) == 12  # a blocking write would hang until the kill
    finally:
        os.close(r)
        os.close(w)


def test_a_bounded_write_to_a_pipe_with_room_writes_everything() -> None:
    r, w = os.pipe()
    try:
        text = "failed (timeout): deadline passed\n" * 40  # more than one chunk
        gateway_module._write_fd(w, text, 5.0)  # pyright: ignore[reportPrivateUsage]
        os.close(w)
        w = -1
        assert os.read(r, 100_000).decode() == text
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)


def test_a_bounded_write_to_a_full_pipe_gives_up_after_the_bound() -> None:
    r, w = _full_pipe()
    try:
        start = time.monotonic()
        gateway_module._write_fd(w, "x\n", 0.1)  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - start < 10  # generous: it must not block for good
    finally:
        os.close(r)
        os.close(w)


def test_a_bounded_write_to_a_closed_descriptor_is_ignored() -> None:
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    gateway_module._write_fd(w, "x\n", 0.1)  # pyright: ignore[reportPrivateUsage]


# -- a child started while the alarm can fire ----------------------------------------------


def test_a_child_is_registered_before_an_alarm_that_ran_out_at_its_start_can_run(
    gw: RealGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline runs out right after the child started (Popen returned, not yet
    registered): the handler must see the child in the registry so exit_now kills it."""
    real_popen = subprocess.Popen

    class RacyPopen(real_popen):  # type: ignore[type-arg,misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[call-overload]
            os.kill(os.getpid(), signal.SIGALRM)  # the timer ran out just now
            time.sleep(0.1)  # a handler not held back would run here

    seen: list[int] = []
    alarm = gw.alarm_arm(30, lambda: seen.append(len(gateway_module._CHILDREN)))  # pyright: ignore[reportPrivateUsage]
    try:
        monkeypatch.setattr(gateway_module.subprocess, "Popen", RacyPopen)
        assert gw.run(["true"]).returncode == 0
    finally:
        monkeypatch.undo()
        alarm.release()
    assert seen == [1]
    assert not gateway_module._CHILDREN  # pyright: ignore[reportPrivateUsage]


def test_the_child_does_not_inherit_a_blocked_sigalrm(gw: RealGateway) -> None:
    alarm = gw.alarm_arm(30, lambda: None)
    try:
        result = gw.run(["grep", "SigBlk", "/proc/self/status"])
    finally:
        alarm.release()
    assert result.stdout.split()[1] == "0000000000000000"


def test_the_caller_mask_is_unchanged_by_a_run(gw: RealGateway) -> None:
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    gw.run(["true"])
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before


def test_a_failed_start_restores_the_mask_and_leaves_no_child(gw: RealGateway) -> None:
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    with pytest.raises(GatewayError):
        gw.run(["/nonexistent/binary"])
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
    assert not gateway_module._CHILDREN  # pyright: ignore[reportPrivateUsage]


def test_the_child_stays_registered_until_it_is_reaped(
    gw: RealGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exit_now must still find a child that the Popen exit is waiting for."""
    real_popen = subprocess.Popen
    registered: list[bool] = []

    class Watching(real_popen):  # type: ignore[type-arg,misc]
        def __exit__(self, *exc: object) -> None:
            registered.append(self in gateway_module._CHILDREN)  # pyright: ignore[reportPrivateUsage]
            super().__exit__(*exc)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway_module.subprocess, "Popen", Watching)
    gw.run(["true"])
    assert registered == [True]
    assert not gateway_module._CHILDREN  # pyright: ignore[reportPrivateUsage]


def test_no_child_is_orphaned_by_a_deadline_that_runs_out_as_it_starts(
    tmp_path: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    """Random millisecond deadlines against a command start: whatever the timing, a child that
    was started is killed with the process. Only the pids the children wrote are signalled."""
    pidfile = tmp_path / "pids"
    body = f"""gw.run(["sh", "-c", 'echo $$ >> {pidfile}; exec sleep 60'])"""
    rng = random.Random(20260101)
    for _ in range(12):
        rc, _out, _err = run_harness(
            tmp_path, launched, body, seconds=rng.uniform(0.001, 0.03), mode="plain"
        )
        assert rc == 12
    time.sleep(0.5)  # an orphan has written its pid by now
    pids = [int(x) for x in pidfile.read_text().split()] if pidfile.exists() else []
    try:
        assert wait_until(lambda: not any(_alive(pid) for pid in pids)), "an orphaned child"
    finally:
        for pid in pids:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
