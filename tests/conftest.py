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

- Child environment rule (every process, not only ``crontab``): before ``Popen`` (and
  ``subprocess._fork_exec``, and the ``posix_spawn`` paths ``subprocess`` takes) starts anything,
  the ``PATH`` of the child's effective environment (the ``env=`` given, or ``os.environ`` when
  none) is searched for ``crontab`` with ``shutil.which``. If one is found it must resolve below
  the pytest base temporary directory (a fake) or be the session tripwire; otherwise
  ``CrontabGuardError`` is raised before any process exists. This closes the hole the other checks
  leave: a child (such as a ``memoctl`` child through ``tests/private_home.py``) that looks
  ``crontab`` up itself inside its own process, which no in-process guard can see. Empty and
  relative ``PATH`` entries mean the child's working directory. There is no switch to disable it.
  ``tests/child_env.py`` builds a ``PATH`` that passes.

Wrappers: ``env``, ``nohup``, ``timeout``, ``xargs``, ``nice``, ``sudo`` and the like (see
``WRAPPERS``) can set the child's ``PATH`` inside argv, after the environment above was checked.
An argument ``PATH=...`` after such a wrapper is checked like an ``env=`` ``PATH``; ``env -i``,
``-S``/``--split-string``, ``-u``/``--unset`` (and ``-``) are refused outright; a ``sh -c`` string
that assigns ``PATH`` is refused. ``os.forkpty`` is blocked with ``os.fork`` (``os.openpty`` only
opens descriptors and stays available).

Out of scope: ``multiprocessing``, ``ctypes`` and a direct ``_posixsubprocess.fork_exec`` call
bypass every wrapper above. They are not used by the tests; the tripwires first on ``PATH`` (the
session one and one per test) are the backstop for a lookup that escapes the guard.

Session backstop: ``pytest_sessionstart`` creates a tripwire directory with a ``crontab`` that
always exits 97 below the session's pytest base temporary directory (not a bare ``/tmp``
directory, which a cleaner may prune) and puts it first on ``os.environ["PATH"]`` for the whole
session (restored and removed at the end; each test re-creates the stub if it vanished), so code
that runs outside the per-test fixtures finds the stub, never the real crontab.
``pytest_sessionstart`` and ``pytest_sessionfinish`` compare the real
``~/.local/state/memoctl`` and ``~/.config/memoctl`` before and after the run and fail the
session (exit status 1) when either was absent at the start and exists at the end.
This also covers child processes.

A real child process that needs a home uses ``tests/private_home.py`` (``argv(home, ...)``): a
wrapper that gives ``pwd.getpwuid`` a private ``pw_dir`` and runs ``python -m memoctl``.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests import passwd_guard

FAKEBIN = Path(__file__).parent / "fakebin"


class CrontabGuardError(AssertionError):
    pass


TRIPWIRE_SCRIPT = "#!/bin/sh\necho 'tripwire: the real crontab must not run' >&2\nexit 97\n"
_SESSION: dict[str, Any] = {}  # "tripwire": Path, "path": the PATH to restore


def _write_tripwire(directory: Path) -> None:
    trap = directory / "crontab"
    trap.write_text(TRIPWIRE_SCRIPT)
    trap.chmod(0o755)


def session_tripwire() -> Path | None:
    """The session-wide directory whose ``crontab`` always fails (``None`` outside a session)."""
    return _SESSION.get("tripwire")


def ensure_session_tripwire() -> None:
    """Rewrite the session stub when it vanished (it lives below the session's base temp dir)."""
    tripwire = session_tripwire()
    if tripwire is not None:
        tripwire.mkdir(parents=True, exist_ok=True)
        stub = tripwire / "crontab"
        if not stub.exists() or stub.read_text() != TRIPWIRE_SCRIPT or not os.access(stub, os.X_OK):
            _write_tripwire(tripwire)


def _path_of(env: Mapping[Any, Any] | list[Any] | None) -> str:
    """The ``PATH`` a child started with ``env`` sees (``os.environ`` when ``env`` is ``None``)."""
    if env is None:
        env = os.environ
    if isinstance(env, Mapping):
        for key, value in env.items():
            if os.fsdecode(key) == "PATH":
                return os.fsdecode(value)
    else:  # the "NAME=value" list that subprocess hands to _fork_exec
        for item in env:
            text = os.fsdecode(item)
            if text.startswith("PATH="):
                return text[len("PATH=") :]
    return os.defpath  # a child without PATH searches the default path


def check_child_path(
    env: Mapping[Any, Any] | list[Any] | None, cwd: Any, bases: list[Path]
) -> None:
    """Refuse a child whose ``PATH`` can resolve ``crontab`` to anything but a fake.

    ``crontab`` found through the child's ``PATH`` must resolve below one of ``bases`` (the pytest
    base temporary directory, the session tripwire). Empty and relative entries are taken
    relative to the child's ``cwd`` (``None`` means the current directory).
    """
    here = Path(os.fsdecode(cwd)) if cwd is not None else Path.cwd()
    entries = [str((here / (entry or ".")).absolute()) for entry in _path_of(env).split(os.pathsep)]
    found = shutil.which("crontab", path=os.pathsep.join(entries))
    if found is None:
        return
    resolved = Path(found).resolve()
    if not any(base in resolved.parents for base in bases):
        raise CrontabGuardError(
            f"refusing to start a process whose PATH finds a crontab at {found!r}: a child may "
            f"only find a fake crontab below {bases[0]} (see tests/child_env.py)"
        )


@pytest.fixture(autouse=True)
def _private_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    ensure_session_tripwire()
    root = tmp_path_factory.mktemp("env")
    home = root / "home"
    home.mkdir()
    tripwire = root / "tripwire"
    tripwire.mkdir()
    _write_tripwire(tripwire)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root / "run"))
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("PATH", f"{tripwire}{os.pathsep}{os.environ.get('PATH', '')}")
    # Sanity: with the tripwire first, a PATH lookup of crontab must end below the base dir.
    base = tmp_path_factory.getbasetemp().resolve()
    check_child_path(None, None, [base])
    found = shutil.which("crontab")
    assert found is not None and base in Path(found).resolve().parents, found
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
    # Below the session's pytest base temporary directory (not a bare /tmp directory a cleaner
    # may prune), so the guard's single allowed base covers the tripwire too.
    factory = session.config._tmp_path_factory  # type: ignore[attr-defined]
    tripwire = (factory.getbasetemp() / "session-tripwire").resolve()
    tripwire.mkdir(parents=True, exist_ok=True)
    _write_tripwire(tripwire)
    _SESSION["tripwire"] = tripwire
    _SESSION["path"] = os.environ.get("PATH")
    os.environ["PATH"] = f"{tripwire}{os.pathsep}{os.environ.get('PATH', '')}"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    tripwire = _SESSION.pop("tripwire", None)
    original = _SESSION.pop("path", None)
    if tripwire is not None:
        if original is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original
        shutil.rmtree(tripwire, ignore_errors=True)
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
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    *(f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
    *(f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
)


def _under(path: str, base: Path) -> bool:
    """True for an explicit path (with a directory part) that resolves below ``base``."""
    return os.sep in path and base in Path(path).resolve().parents


WRAPPERS = {
    "env", "nohup", "timeout", "xargs", "nice", "ionice", "setsid", "stdbuf", "sudo", "doas",
    "chroot", "unshare", "command", "exec", "busybox", "flock", "time", "taskset", "chrt",
    "runuser", "su", "nsenter", "setpriv", "strace", "ltrace", "watch", "script",
}  # fmt: skip
_ASSIGNS_PATH = re.compile(r"(?<![A-Za-z0-9_])PATH\+?=")
_SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*$")
# env options that reset or edit the environment, or take a value that hides what follows
_ENV_REFUSED_LONG = ("--ignore-environment", "--split-string", "--unset")


def _check_wrapper_environment(argv: list[str], base: Path) -> None:
    """Refuse a wrapper or shell that sets the child's ``PATH`` from inside argv.

    ``check_child_path`` sees only the environment handed to ``Popen``; ``env PATH=... sh`` or
    ``sh -c 'PATH=...'`` replaces it afterwards, so the assigned ``PATH`` is checked the same
    way, and the forms that cannot be checked are refused.
    """
    names = [os.path.basename(a) for a in argv]
    if not argv or not (set(names) & (WRAPPERS | SHELLS)):
        return
    if names[0] == "env":
        _check_env_options(argv)
    for i, name in enumerate(names):
        if name in WRAPPERS:
            for arg in argv[i + 1 :]:
                if arg.startswith("PATH="):
                    check_child_path([arg], None, [base])
        if name in SHELLS:
            rest = argv[i + 1 :]
            if any(_SHELL_C.match(a) for a in rest) and any(_ASSIGNS_PATH.search(a) for a in rest):
                raise CrontabGuardError(f"refusing a shell command that assigns PATH: {argv!r}")


def _check_env_options(argv: list[str]) -> None:
    """``env -i/-S/-u`` (and ``-``) reset or hide the environment: a test never needs them."""
    values = {"-C", "-P", "--chdir"}  # options whose value is the next argument
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--":
            return
        if arg == "-" or arg.startswith(_ENV_REFUSED_LONG):
            raise CrontabGuardError(f"refusing env option {arg!r} (it resets the PATH): {argv!r}")
        if arg.startswith("-") and not arg.startswith("--"):
            if set(arg[1:]) & {"i", "S", "u"}:
                raise CrontabGuardError(f"refusing env option {arg!r} (edits the PATH): {argv!r}")
            skip = arg in values
        elif arg.startswith("--"):
            skip = arg in values
        elif "=" not in arg:
            return  # the command: what follows are its own arguments


def check_process(argv: list[str], executables: list[str], base: Path) -> None:
    """Refuse anything that could reach the real crontab.

    ``argv`` is the program and its arguments; ``executables`` the candidate paths the program
    may be loaded from (the ``executable=`` argument, or one per ``PATH`` entry).
    """
    program = argv[0] if argv else ""
    if os.path.basename(program) in SHELLS and "-c" in argv[1:]:
        if any("crontab" in a for a in argv[1:]):
            raise CrontabGuardError(f"refusing shell command mentioning crontab: {argv!r}")
    _check_wrapper_environment(argv, base)
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
    bases = [base]  # the session tripwire lives below the base too
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
            # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds,
            # shell, cwd, env, ...): cwd is the 9th and env the 10th positional after args.
            check_child_path(
                kw.get("env", a[9] if len(a) > 9 else None),
                kw.get("cwd", a[8] if len(a) > 8 else None),
                bases,
            )
            super().__init__(args, *a, **kw)
            started.append(self)

    def guarded_fork_exec(args: Any, executable_list: Any, *a: Any, **kw: Any) -> Any:
        # Reached by every Popen, including one imported before the guard was installed.
        argv = [os.fsdecode(x) for x in args] if args is not None else []
        check_process(argv, [os.fsdecode(x) for x in executable_list], base)
        # CPython 3.12-3.14: _fork_exec(args, executable_list, close_fds, pass_fds, cwd, env, ...)
        if len(a) < 4:
            raise CrontabGuardError("unexpected subprocess._fork_exec signature")
        check_child_path(a[3], a[2], bases)
        return real_fork_exec(args, executable_list, *a, **kw)

    def from_subprocess() -> bool:
        return sys._getframe(2).f_globals.get("__name__") == "subprocess"

    def guarded_posix_spawn(path: Any, argv: Any, *a: Any, **kw: Any) -> Any:
        if not from_subprocess():
            raise CrontabGuardError("os.posix_spawn is not allowed in tests")
        check_process([os.fsdecode(x) for x in argv], [os.fsdecode(path)], base)
        check_child_path(a[0] if a else kw.get("env"), None, bases)
        return real_posix_spawn(path, argv, *a, **kw)

    def guarded_posix_spawnp(path: Any, argv: Any, *a: Any, **kw: Any) -> Any:
        if not from_subprocess():
            raise CrontabGuardError("os.posix_spawnp is not allowed in tests")
        check_process([os.fsdecode(x) for x in argv], [], base)
        check_child_path(a[0] if a else kw.get("env"), None, bases)
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
