"""The pinned copy of claude-session's sanitizer is exactly what its provenance says.

``tests/pinned/claude_session_sanitizer.py`` is claude-session's ``notes.sanitize`` with the
module constants it uses, copied from the source at a version and a commit. When claude-session
changes its sanitizer, add a new pinned file (new name), change the constants below and review
the new differences in ``tests/test_sanitizer_differential.py``: never edit the old file.
"""

from __future__ import annotations

import ast
import hashlib
import sys

import pytest

from tests import sanitizer_pin

PINNED_VERSION = "0.38.4"
PINNED_COMMIT = "50b4b252c7ebf36b0f82d1827cd7bf8b78b85449"
# SHA-256 of claude-session's whole src/claude_session/notes.py at that commit.
SOURCE_SHA256 = "135ca651b653255a420d5ce2f281886b384f4725be1f3fde07a56acd8eca3f2d"
# SHA-256 of the pinned file's bytes: an edit to the copy fails here and has to be a new pinned
# file (and these constants) instead.
PINNED_SHA256 = "3a878afbd85b83ebb19b9a2cfd935692b2e2f3567b19ba9f2ea6c6eee143e4be"
COPIED_ON = "2026-11-24"
TOP_LEVEL_CALLS = {("re", "compile"), ("str", "maketrans")}


def _source() -> str:
    return sanitizer_pin.PINNED_FILE.read_text(encoding="utf-8")


def test_the_pinned_file_is_unchanged() -> None:
    digest = hashlib.sha256(sanitizer_pin.PINNED_FILE.read_bytes()).hexdigest()
    assert digest == PINNED_SHA256


def test_the_provenance_header_names_the_pin() -> None:
    header = "\n".join(line for line in _source().splitlines() if line.startswith("#"))
    assert "claude-session" in header
    assert f"v{PINNED_VERSION}" in header
    assert PINNED_COMMIT in header
    assert SOURCE_SHA256 in header
    assert f"copied:    {COPIED_ON}" in header
    assert "src/claude_session/notes.py" in header
    assert "MIT" in header


def test_the_pinned_module_imports_nothing_outside_the_standard_library() -> None:
    tree = ast.parse(_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import reaches the sibling's package"
            assert node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported, "the sanitizer needs unicodedata"
    assert imported <= set(sys.stdlib_module_names)
    assert "claude_session" not in imported


def test_loading_the_pinned_module_imports_nothing_new_outside_the_standard_library() -> None:
    before = set(sys.modules)
    sanitizer_pin.load()
    new = {name.split(".")[0] for name in set(sys.modules) - before}
    # The loader never registers the pinned module in sys.modules, so nothing is allowed beyond
    # the standard library.
    assert new <= set(sys.stdlib_module_names)


def _call_name(node: ast.expr) -> tuple[str, str] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return (node.func.value.id, node.func.attr)
    return None


def _is_literal(node: ast.expr | None) -> bool:
    """A constant, a module-level name or ``re.FLAG``, or a container or ``|`` of those."""
    if node is None or isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name)
    if isinstance(node, ast.Dict):
        return all(
            _is_literal(k) and _is_literal(v) for k, v in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.BinOp):
        return _is_literal(node.left) and _is_literal(node.right)
    return False


def _import_time_expressions(node: ast.stmt) -> list[ast.expr]:
    """The expressions a top-level statement evaluates when the module is imported."""
    if isinstance(node, ast.Assign):
        return [node.value]
    if isinstance(node, ast.FunctionDef):
        args = node.args
        every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        every += [a for a in (args.vararg, args.kwarg) if a is not None]
        if node.decorator_list:  # a decorator is called at import, a bare name included
            return [ast.Call(func=ast.Name(id="<decorator>"), args=[], keywords=[])]
        found = [*args.defaults, *(d for d in args.kw_defaults if d)]
        found += [a.annotation for a in every if a.annotation is not None]
        if node.returns is not None:
            found.append(node.returns)
        return found
    return []


def import_time_violations(source: str) -> list[str]:
    """What module-level code would run beyond pure constructors (empty: nothing)."""
    bad: list[str] = []
    allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef)
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a docstring
        if not isinstance(node, allowed):
            bad.append(f"statement: {ast.dump(node)[:60]}")
            continue
        for expr in _import_time_expressions(node):
            for sub in ast.walk(expr):
                if isinstance(sub, ast.Call):
                    if _call_name(sub) not in TOP_LEVEL_CALLS:
                        bad.append(f"call: {ast.dump(sub)[:60]}")
                    elif not all(_is_literal(a) for a in sub.args) or not all(
                        _is_literal(k.value) for k in sub.keywords
                    ):
                        bad.append(f"non-literal argument: {ast.dump(sub)[:60]}")
    return bad


def test_the_pinned_module_runs_nothing_at_import_but_pure_constructors() -> None:
    assert import_time_violations(_source()) == []


def test_the_import_time_check_accepts_the_constructors_with_literal_arguments() -> None:
    source = (
        "import re\nA = re.compile('x', re.I | re.M)\nB = str.maketrans({'a': 'b'})\n"
        "def f(a: int = 1) -> str:\n    return open('x').read()\n"
    )
    assert import_time_violations(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.system('x')\n",
        "@print\ndef f(): pass\n",
        "@open('x').read\ndef f(): pass\n",
        "def f(a=open('x')): pass\n",
        "def f(*, a=open('x')): pass\n",
        "def f() -> open('x'): pass\n",
        "def f(a: open('x')): pass\n",
        "import re\nA = re.compile(open('x').read())\n",
        "import re\nA = re.compile('x', flags=print('y'))\n",
        "import re\nA = re.compile({1: print(2)})\n",
        "A = [print(1)]\n",
        "class C:\n    x = print(1)\n",
    ],
    ids=[
        "call",
        "decorator-name",
        "decorator-call",
        "default",
        "kw-default",
        "return-annotation",
        "arg-annotation",
        "compile-arg",
        "compile-keyword",
        "compile-nested",
        "list-call",
        "class",
    ],
)
def test_the_import_time_check_rejects_each_way_to_run_code_at_import(source: str) -> None:
    assert import_time_violations(source) != []


def test_loading_the_pinned_module_has_no_side_effects() -> None:
    # A module-level statement that wrote, spawned or read the environment would have failed the
    # AST check above; this pins the observable result: the same public names every load.
    first = sanitizer_pin.load()
    second = sanitizer_pin.load()
    assert sorted(n for n in vars(first) if not n.startswith("__")) == sorted(
        n for n in vars(second) if not n.startswith("__")
    )
    assert first.sanitize("a") == "a"
