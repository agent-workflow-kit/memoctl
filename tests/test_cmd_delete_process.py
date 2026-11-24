"""``memoctl delete`` as real processes: kills at the transaction, concurrent writers, a real
terminal (a pty), no terminal, closed descriptors. Private home throughout (the password entry
is replaced in the child; see ``tests/private_home.py``)."""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import sqlite3
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Any

import pytest

from tests import private_home
from tests.child_env import child_path

REPO = "example.com/a/b"
HOLD = Path(__file__).with_name("delete_hold_wrapper.py")


def env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": child_path(home.parent),
        "LC_ALL": "C",
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def memoctl(
    home: Path, *args: str, stdin: bytes | None = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        private_home.argv(home, *args), input=stdin, capture_output=True, env=env(home),
        timeout=60,
    )  # fmt: skip


def add(home: Path, body: str, *extra: str) -> int:
    done = memoctl(
        home, "add", "--repo", REPO, "--topic", "t", "--body-file", "-", "--json", *extra,
        stdin=body.encode(),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return int(json.loads(done.stdout)["id"])


def rows(home: Path, sql: str = "SELECT id, superseded_by FROM notes ORDER BY id") -> list[Any]:
    conn = sqlite3.connect(home / ".local/state/memoctl/state.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def log_lines(home: Path) -> list[dict[str, Any]]:
    path = home / ".local/state/memoctl/log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# -- kill inside the transaction ------------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_a_kill_before_the_commit_deletes_nothing(home: Path, sig: int) -> None:
    ids = [add(home, f"note {i}") for i in range(4)]
    add(home, "newer", "--supersedes", str(ids[0]))  # a chain, so the rule has work to do
    before = rows(home)
    log_before = log_lines(home)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(HOLD), str(home), "delete", "--yes", "5", "2", "3"],
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
    assert rows(home) == before  # the uncommitted deletes and repoints are gone
    assert log_lines(home) == log_before
    # the store is usable at once (no stale lock) and the same command now works
    done = memoctl(home, "delete", "--yes", "--json", "5", "2", "3")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"deleted": [2, 3, 5], "reinstated": [1]}


# -- concurrent writers ---------------------------------------------------------------------


def spawn(home: Path, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        private_home.argv(home, *args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env(home),
    )  # fmt: skip


def finish(procs: list[subprocess.Popen[bytes]]) -> list[tuple[int, bytes]]:
    out = []
    try:
        for proc in procs:
            stdout, _ = proc.communicate(timeout=60)
            out.append((proc.returncode, stdout))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    return out


def test_overlapping_deletes_are_all_or_nothing(home: Path) -> None:
    for i in range(6):
        add(home, f"n{i}")
    a = spawn(home, "delete", "--yes", "--json", "1", "2", "3")
    b = spawn(home, "delete", "--yes", "--json", "3", "4", "5")
    (ca, _), (cb, _) = finish([a, b])
    left = [r[0] for r in rows(home)]
    assert sorted([ca, cb]) == [0, 10]  # one wins; the other finds 3 gone
    assert left == ([4, 5, 6] if ca == 0 else [1, 2, 6])
    assert len([r for r in log_lines(home) if r["event"] == "delete"]) == 3


def test_delete_against_a_superseding_add_leaves_no_dangling_pointer(home: Path) -> None:
    for round_ in range(3):
        target = add(home, f"target {round_}")
        d = spawn(home, "delete", "--yes", str(target))
        body = home.parent / f"body{round_}.txt"
        body.write_text(f"replacement {round_}\n")
        a = spawn(
            home, "add", "--repo", REPO, "--topic", "t", "--body-file", str(body),
            "--supersedes", str(target),
        )  # fmt: skip
        (cd, _), (ca, _) = finish([d, a])
        assert cd in (0, 10) and ca in (0, 10)
        present = {r[0] for r in rows(home)}
        for note_id, pointer in rows(home):
            assert pointer is None or pointer in present, (round_, note_id, pointer)
        if cd == 0 and ca == 0:
            assert target not in present  # delete ran last: the new note is live


# -- a real terminal ------------------------------------------------------------------------


# Runs in the child after ``start_new_session`` (the child leads a new session): take the pty on
# descriptor 0 as the controlling terminal, then become the real command. No ``preexec_fn``
# (unsafe with threads), and nothing of the production code is changed.
TAKE_TTY = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


def run_on_pty(
    home: Path, *args: str, send: bytes, typed_ahead: bytes = b"", icrnl: bool = True
) -> tuple[int, bytes, bytes]:
    """Start the command with a pty as its controlling terminal; write ``send`` after the
    prompt appears. ``typed_ahead`` is written before the command starts; ``icrnl=False`` keeps
    a carriage return as typed. Returns the exit code, what was shown on the terminal and stdout."""
    master, slave = pty.openpty()
    try:
        if not icrnl:
            attrs = termios.tcgetattr(slave)
            attrs[0] &= ~termios.ICRNL
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        if typed_ahead:
            os.write(master, typed_ahead)
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", TAKE_TTY, *private_home.argv(home, *args)],
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env(home),
            start_new_session=True,
        )  # fmt: skip
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    shown = b""
    try:
        deadline = time.monotonic() + 30
        while b"[y/N] " not in shown and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                shown += os.read(master, 4096)
        os.write(master, send)
        stdout, _ = proc.communicate(timeout=60)
        try:
            while True:  # whatever the terminal still holds
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    break
                shown += os.read(master, 4096)
        except OSError:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    return proc.returncode, shown, stdout


def test_the_prompt_is_on_the_terminal_and_yes_deletes(home: Path) -> None:
    add(home, "x")
    code, shown, stdout = run_on_pty(home, "delete", "1", send=b"y\n")
    assert code == 0
    assert f"Delete note 1 ({REPO}, t)? [y/N] ".encode() in shown
    assert b"Delete" not in stdout
    assert rows(home) == []
    assert [r["event"] for r in log_lines(home)][-1] == "delete"
    assert log_lines(home)[-1]["actor_source"] in ("tty", "default")


@pytest.mark.parametrize(
    "send",
    [b"n\n", b"\n", b"\x04", b"yes please\n", b"y\x00\n", b"y" * 2000 + b"\n", b" y\n"],
)
def test_other_answers_delete_nothing_exit_13(home: Path, send: bytes) -> None:
    add(home, "x")
    code, _shown, _ = run_on_pty(home, "delete", "1", send=send)
    assert code == 13
    assert rows(home) == [(1, None)]


def test_input_typed_before_the_prompt_is_discarded(home: Path) -> None:
    add(home, "x")
    code, _shown, _ = run_on_pty(home, "delete", "1", send=b"n\n", typed_ahead=b"y\n")
    assert code == 13 and rows(home) == [(1, None)]


def test_a_carriage_return_makes_it_another_answer(home: Path) -> None:
    add(home, "x")
    code, _shown, _ = run_on_pty(home, "delete", "1", send=b"y\r\n", icrnl=False)
    assert code == 13 and rows(home) == [(1, None)]


def test_end_of_input_on_the_terminal_after_a_partial_line(home: Path) -> None:
    add(home, "x")
    code, _shown, _ = run_on_pty(home, "delete", "1", send=b"y\x04\x04")
    assert code == 13 and rows(home) == [(1, None)]


def test_yes_variants_any_case(home: Path) -> None:
    add(home, "x")
    code, _, _ = run_on_pty(home, "delete", "1", send=b"YeS\n")
    assert code == 0 and rows(home) == []


def test_no_terminal_exit_13_without_yes_and_works_with_yes(home: Path) -> None:
    add(home, "x")
    add(home, "y")
    # a new session with no controlling terminal: /dev/tty cannot be opened
    proc = subprocess.run(
        private_home.argv(home, "delete", "1", "--json"), stdin=subprocess.DEVNULL,
        capture_output=True, env=env(home), start_new_session=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 13
    assert json.loads(proc.stdout)["error"]["code"] == "needs_human"
    assert [r[0] for r in rows(home)] == [1, 2]
    ok = subprocess.run(
        private_home.argv(home, "delete", "1", "--yes"), stdin=subprocess.DEVNULL,
        capture_output=True, env=env(home), start_new_session=True, timeout=60,
    )  # fmt: skip
    assert ok.returncode == 0 and ok.stdout == b"deleted note 1\n"
    assert [r[0] for r in rows(home)] == [2]


# -- descriptors ----------------------------------------------------------------------------


def test_closed_standard_descriptors(home: Path) -> None:
    add(home, "x")

    close_all = (
        "import os, sys\n"
        "for fd in (0, 1, 2):\n"
        "    os.close(fd)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", close_all, *private_home.argv(home, "delete", "1", "--yes")],
        env=env(home),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    assert proc.wait(timeout=60) == 0
    assert rows(home) == []


def test_closed_stdout_pipe_still_deletes_and_exits_0(home: Path) -> None:
    add(home, "x")
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        proc = subprocess.Popen(
            private_home.argv(home, "delete", "1", "--yes"), env=env(home),
            stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        assert proc.wait(timeout=60) == 0
    finally:
        os.close(write_end)
    assert rows(home) == []
