"""Keep a test from reaching the real home through the password database.

The tool takes the home from ``pwd.getpwuid(...).pw_dir``, never from ``HOME``, so the private
``HOME`` of ``tests/conftest.py`` does not protect the real ``~/.local/state/memoctl`` from a
test that runs the real gateway. Two layers (see the ``conftest`` docstring):

- :class:`GuardedPasswd` wraps a password entry and raises ``AssertionError`` when its
  ``pw_dir`` is the real home (``conftest`` patches ``pwd.getpwuid`` and ``pwd.getpwnam`` with
  it for every test that is not marked ``real_passwd``).
- :func:`real_state_snapshot` and :func:`changed_paths` are the session backstop: whether the
  real state and config directories appeared or changed while the session ran (this also
  covers child processes).
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
HOME_FIELD = 5  # index of pw_dir in a password entry

WATCHED = (".local/state/memoctl", ".config/memoctl")


class RealHomeError(AssertionError):
    pass


class GuardedPasswd:
    """A password entry that refuses to give out the real home directory."""

    def __init__(self, entry: pwd.struct_passwd, real_home: str = REAL_HOME) -> None:
        self._entry = entry
        self._real_home = real_home

    def _refuse(self) -> RealHomeError:
        return RealHomeError(
            "this test read the real home directory from the password database "
            f"({self._real_home}); the tool would write there. Give the test a fake gateway "
            "or a wrapper from tests/private_home.py, or mark it `real_passwd` if it only "
            "compares the value."
        )

    @property
    def pw_dir(self) -> str:
        if self._entry.pw_dir == self._real_home:
            raise self._refuse()
        return self._entry.pw_dir

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry, name)

    def __getitem__(self, index: int) -> Any:
        if index == HOME_FIELD or index == HOME_FIELD - len(self._entry):
            return self.pw_dir
        return self._entry[index]

    def __iter__(self) -> Iterator[Any]:
        return iter([self[i] for i in range(len(self._entry))])

    def __len__(self) -> int:
        return len(self._entry)


def guarded(lookup: Any, real_home: str = REAL_HOME) -> Any:
    """``lookup`` (``pwd.getpwuid`` or ``pwd.getpwnam``) returning guarded entries."""

    def wrapper(*args: Any, **kwargs: Any) -> GuardedPasswd:
        return GuardedPasswd(lookup(*args, **kwargs), real_home)

    return wrapper


def real_state_snapshot(home: str | Path = REAL_HOME) -> dict[str, int | None]:
    """The mtime (ns) of each watched directory below ``home``, ``None`` when absent."""
    result: dict[str, int | None] = {}
    for rel in WATCHED:
        try:
            result[rel] = (Path(home) / rel).stat().st_mtime_ns
        except OSError:
            result[rel] = None
    return result


def changed_paths(before: dict[str, int | None], after: dict[str, int | None]) -> list[str]:
    """The watched directories that appeared, vanished or changed between two snapshots."""
    return [rel for rel in WATCHED if before.get(rel) != after.get(rel)]
