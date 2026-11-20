"""The autouse process guard in ``conftest.py`` blocks every route to a real crontab.

The decoy is a script named ``crontab`` outside the pytest tmp tree. If a guard failed, the
decoy would run and leave a marker; the real crontab is never involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from subprocess import Popen as EarlyPopen  # taken before the guard patches subprocess

import pytest

from tests.conftest import CrontabGuardError


@pytest.fixture
def decoy() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="memoctl-decoy-"))
    script = root / "crontab"
    script.write_text(f"#!/bin/sh\ntouch {root}/ran\n")
    script.chmod(0o755)
    try:
        yield script
    finally:
        shutil.rmtree(root)


def _ran(decoy: Path) -> bool:
    return (decoy.parent / "ran").exists()


def test_argv0_outside_tmp_is_refused(decoy: Path) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run([str(decoy), "-l"], check=False)
    assert not _ran(decoy)


def test_executable_argument_is_checked(decoy: Path) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run(["harmless"], executable=str(decoy), check=False)
    assert not _ran(decoy)


@pytest.mark.parametrize("shell", ["/bin/sh", "bash", "dash"])
def test_shell_dash_c_mentioning_crontab_is_refused(decoy: Path, shell: str) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run([shell, "-c", f"{decoy} -l"], check=False)
    assert not _ran(decoy)


def test_shell_string_form_is_refused(decoy: Path) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run(f"{decoy} -l", shell=True, check=False)
    assert not _ran(decoy)


def test_popen_imported_before_the_guard_is_still_guarded(decoy: Path) -> None:
    assert EarlyPopen is not subprocess.Popen
    with pytest.raises(CrontabGuardError):
        EarlyPopen([str(decoy), "-l"])
    with pytest.raises(CrontabGuardError):
        EarlyPopen(["harmless"], executable=str(decoy))
    with pytest.raises(CrontabGuardError):
        EarlyPopen("crontab -l", shell=True)
    assert not _ran(decoy)


def test_os_system_is_blocked(decoy: Path) -> None:
    with pytest.raises(CrontabGuardError):
        os.system(f"{decoy} -l")
    assert not _ran(decoy)


@pytest.mark.parametrize(
    "call",
    [
        lambda d: os.posix_spawn(d, [d], {}),
        lambda d: os.posix_spawnp(d, [d], {}),
        lambda d: os.spawnv(os.P_WAIT, d, [d]),
        lambda d: os.spawnl(os.P_WAIT, d, d),
        lambda d: os.spawnvp(os.P_WAIT, d, [d]),
        lambda d: os.execv(d, [d]),
        lambda d: os.execl(d, d),
        lambda d: os.execvp(d, [d]),
        lambda d: os.execve(d, [d], {}),
        lambda d: os.fork(),
    ],
    ids=[
        "posix_spawn",
        "posix_spawnp",
        "spawnv",
        "spawnl",
        "spawnvp",
        "execv",
        "execl",
        "execvp",
        "execve",
        "fork",
    ],
)
def test_direct_os_process_calls_are_blocked(decoy: Path, call: object) -> None:
    with pytest.raises(CrontabGuardError, match="not allowed"):
        call(str(decoy))  # type: ignore[operator]
    assert not _ran(decoy)


def test_subprocess_without_posix_spawn_restrictions_still_works() -> None:
    # close_fds=False takes subprocess's own os.posix_spawn path.
    result = subprocess.run(["/bin/echo", "ok"], close_fds=False, capture_output=True, check=False)
    assert result.stdout == b"ok\n"


def test_tripwire_on_the_default_path_exits_97() -> None:
    # `env` is not crontab, so the guard lets it through; it then resolves crontab on PATH.
    result = subprocess.run(["/usr/bin/env", "crontab", "-l"], capture_output=True, check=False)
    assert result.returncode == 97
    assert b"tripwire" in result.stderr
