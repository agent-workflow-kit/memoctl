"""Loader for the pinned copy of claude-session's sanitizer (tests/pinned/).

memoctl never imports claude-session; the pinned file is plain test data that is loaded from its
path under a private module name and never put on ``sys.path`` or in ``sys.modules``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

PINNED_FILE = Path(__file__).parent / "pinned" / "claude_session_sanitizer.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pinned_claude_session_sanitizer", PINNED_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claude_session_sanitize() -> Callable[[str], str]:
    fn = load().sanitize
    assert callable(fn)
    return fn  # type: ignore[no-any-return]
