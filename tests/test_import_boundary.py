"""Only ``gateway.py`` may reach outside the process (design guide section 14).

The scanner reads each module's syntax tree, so a banned import hidden in a function, an
aliased import or a dynamic ``__import__`` is still found. It does not run any code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "memoctl"
GATEWAY = SRC / "gateway.py"

BANNED_MODULES = {"subprocess", "time", "pwd", "random", "secrets", "importlib", "ctypes"}
# Attribute calls that reach the host (clock reads are matched by CLOCK_SUFFIXES).
BANNED_CALLS = {
    "os.urandom",
    "os.getuid",
    "os.system",
    "os.popen",
    "os.get_terminal_size",
    "shutil.get_terminal_size",
    "shutil.which",
    "Path.home",
    "Path.cwd",
    "os.path.expanduser",
}
CLOCK_SUFFIXES = ("datetime.now", "datetime.utcnow", "datetime.today", "date.today")
BANNED_NAME_CALLS = {"__import__", "open", "input", "print", "exec", "eval"}
BANNED_STREAMS = {"sys.stdin", "sys.stdout", "sys.stderr", "sys.__stdin__", "sys.__stdout__"}
# Names that mean "the process's descriptors" when imported straight from sys or os.
BANNED_FROM = {
    "sys": {"stdin", "stdout", "stderr"},
    "os": {"urandom", "getuid", "system", "popen", "environ", "getenv", "fdopen"},
}


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def violations(source: str, filename: str = "<module>") -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    found.append(f"{where}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in BANNED_MODULES:
                found.append(f"{where}: from {node.module} import ...")
            for alias in node.names:
                if alias.name in BANNED_FROM.get(top, set()):
                    found.append(f"{where}: from {top} import {alias.name}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_NAME_CALLS:
                found.append(f"{where}: call {node.func.id}()")
            name = _dotted(node.func)
            if name is not None and name in BANNED_CALLS:
                found.append(f"{where}: call {name}()")
            if name is not None and name.endswith(CLOCK_SUFFIXES):
                found.append(f"{where}: clock read {name}()")
        elif isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name in BANNED_STREAMS or name in {"os.environ", "os.getenv"}:
                found.append(f"{where}: use of {name}")
    return sorted(set(found))


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p != GATEWAY)


def test_there_are_modules_to_check() -> None:
    assert any(p.name == "cli.py" for p in _modules())


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_reaches_outside_only_through_the_gateway(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8"), path.name) == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import time",
        "from time import time as t",
        "import pwd",
        "import random",
        "from random import randint",
        "import secrets",
        "import importlib",
        "def f():\n    import subprocess",
        "x = __import__('subprocess')",
        "import datetime\ndatetime.datetime.now()",
        "from datetime import datetime\ndatetime.now()",
        "from datetime import datetime\ndatetime.utcnow()",
        "from datetime import date\ndate.today()",
        "import sys\nsys.stdin.read()",
        "import sys\nsys.stderr.write('x')",
        "from sys import stdout",
        "import os\nos.urandom(3)",
        "from os import urandom",
        "import os\nos.environ['HOME']",
        "import os\nos.getenv('HOME')",
        "from pathlib import Path\nPath.home()",
        "open('f')",
        "print('x')",
        "input()",
    ],
)
def test_scanner_flags_each_kind_of_direct_access(source: str) -> None:
    assert violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import json\nimport re\nfrom pathlib import Path\nPath('x').name",
        "from datetime import datetime, UTC\ndatetime(2026, 1, 1, tzinfo=UTC)",
        "def f(gateway):\n    return gateway.now()",
    ],
)
def test_scanner_accepts_harmless_code(source: str) -> None:
    assert violations(source) == []
