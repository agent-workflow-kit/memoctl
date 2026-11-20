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

# Whole modules that reach outside. todo(#9): sqlite3 opens files itself and is not banned
# yet; the issue that adds the store decides how it goes through the gateway. todo(#10):
# signal and threading are likewise left to the issue that needs them. io is not banned
# (StringIO is harmless); io.open is listed below.
BANNED_MODULES = {
    "subprocess",
    "time",
    "pwd",
    "random",
    "secrets",
    "importlib",
    "ctypes",
    "asyncio",
    "multiprocessing",
    "pty",
    "socket",
    "urllib",
    "http",
    "uuid",
    "tempfile",
    "getpass",
    "shutil",
    "glob",
}
# Canonical dotted names (after alias resolution) that reach the host, whether called or
# merely referenced. todo(#6): sys.argv is not banned because the CLI needs it.
BANNED_REFS = {
    # builtins, also through the ``builtins`` module
    *{
        f"{prefix}{name}"
        for prefix in ("", "builtins.")
        for name in ("__import__", "open", "input", "print", "exec", "eval", "breakpoint")
    },
    "__builtins__",
    "io.open",
    "io.open_code",
    "codecs.open",
    "sys.modules",
    # the process's descriptors
    "sys.stdin",
    "sys.stdout",
    "sys.stderr",
    "sys.__stdin__",
    "sys.__stdout__",
    "sys.__stderr__",
    # os: environment, identity, terminal, processes, files
    *{
        f"os.{name}"
        for name in (
            "urandom getuid get_terminal_size environ environb getenv getenvb putenv fdopen "
            "getlogin getcwd getcwdb listdir remove unlink rename replace mkdir makedirs rmdir "
            "stat lstat open scandir walk chmod chdir read write isatty kill killpg popen "
            "fork system fstat dup dup2 close pipe geteuid getgid getegid umask fsync"
        ).split()
    },
    *{
        f"os.path.{name}"
        for name in (
            "expanduser expandvars exists isfile isdir islink getsize getmtime realpath abspath"
        ).split()
    },
    "shutil.get_terminal_size",
    "shutil.which",
    "pathlib.Path.home",
    "pathlib.Path.cwd",
    "pathlib.Path().home",
    "pathlib.Path().cwd",
    # clock reads
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.date.today",
}
# Families matched by prefix: os.exec*, os.spawn*, os.posix_spawn*.
BANNED_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
CLOCK_SUFFIXES = ("datetime.now", "datetime.utcnow", "datetime.today", "date.today")
# Path-style methods with names no other common type uses, banned as attribute calls on any
# receiver. The two gateway methods share names, so they are accepted on a receiver named
# like the gateway.
PATH_METHODS = set(
    (
        "read_text write_text read_bytes write_bytes touch mkdir unlink rmdir iterdir "
        "symlink_to readlink samefile"
    ).split()
)
# Names that str, re, dict-like or tree objects share (``'a'.replace``, ``node.resolve``): banned
# only when the receiver is a pathlib object (a class call such as ``Path(x)``, or a chain
# from one).
AMBIGUOUS_PATH_METHODS = set(
    (
        "open glob rglob exists is_file is_dir is_symlink stat lstat resolve absolute "
        "expanduser chmod rename replace match full_match walk owner group"
    ).split()
)
PATH_CLASSES = {
    f"pathlib.{name}"
    for name in "Path PurePath PosixPath WindowsPath PurePosixPath PureWindowsPath".split()
}
# Segments after ``Path(x)`` that still yield a path (``Path(x).parent.exists()``).
PATH_CHAIN = {
    "parent", "with_name()", "with_suffix()", "with_stem()", "joinpath()", "absolute()",
    "resolve()", "expanduser()", "relative_to()", "readlink()",
}  # fmt: skip
GATEWAY_METHODS = {"read_bytes", "write_bytes"}
GATEWAY_RECEIVERS = {"gateway", "_gateway", "gw"}
DYNAMIC_ATTR_CALLS = {"getattr", "setattr", "delattr"}
BANNED_TAILS = (
    {name.rsplit(".", 1)[-1] for name in BANNED_REFS}
    | PATH_METHODS
    | AMBIGUOUS_PATH_METHODS
    | BANNED_MODULES
    | {"now", "utcnow", "today", "home", "cwd"}
)
SUSPECT_MODULES = BANNED_MODULES | {"os", "sys", "builtins"}


def _resolve(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Dotted name of a Name/Attribute/Call chain with import aliases replaced by their targets."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    if isinstance(node, ast.Call):
        base = _resolve(node.func, aliases)
        return None if base is None else f"{base}()"
    return None


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map each name bound by an import (or a plain ``a = b.c`` alias) to its canonical name."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{prefix}.{alias.name}"
    # Plain aliasing (``o = os``, ``g = getattr``, ``f = os.system``), twice so chains settle.
    for _ in range(2):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.Name, ast.Attribute))
            ):
                target = _resolve(node.value, aliases)
                if target is not None:
                    aliases[node.targets[0].id] = target
    return aliases


def _is_path_object(receiver: str) -> bool:
    """True when the resolved receiver is a pathlib class instance or a path-valued chain."""
    for cls in PATH_CLASSES:
        if not receiver.startswith(cls):
            continue
        segments = _split(receiver[len(cls) :])
        if not segments:
            continue
        first, *chain = segments
        if first in ("()", "cwd()", "home()") and all(seg in PATH_CHAIN for seg in chain):
            return True
    return False


def _split(rest: str) -> list[str]:
    """Split ``().parent.with_name()`` or ``.cwd()`` into its call/attribute segments."""
    if rest.startswith("()"):
        return ["()", *[s for s in rest[2:].split(".") if s]]
    return [s for s in rest.split(".") if s]


def _is_banned_ref(name: str) -> bool:
    return name in BANNED_REFS or name.startswith(BANNED_PREFIXES) or name.endswith(CLOCK_SUFFIXES)


def violations(source: str, filename: str = "<module>") -> list[str]:
    found: list[str] = []
    tree = ast.parse(source, filename)
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    found.append(f"{where}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in BANNED_MODULES:
                found.append(f"{where}: from {module} import ...")
            for alias in node.names:
                if alias.name == "*":
                    found.append(f"{where}: from {module} import *")
                elif _is_banned_ref(f"{module}.{alias.name}"):
                    found.append(f"{where}: from {module} import {alias.name}")
        elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            name = _resolve(node, aliases)
            if name is not None and _is_banned_ref(name):
                found.append(f"{where}: use of {name}")
        if isinstance(node, ast.Call):
            found.extend(_call_violations(node, aliases, where))
    return sorted(set(found))


def _call_violations(node: ast.Call, aliases: dict[str, str], where: str) -> list[str]:
    found: list[str] = []
    func = node.func
    if isinstance(func, ast.Attribute) and (
        func.attr in PATH_METHODS or func.attr in AMBIGUOUS_PATH_METHODS
    ):
        receiver = _resolve(func.value, aliases) or ""
        gateway_call = (
            func.attr in GATEWAY_METHODS and receiver.rsplit(".", 1)[-1] in GATEWAY_RECEIVERS
        )
        banned = func.attr in PATH_METHODS or _is_path_object(receiver)
        if banned and not gateway_call:
            found.append(f"{where}: call to .{func.attr}()")
    name = _resolve(func, aliases)
    if name == "globals":
        found.append(f"{where}: call globals()")
    elif name == "vars":
        if not node.args:
            found.append(f"{where}: call vars() without an argument")
        else:
            target = _resolve(node.args[0], aliases) or ""
            if target.split(".")[0] in SUSPECT_MODULES:
                found.append(f"{where}: call vars({target})")
    elif name in DYNAMIC_ATTR_CALLS:
        attr = node.args[1] if len(node.args) > 1 else None
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            found.append(f"{where}: {name}() with a non-constant attribute name")
        elif attr.value.rsplit(".", 1)[-1] in BANNED_TAILS:
            found.append(f"{where}: {name}(..., {attr.value!r})")
    return found


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


@pytest.mark.parametrize(
    "source",
    [
        # aliased imports
        "import os as o\no.system('x')",
        "import os as o\no.environ",
        "import os.path as p\np.expanduser('~')",
        "from os import path\npath.expanduser('~')",
        "from os import path as p\np.expanduser('~')",
        "from os.path import expanduser",
        "from os import system as s\ns('x')",
        "from datetime import datetime as D\nD.now()",
        "import datetime as dt\ndt.datetime.now()",
        "import datetime as dt\ndt.date.today()",
        "from pathlib import Path as P\nP.home()",
        "from pathlib import Path as P\nP().cwd()",
        "import pathlib as pl\npl.Path.home()",
        "from sys import stdout as out\nout.write('x')",
        "import sys as s\ns.stderr.write('x')",
        "import sys as s\ns.modules",
        "import builtins as b\nb.open('f')",
        "from builtins import open as o",
        "from io import open as o\no('f')",
        "import io\nio.open('f')",
        "import codecs\ncodecs.open('f')",
        "import subprocess as sp",
        "from subprocess import Popen as P",
        "import os as o\nf = o.system\nf('x')",
        "import os\nrun = os.system\nrun('x')",
        "from os import *",
        # new banned modules
        "import asyncio",
        "import multiprocessing",
        "from multiprocessing import Process",
        "import pty",
        "import socket",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "from http import client",
        "import uuid",
        "import tempfile",
        "import getpass",
        "import shutil",
        "import shutil as sh",
        "from shutil import copy",
        "import glob",
        # new banned os calls
        "import os\nos.execv('a', [])",
        "import os\nos.execvp('a', [])",
        "import os\nos.spawnl(0, 'a')",
        "import os\nos.posix_spawn('a', [], {})",
        "import os\nos.posix_spawnp('a', [], {})",
        "import os\nos.fork()",
        "import os\nos.getlogin()",
        "import os\nos.getcwd()",
        "import os\nos.listdir('.')",
        "import os\nos.remove('f')",
        "import os\nos.unlink('f')",
        "import os\nos.rename('a', 'b')",
        "import os\nos.mkdir('d')",
        "import os\nos.stat('f')",
        "import os\nos.open('f', 0)",
        "import os\nos.fstat(0)",
        "import os\nos.dup2(3, 1)",
        "import os\nos.dup(1)",
        "import os\nos.close(1)",
        "import os\nos.pipe()",
        "import os\nos.geteuid()",
        "from os import execv",
        # path-style methods, on any receiver
        "from pathlib import Path\nPath('f').read_text()",
        "from pathlib import Path\nPath('f').write_text('x')",
        "def f(p):\n    return p.read_bytes()",
        "def f(p):\n    p.write_bytes(b'')",
        "def f(p):\n    p.touch()",
        "def f(p):\n    p.mkdir()",
        "def f(p):\n    p.unlink()",
        "def f(p):\n    p.rmdir()",
        "def f(p):\n    p.iterdir()",
        "def f(p):\n    p.symlink_to('x')",
        "def f(p):\n    p.readlink()",
        "def f(p):\n    p.samefile(p)",
        # ambiguous names, banned when the receiver is a pathlib object
        "from pathlib import Path\nPath('a').replace('b')",
        "from pathlib import Path\nPath('a').rename('b')",
        "from pathlib import Path\nPath('a').open()",
        "from pathlib import Path\nPath('a').stat()",
        "from pathlib import Path\nPath('a').resolve()",
        "from pathlib import Path\nPath('a').exists()",
        "from pathlib import Path\nPath('a').glob('*')",
        "from pathlib import Path as P\nP('a').open()",
        "from pathlib import Path as P\nP('a').replace('b')",
        "import pathlib\npathlib.Path('a').stat()",
        "import pathlib as pl\npl.PurePath('a').resolve()",
        "from pathlib import PurePosixPath\nPurePosixPath('a').match('b')",
        "from pathlib import Path\nPath.cwd().exists()",
        "from pathlib import Path\nPath('a').parent.stat()",
        "from pathlib import Path\nPath('a').with_name('b').exists()",
        "from pathlib import Path\nPath('a').expanduser()",
        # a bare reference to open, not only a call
        "f = open",
        "def f(opener=open):\n    pass",
        "list(map(open, ['a']))",
        # reaching a banned thing dynamically
        "import os\ngetattr(os, 'system')('x')",
        "import os\ngetattr(os, 'environ')",
        "def f(x, n):\n    return getattr(x, n)",
        "def f(x, n):\n    setattr(x, n, 1)",
        "def f(x):\n    setattr(x, 'stdout', 1)",
        "g = getattr\ng(object, 'x' + 'y')",
        "globals()['o' + 's']",
        "import os\nvars(os)['system']",
        "vars()",
        "__builtins__",
    ],
)
def test_scanner_flags_aliases_and_new_bans(source: str) -> None:
    assert violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        # gateway-routed code
        "def f(gateway, p):\n    return gateway.read_bytes(p)",
        "def f(gateway, p):\n    gateway.write_bytes(p, b'x')",
        "class C:\n    def f(self):\n        self.gateway.write_bytes(self.p, b'x')",
        "def f(gateway):\n    return gateway.home() / 'x', gateway.now()",
        "def f(gateway):\n    gateway.write_out('x')\n    return gateway.run(['ls'])",
        "def f(gateway):\n    return gateway.crontab_read()",
        "def f(gateway):\n    return gateway.read_stdin_bounded(9), gateway.random_hex(4)",
        # harmless neighbours of the banned names
        "import os\nos.path.join('a', 'b')",
        "from os import path\npath.join('a', 'b')",
        "import os\nos.sep",
        "import sys\nsys.argv[1:]",
        "import sqlite3",
        "import signal\nimport threading\nimport io\nio.StringIO()",
        "from pathlib import Path as P\nP('x') / 'y'",
        "from datetime import datetime as D, UTC\nD(2026, 1, 1, tzinfo=UTC)",
        "def f(args):\n    return getattr(args, 'json'), vars(args)",
        "import argparse\nargparse.ArgumentParser()",
        # ambiguous method names on non-path receivers
        "'a'.replace('a', 'b')",
        "def f(p):\n    return p.name.replace('a', 'b')",
        "def f(node):\n    return node.resolve()",
        "def f(x):\n    return x.stat(), x.exists(), x.open(), x.rename('a')",
        "import re\nre.compile('a').match('b')",
        "from pathlib import Path\nPath('a').name.replace('a', 'b')",
    ],
)
def test_scanner_accepts_gateway_routed_and_harmless_code(source: str) -> None:
    assert violations(source) == []
