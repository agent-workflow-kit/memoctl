"""The autouse process guard in ``conftest.py`` blocks every route to a real crontab.

The decoy is a script named ``crontab`` outside the pytest tmp tree. If a guard failed, the
decoy would run and leave a marker; the real crontab is never involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from subprocess import Popen as EarlyPopen  # taken before the guard patches subprocess

import pytest

from tests import private_home
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
    # `env crontab` is refused by the guard (see below); the tripwire is the second line of
    # defence, run here by its explicit path, which the guard allows under its base.
    tripwire = os.environ["PATH"].split(os.pathsep)[0] + "/crontab"
    result = subprocess.run(["/usr/bin/env", tripwire, "-l"], capture_output=True, check=False)
    assert result.returncode == 97
    assert b"tripwire" in result.stderr


@pytest.mark.parametrize("wrapper", [["env"], ["nohup"], ["timeout", "5"], ["xargs"]])
def test_wrapper_running_a_crontab_by_path_is_refused(decoy: Path, wrapper: list[str]) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run([*wrapper, str(decoy), "-l"], check=False)
    assert not _ran(decoy)


def test_wrapper_running_a_crontab_by_name_is_refused(decoy: Path) -> None:
    with pytest.raises(CrontabGuardError):
        subprocess.run(["env", "crontab", "-l"], check=False)


def test_wrapper_running_a_fake_crontab_under_the_base_is_allowed(
    fake_crontab: tuple[str, Path],
) -> None:
    script, _state = fake_crontab
    result = subprocess.run(["env", script, "-l"], check=False, capture_output=True)
    assert result.returncode in (0, 1)


# --- the child's PATH may never find a real crontab -------------------------------------------


@pytest.fixture
def decoy_bin(decoy: Path) -> str:
    """A directory outside the pytest tmp tree whose ``crontab`` stands in for the real one."""
    return str(decoy.parent)


def _system_crontab_path() -> str:
    found = shutil.which("crontab", path="/usr/bin:/bin")
    if found is None:
        pytest.skip("no system crontab on this machine")
    return "/usr/bin:/bin"


def test_child_whose_path_finds_a_crontab_outside_the_tmp_dir_is_refused(
    decoy: Path, decoy_bin: str
) -> None:
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": decoy_bin}, check=False)
    with pytest.raises(CrontabGuardError, match="PATH"):
        EarlyPopen([sys.executable, "-c", "pass"], env={"PATH": f"/nonexistent:{decoy_bin}"})
    assert not _ran(decoy)


def test_child_whose_path_is_the_system_path_is_refused_before_exec() -> None:
    path = _system_crontab_path()  # skipped when there is no system crontab
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": path}, check=False)
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": path}, close_fds=False)


def test_the_check_runs_before_any_process_exists(
    decoy_bin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    forks: list[object] = []
    real = subprocess._fork_exec  # type: ignore[attr-defined]  # the guard around the real one

    def record(*a: object, **k: object) -> object:
        forks.append(a)
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "_fork_exec", record)
    with pytest.raises(CrontabGuardError):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": decoy_bin}, check=False)
    assert forks == []  # Popen refused before it reached _fork_exec


def test_inherited_environment_is_checked_too(
    decoy: Path, decoy_bin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", decoy_bin)  # no per-test tripwire in front any more
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], check=False)
    assert not _ran(decoy)


def test_a_path_with_the_fake_first_is_allowed(
    fake_crontab: tuple[str, Path], decoy_bin: str
) -> None:
    script, _state = fake_crontab
    path = f"{Path(script).parent}{os.pathsep}{decoy_bin}"  # the fake wins the lookup
    result = subprocess.run([sys.executable, "-c", "pass"], env={"PATH": path}, check=False)
    assert result.returncode == 0


def test_a_path_without_any_crontab_is_allowed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run([sys.executable, "-c", "pass"], env={"PATH": str(empty)}, check=False)
    assert result.returncode == 0


def test_an_empty_path_entry_means_the_childs_cwd(
    decoy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run(
            [sys.executable, "-c", "pass"],
            env={"PATH": f"{tmp_path}{os.pathsep}"}, cwd=decoy.parent, check=False,
        )  # fmt: skip
    # the same entry is harmless when the child starts in a directory without a crontab
    result = subprocess.run(
        [sys.executable, "-c", "pass"], env={"PATH": ""}, cwd=tmp_path, check=False
    )
    assert result.returncode == 0
    monkeypatch.chdir(decoy.parent)  # and with no cwd given it is this process's own
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": ""}, check=False)


def test_a_relative_path_entry_is_resolved_against_the_childs_cwd(
    decoy: Path, tmp_path: Path
) -> None:
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run(
            [sys.executable, "-c", "pass"],
            env={"PATH": "."}, cwd=decoy.parent, check=False,
        )  # fmt: skip
    (tmp_path / "rel").mkdir()
    result = subprocess.run(
        [sys.executable, "-c", "pass"], env={"PATH": "rel"}, cwd=tmp_path, check=False
    )
    assert result.returncode == 0


def test_a_child_without_a_path_gets_the_default_search_path(
    monkeypatch: pytest.MonkeyPatch, decoy_bin: str
) -> None:
    monkeypatch.setattr(os, "defpath", decoy_bin)
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"HOME": "/nonexistent"}, check=False)


def test_a_symlink_to_a_real_crontab_in_a_tmp_dir_is_refused(decoy: Path, tmp_path: Path) -> None:
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "crontab").symlink_to(decoy)
    with pytest.raises(CrontabGuardError, match="PATH"):
        subprocess.run([sys.executable, "-c", "pass"], env={"PATH": str(link_dir)}, check=False)
    assert not _ran(decoy)


def test_a_memoctl_child_with_the_system_path_is_refused(tmp_path: Path, decoy_bin: str) -> None:
    """The incident: ``memoctl cron`` in a child with a PATH that holds a real crontab."""
    for path in (decoy_bin, _system_crontab_path()):
        with pytest.raises(CrontabGuardError, match="PATH"):
            subprocess.run(
                private_home.argv(tmp_path, "cron", "install"),
                input=b"", capture_output=True, env={"PATH": path, "HOME": str(tmp_path)},
                timeout=60, check=False,
            )  # fmt: skip


def test_the_session_tripwire_is_on_the_process_path_and_fails() -> None:
    from tests.conftest import session_tripwire

    tripwire = session_tripwire()
    assert tripwire is not None and (tripwire / "crontab").exists()
    assert str(tripwire) in os.environ["PATH"].split(os.pathsep)
    assert "exit 97" in (tripwire / "crontab").read_text()  # the guard refuses to run it here
    assert os.access(tripwire / "crontab", os.X_OK)


def test_child_env_helper_builds_a_path_that_passes(tmp_path: Path) -> None:
    from tests.child_env import child_path

    path = child_path(tmp_path)
    assert shutil.which("crontab", path=path) is None
    assert subprocess.run([sys.executable, "-c", "pass"], env={"PATH": path}).returncode == 0
    with pytest.raises(ValueError):
        child_path(tmp_path, tools=["crontab"])
    with pytest.raises(AssertionError):  # a fake_bin without a crontab in it
        child_path(tmp_path, fake_bin=tmp_path / "nowhere")


def test_child_env_helper_puts_the_fake_first(fake_crontab: tuple[str, Path]) -> None:
    from tests.child_env import child_path

    script, state = fake_crontab
    path = child_path(Path(script).parent.parent, fake_bin=Path(script).parent, tools=["sh"])
    assert shutil.which("crontab", path=path) == script
    assert shutil.which("sh", path=path) is not None
    result = subprocess.run([script, "-l"], env={"PATH": path, "FAKE_CRONTAB_DIR": str(state)})
    assert result.returncode == 1  # the fake: no table stored
