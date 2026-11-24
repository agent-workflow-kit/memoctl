"""Run the real ``memoctl`` entry point in a child process with a private home.

The tool reads the home from the password database, not from ``HOME``, so a child started with a
private ``HOME`` would still use the real ``~/.local/state/memoctl``. The wrapper script
(``tests/private_home_wrapper.py``) replaces ``pw_dir`` of ``pwd.getpwuid`` with its first
argument, then runs ``python -m memoctl`` in the same interpreter; production code is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

WRAPPER = Path(__file__).with_name("private_home_wrapper.py")


def argv(home: Path, *args: str) -> list[str]:
    """The command line of a ``memoctl`` child whose home is ``home``."""
    return [sys.executable, "-I", str(WRAPPER), str(home), *args]
