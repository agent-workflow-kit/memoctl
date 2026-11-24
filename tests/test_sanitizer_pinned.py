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
    assert new <= set(sys.stdlib_module_names) | {"pinned_claude_session_sanitizer"}


def _call_name(node: ast.expr) -> tuple[str, str] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return (node.func.value.id, node.func.attr)
    return None


def test_the_pinned_module_runs_nothing_at_import_but_pure_constructors() -> None:
    allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef)
    for node in ast.parse(_source()).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a docstring
        assert isinstance(node, allowed), ast.dump(node)[:80]
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call):
                    assert _call_name(sub) in TOP_LEVEL_CALLS, ast.dump(sub)[:80]


def test_loading_the_pinned_module_has_no_side_effects() -> None:
    # A module-level statement that wrote, spawned or read the environment would have failed the
    # AST check above; this pins the observable result: the same public names every load.
    first = sanitizer_pin.load()
    second = sanitizer_pin.load()
    assert sorted(n for n in vars(first) if not n.startswith("__")) == sorted(
        n for n in vars(second) if not n.startswith("__")
    )
    assert first.sanitize("a") == "a"
