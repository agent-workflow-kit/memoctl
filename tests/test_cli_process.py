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

from memoctl import __version__
from tests import private_home
from tests.child_env import child_path


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "PATH": child_path(tmp_path, tools=["sh"]),
        "LC_ALL": "C",
    }


def _run(
    tmp_path: Path,
    args: Sequence[bytes | str],
    *,
    shell_prefix: str = "",
    stdin: int | None = None,
    entry: Sequence[str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # The default entry is the real one with a private home (tests/private_home.py).
    argv: list[bytes | str] = [*(entry or private_home.argv(tmp_path / "home")), *args]
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
    assert (r.returncode, r.stdout, r.stderr) == (0, f"memoctl {__version__}\n".encode(), b"")


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
            [*private_home.argv(tmp_path / "home"), "bogus", "--json"],
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


PROBE = """\
import os
import sys

from memoctl.cli import main
from memoctl.cli_args import Command


def run(args, gw):
    fd = os.open(__file__, os.O_RDONLY)
    if fd < 3:  # a closed standard descriptor was not refilled before this open
        raise RuntimeError("descriptor %d" % fd)
    gw.write_out("ok\\n")
    return 0


sys.exit(main(None, None, (Command("probe", "probe", run),)))
"""


@pytest.mark.parametrize(
    "closed",
    [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)],
    ids=lambda c: "closed-" + "".join(map(str, c)),
)
def test_a_command_never_gets_a_standard_descriptor_for_its_own_files(
    tmp_path: Path, closed: tuple[int, ...]
) -> None:
    script = tmp_path / "probe.py"  # a probe that never asks for the home: no wrapper needed
    script.write_text(PROBE)
    prefix = " ".join({0: "<&-", 1: ">&-", 2: "2>&-"}[fd] for fd in closed)
    r = _run(tmp_path, ["probe"], shell_prefix=prefix, entry=(sys.executable, "-I", str(script)))
    assert r.returncode == 0
    if 1 not in closed:
        assert r.stdout == b"ok\n"
    if 2 not in closed:
        assert r.stderr == b""
