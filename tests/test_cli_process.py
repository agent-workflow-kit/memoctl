"""The real entry point in a child process: closed descriptors, EPIPE, locale, hostile argv.

Children get a private HOME and XDG variables and a minimal environment; each is waited on
with a timeout and, on timeout, killed by its own handle (never by name).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }


def _run(
    tmp_path: Path, args: Sequence[bytes | str], *, shell_prefix: str = "", stdin: int | None = None
) -> subprocess.CompletedProcess[bytes]:
    argv: list[bytes | str] = [sys.executable, "-I", "-m", "memoctl", *args]
    cmd: list[bytes | str] = argv
    if shell_prefix:
        cmd = ["sh", "-c", f'exec "$0" "$@" {shell_prefix}', *argv]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL if stdin is None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path),
        cwd=tmp_path,
    )
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def test_version_under_c_locale_is_ascii_and_exit_zero(tmp_path: Path) -> None:
    r = _run(tmp_path, ["--version"])
    assert (r.returncode, r.stdout, r.stderr) == (0, b"memoctl 0.0.0\n", b"")


def test_json_usage_error_end_to_end(tmp_path: Path) -> None:
    r = _run(tmp_path, ["--json", "bogus"])
    assert r.returncode == 2
    assert json.loads(r.stdout)["error"]["details"] == {"field": "argv"}
    assert r.stderr == b"usage: invalid argv\n"


def test_non_utf8_argv_is_a_usage_error_and_never_echoed(tmp_path: Path) -> None:
    r = _run(tmp_path, [b"bogus\xff\xfe", "--json"])
    assert r.returncode == 2
    assert b"bogus" not in r.stdout + r.stderr
    assert b"\xff" not in r.stdout + r.stderr


def test_huge_argument_is_a_usage_error(tmp_path: Path) -> None:
    r = _run(tmp_path, ["x" * 100_000, "--json"])
    assert r.returncode == 2
    assert len(r.stdout) < 300


def test_all_standard_descriptors_closed_still_works(tmp_path: Path) -> None:
    r = _run(tmp_path, ["--version"], shell_prefix="<&- >&- 2>&-")
    assert r.returncode == 0
    r = _run(tmp_path, ["bogus", "--json"], shell_prefix="<&- >&- 2>&-")
    assert r.returncode == 2


def test_closed_stdout_alone_keeps_the_exit_code_and_stderr(tmp_path: Path) -> None:
    r = _run(tmp_path, ["bogus", "--json"], shell_prefix=">&-")
    assert r.returncode == 2
    assert r.stderr == b"usage: invalid argv\n"


def test_broken_pipe_on_stdout_keeps_the_exit_code(tmp_path: Path) -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)  # a reader that is gone: every write is EPIPE
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "memoctl", "bogus", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=write_end,
            stderr=subprocess.PIPE,
            env=_env(tmp_path),
            cwd=tmp_path,
        )
    finally:
        os.close(write_end)
    try:
        _, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    assert proc.returncode == 2
    assert err == b"usage: invalid argv\n"


@pytest.mark.parametrize("args", [[], ["--help", "x"], ["-h"]])
def test_refused_forms_exit_2(tmp_path: Path, args: list[str]) -> None:
    assert _run(tmp_path, args).returncode == 2
