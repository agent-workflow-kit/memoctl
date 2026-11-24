"""The ``PATH`` of a child process a test starts, built so it can never find a real crontab.

A child that looks ``crontab`` up itself (``memoctl cron`` through ``tests/private_home.py``) is
beyond the in-process guards, so the only safe input is a ``PATH`` that cannot resolve a real one.
``conftest.py`` refuses (``CrontabGuardError``, before any process exists) every child whose
``PATH`` finds a ``crontab`` outside the pytest base temporary directory; this module builds a
``PATH`` that passes: the fake-bin directory (when the test has a fake ``crontab``), an empty
directory and, for a child that needs a few system programs, a directory of symlinks to just those
programs. Never ``/usr/bin:/bin``: a real ``crontab`` lives there.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path

FAKE_CRONTAB_TOOLS = ("cat", "touch", "wc")  # what tests/fakebin/crontab calls besides builtins


def child_path(root: Path, *, fake_bin: Path | None = None, tools: Iterable[str] = ()) -> str:
    """A ``PATH`` string for a child; ``root`` is a directory below the pytest tmp dir.

    ``fake_bin`` is a directory holding a fake ``crontab`` (it comes first); without it no
    ``crontab`` exists on the path. ``tools`` names system programs the child needs (``sh``,
    ``bash``, ...): each is linked into a private directory, never ``crontab``.
    """
    empty = root / "child-path-empty"
    empty.mkdir(parents=True, exist_ok=True)
    entries: list[str] = [str(empty)]
    wanted = list(tools)
    if wanted:
        linked = root / "child-path-tools"
        linked.mkdir(parents=True, exist_ok=True)
        for name in wanted:
            if name == "crontab" or os.sep in name:
                raise ValueError(f"not a tool a child may use: {name!r}")
            target = shutil.which(name)
            if target is None:
                raise FileNotFoundError(f"tool {name!r} is not installed")
            link = linked / name
            if not link.is_symlink():
                link.symlink_to(target)
        entries.append(str(linked))
    if fake_bin is not None:
        entries.insert(0, str(fake_bin))
    path = os.pathsep.join(entries)
    found = shutil.which("crontab", path=path)
    if fake_bin is None:
        assert found is None, f"child PATH {path!r} finds a crontab at {found!r}"
    else:
        assert found is not None and Path(found).resolve() == (fake_bin / "crontab").resolve(), (
            f"child PATH {path!r} does not resolve crontab to the fake in {fake_bin}: {found!r}"
        )
    return path
