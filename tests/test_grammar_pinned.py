"""memoctl's grammars against claude-session's, pinned as test data.

claude-session's documented grammars (kind name, label, repo id) are copied into
``tests/pinned/`` with the version they came from. memoctl never imports the sibling. When
claude-session changes one of these contracts, add a new pinned file, change PINNED_FILE and
PINNED_VERSION here, and review the difference: do not edit the old file.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from memoctl import grammar
from tests.grammar_corpus import pinned_corpus

PINNED_VERSION = "0.38.4"
PINNED_FILE = Path(__file__).parent / "pinned" / f"claude_session_grammars_{PINNED_VERSION}.json"
PINNED_COMMIT = "50b4b25"
# SHA-256 of the canonical JSON (sorted keys, no spaces) of the ``patterns`` object: an edit to
# the copied data fails here and has to be a new pinned file instead.
PINNED_PATTERNS_SHA256 = "de6ab9c3c21ab39bfab04ce4ab8b0a2c56b506a9b78f8cff91425f1683df5d43"
CORPUS = pinned_corpus()
# The field names memoctl uses for each pinned pattern.
PAIRS = ["kind", "label_key", "label_value", "repo"]


@pytest.fixture(scope="module")
def pinned() -> dict[str, object]:
    return json.loads(PINNED_FILE.read_text(encoding="utf-8"))


def test_pinned_copy_names_its_source_version(pinned: dict[str, object]) -> None:
    assert pinned["source"] == "claude-session"
    assert pinned["version"] == PINNED_VERSION
    assert pinned["tag"] == f"v{PINNED_VERSION}"
    assert pinned["commit"] == PINNED_COMMIT
    assert pinned["sources"]


def test_pinned_copy_has_exactly_the_expected_patterns(pinned: dict[str, object]) -> None:
    patterns = pinned["patterns"]
    assert isinstance(patterns, dict)
    assert sorted(patterns) == sorted(PAIRS)


def test_pinned_patterns_are_unchanged(pinned: dict[str, object]) -> None:
    canonical = json.dumps(pinned["patterns"], sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == PINNED_PATTERNS_SHA256


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
    assert not any(grammar.matches(field, s + "\n") for s in accepted)


def _differs(field: str, pinned: dict[str, object], old: str, new: str) -> bool:
    patterns = pinned["patterns"]
    assert isinstance(patterns, dict)
    source = patterns[field]
    assert old in source, (field, old)
    mutated = re.compile(source.replace(old, new))
    original = re.compile(source)
    return any(bool(original.fullmatch(s)) != bool(mutated.fullmatch(s)) for s in CORPUS)


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [
        ("repo", "{1,5}", "{1,6}"),
        ("repo", "{1,5}", "{1,9}"),
        ("repo", "{1,5}", "{1,4}"),
        ("label_value", "A-Za-z", "A-Za-z~"),
        ("label_value", "{0,256}", "{0,257}"),
        ("label_value", "{0,256}", "{0,255}"),
        ("label_key", "{0,63}", "{0,64}"),
        ("kind", "{0,63}", "{0,62}"),
    ],
)
def test_the_corpus_catches_a_mutated_pattern(
    field: str, old: str, new: str, pinned: dict[str, object]
) -> None:
    assert _differs(field, pinned, old, new)
