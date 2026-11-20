"""memoctl's grammars against claude-session's, pinned as test data.

claude-session's documented grammars (kind name, label, repo id) are copied into
``tests/pinned/`` with the version they came from. memoctl never imports the sibling. When
claude-session changes one of these contracts, add a new pinned file, change PINNED_FILE and
PINNED_VERSION here, and review the difference: do not edit the old file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memoctl import grammar
from tests.test_grammar_fuzz import CORPUS

PINNED_FILE = Path(__file__).parent / "pinned" / "claude_session_grammars.json"
PINNED_VERSION = "0.38.4"
# The field names memoctl uses for each pinned pattern.
PAIRS = ["kind", "label_key", "label_value", "repo"]


@pytest.fixture(scope="module")
def pinned() -> dict[str, object]:
    return json.loads(PINNED_FILE.read_text(encoding="utf-8"))


def test_pinned_copy_names_its_source_version(pinned: dict[str, object]) -> None:
    assert pinned["source"] == "claude-session"
    assert pinned["version"] == PINNED_VERSION
    assert pinned["tag"] == f"v{PINNED_VERSION}"
    assert re.fullmatch(r"[0-9a-f]{7,40}", str(pinned["commit"]))
    assert pinned["sources"]


def test_pinned_copy_has_exactly_the_expected_patterns(pinned: dict[str, object]) -> None:
    patterns = pinned["patterns"]
    assert isinstance(patterns, dict)
    assert sorted(patterns) == sorted(PAIRS)


@pytest.mark.parametrize("field", PAIRS)
def test_memoctl_accepts_exactly_what_claude_session_accepts(
    field: str, pinned: dict[str, object]
) -> None:
    patterns = pinned["patterns"]
    assert isinstance(patterns, dict)
    theirs = re.compile(patterns[field])
    mismatches = [s for s in CORPUS if grammar.matches(field, s) != bool(theirs.fullmatch(s))]
    assert mismatches[:5] == []


@pytest.mark.parametrize("field", PAIRS)
def test_pinned_patterns_reject_a_trailing_newline_under_fullmatch(
    field: str, pinned: dict[str, object]
) -> None:
    patterns = pinned["patterns"]
    assert isinstance(patterns, dict)
    theirs = re.compile(patterns[field])
    accepted = [s for s in CORPUS if theirs.fullmatch(s)]
    assert accepted
    assert not any(theirs.fullmatch(s + "\n") for s in accepted)
