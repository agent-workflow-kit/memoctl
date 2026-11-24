"""Shared fixtures: no test may touch the user's real home, crontab or stray processes.

Autouse guards:

- ``_private_environment``: HOME, XDG_* and ``LC_ALL`` point at private values and ``PATH``
  starts with a ``crontab`` that always fails, so a program that resolves ``crontab`` itself
  never reaches the real one.
- ``_process_guard``: wraps ``subprocess.Popen`` and ``subprocess._fork_exec`` (so a ``Popen``
  imported before the guard is covered too) to refuse any ``crontab`` program (as argv[0], as
  ``executable=`` or inside a ``sh -c`` command) that is not given as an explicit path under
  the pytest base temporary directory (a fake). ``os.system``, ``os.exec*``, ``os.spawn*``,
  ``os.posix_spawn*`` and ``os.fork`` raise when called directly. Every process a test
  starts is recorded so the exact ones still running at teardown are killed (by the ``Popen``
  object that launched them, never by name or pattern).
- ``_passwd_guard``: the tool takes its home from the password database, not from ``HOME``, so
  ``pwd.getpwuid`` and ``pwd.getpwnam`` are patched to return entries whose ``pw_dir`` raises
  ``AssertionError`` when it is the real home (``tests/passwd_guard.py``). Other fields still
  work. A test that must read the real value (it only compares it) takes the ``real_passwd``
  marker; a test that patches ``pwd.getpwuid`` itself replaces the guard. The guard works only
  because code reads ``pwd.getpwuid`` as a module attribute: ``from pwd import getpwuid``
  bypasses it (the session backstop still catches a write after the fact).

Session backstop: ``pytest_sessionstart`` and ``pytest_sessionfinish`` compare the real
``~/.local/state/memoctl`` and ``~/.config/memoctl`` before and after the run and fail the
session (exit status 1) when either was absent at the start and exists at the end.
This also covers child processes.

A real child process that needs a home uses ``tests/private_home.py`` (``argv(home, ...)``): a
wrapper that gives ``pwd.getpwuid`` a private ``pw_dir`` and runs ``python -m memoctl``.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import passwd_guard

FAKEBIN = Path(__file__).parent / "fakebin"


class CrontabGuardError(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _private_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path_factory.mktemp("env")
    home = root / "home"
    home.mkdir()
    tripwire = root / "tripwire"
    tripwire.mkdir()
    trap = tripwire / "crontab"
    trap.write_text("#!/bin/sh\necho 'tripwire: the real crontab must not run' >&2\nexit 97\n")
    trap.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root / "run"))
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("PATH", f"{tripwire}{os.pathsep}{os.environ.get('PATH', '')}")
    return home


@pytest.fixture(autouse=True)
def _passwd_guard(
    request: pytest.FixtureRequest, _private_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real_passwd") is not None:
        return
    monkeypatch.setattr(pwd, "getpwuid", passwd_guard.guarded(pwd.getpwuid))
    monkeypatch.setattr(pwd, "getpwnam", passwd_guard.guarded(pwd.getpwnam))


_BEFORE: dict[str, int | None] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _BEFORE.update(passwd_guard.real_state_snapshot())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changed = passwd_guard.changed_paths(_BEFORE, passwd_guard.real_state_snapshot())
    if changed:
        sys.stderr.write(
            f"\nISOLATION BREACH: {', '.join(changed)} below {passwd_guard.REAL_HOME} appeared "
            "or changed during the test session; a test reached the real home.\n"
        )
        session.exitstatus = 1


SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
OS_BLOCKED = (
    "system",
    "fork",
    "posix_spawn",
    "posix_spawnp",
    *(f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
    *(f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
)


def _under(path: str, base: Path) -> bool:
    """True for an explicit path (with a directory part) that resolves below ``base``."""
    return os.sep in path and base in Path(path).resolve().parents


def check_process(argv: list[str], executables: list[str], base: Path) -> None:
    """Refuse anything that could reach the real crontab.

    ``argv`` is the program and its arguments; ``executables`` the candidate paths the program
    may be loaded from (the ``executable=`` argument, or one per ``PATH`` entry).
    """
    program = argv[0] if argv else ""
    if os.path.basename(program) in SHELLS and "-c" in argv[1:]:
        if any("crontab" in a for a in argv[1:]):
            raise CrontabGuardError(f"refusing shell command mentioning crontab: {argv!r}")
    # A wrapper (env, nohup, timeout, xargs, ...) may run a crontab given later in argv.
    for arg in argv[1:]:
        if os.path.basename(arg) == "crontab" and not _under(arg, base):
            raise CrontabGuardError(f"refusing {program!r} with a crontab argument: {argv!r}")
    names = [program, *executables]
    if not any(os.path.basename(n) == "crontab" for n in names):
        return
    # An explicit path is the single executable given, or argv[0] when it has a directory.
    # Several candidates mean a PATH search, which could end at the real crontab.
    target = executables[0] if executables else program
    explicit = len(executables) <= 1 and os.sep in target
    resolved = Path(target).resolve() if explicit else None
    if resolved is None or base not in resolved.parents:
        raise CrontabGuardError(
            f"refusing to run {program!r} (executable {executables!r}): only a fake crontab "
            f"given as an explicit path under {base} may run in tests"
        )


@pytest.fixture(autouse=True)
def _process_guard(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    base = tmp_path_factory.getbasetemp().resolve()
    real_popen = subprocess.Popen
    real_fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]
    real_posix_spawn = os.posix_spawn
    real_posix_spawnp = os.posix_spawnp
    started: list[Any] = []

    class GuardedPopen(real_popen):  # type: ignore[misc, valid_type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            executable = kw.get("executable", a[1] if len(a) > 1 else None)
            if isinstance(args, (str, bytes)):
                # A command string (shell form): never allow it to mention crontab.
                if "crontab" in os.fsdecode(args):
                    raise CrontabGuardError(f"refusing shell command mentioning crontab: {args!r}")
                argv: list[str] = []
            else:
                argv = [os.fsdecode(x) for x in args]
            check_process(argv, [os.fsdecode(executable)] if executable is not None else [], base)
            super().__init__(args, *a, **kw)
            started.append(self)

    def guarded_fork_exec(args: Any, executable_list: Any, *a: Any, **kw: Any) -> Any:
        # Reached by every Popen, including one imported before the guard was installed.
        argv = [os.fsdecode(x) for x in args] if args is not None else []
        check_process(argv, [os.fsdecode(x) for x in executable_list], base)
        return real_fork_exec(args, executable_list, *a, **kw)

    def from_subprocess() -> bool:
        return sys._getframe(2).f_globals.get("__name__") == "subprocess"

    def guarded_posix_spawn(path: Any, argv: Any, *a: Any, **kw: Any) -> Any:
        if not from_subprocess():
            raise CrontabGuardError("os.posix_spawn is not allowed in tests")
        check_process([os.fsdecode(x) for x in argv], [os.fsdecode(path)], base)
        return real_posix_spawn(path, argv, *a, **kw)

    def guarded_posix_spawnp(path: Any, argv: Any, *a: Any, **kw: Any) -> Any:
        if not from_subprocess():
            raise CrontabGuardError("os.posix_spawnp is not allowed in tests")
        check_process([os.fsdecode(x) for x in argv], [], base)
        return real_posix_spawnp(path, argv, *a, **kw)

    def blocked(name: str) -> Any:
        def refuse(*a: Any, **kw: Any) -> Any:
            raise CrontabGuardError(f"os.{name} is not allowed in tests (it can start a process)")

        return refuse

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(subprocess, "_fork_exec", guarded_fork_exec)
    for name in OS_BLOCKED:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, blocked(name))
    monkeypatch.setattr(os, "posix_spawn", guarded_posix_spawn)
    monkeypatch.setattr(os, "posix_spawnp", guarded_posix_spawnp)
    try:
        yield
    finally:
        for proc in started:
            # poll() is None only while this exact child is unreaped, so its pid is still ours.
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.fixture
def fake_crontab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """A copy of ``tests/fakebin/crontab`` under the pytest tmp dir and its state directory.

    ``FAKE_CRONTAB_DIR`` is set for the test; the fake refuses to run without it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "crontab"
    shutil.copy2(FAKEBIN / "crontab", script)
    state = tmp_path / "crontab-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_CRONTAB_DIR", str(state))
    yield str(script), state
