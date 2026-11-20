"""Shared fixtures: no test may touch the user's real home, crontab or stray processes.

Autouse guards:

- ``_private_environment``: HOME, XDG_* and ``LC_ALL`` point at private values and ``PATH``
  starts with a ``crontab`` that always fails, so a program that resolves ``crontab`` itself
  never reaches the real one.
- ``_process_guard``: wraps ``subprocess.Popen`` to refuse any ``crontab`` program that is not
  given as an explicit path under the pytest base temporary directory (a fake), and records
  every process a test starts so the exact ones still running at teardown are killed (by the
  ``Popen`` object that launched them, never by name or pattern).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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
def _process_guard(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    base = tmp_path_factory.getbasetemp().resolve()
    real_popen = subprocess.Popen
    started: list[Any] = []

    class GuardedPopen(real_popen):  # type: ignore[misc, valid_type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            if isinstance(args, (str, bytes)):
                # A command string (shell form): never allow it to mention crontab.
                if "crontab" in os.fsdecode(args):
                    raise CrontabGuardError(f"refusing shell command mentioning crontab: {args!r}")
                program = ""
            else:
                argv = [os.fsdecode(x) for x in args]
                program = argv[0] if argv else ""
            if os.path.basename(program) == "crontab":
                resolved = Path(program).resolve() if os.sep in program else None
                if resolved is None or base not in resolved.parents:
                    raise CrontabGuardError(
                        f"refusing to run {program!r}: only a fake crontab given as an "
                        f"explicit path under {base} may run in tests"
                    )
            super().__init__(args, *a, **kw)
            started.append(self)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
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
