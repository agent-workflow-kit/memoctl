"""Refuse secrets (interface.md, Secret check; Body rules item 7).

Named ``secretscan`` so it never shadows the standard library ``secrets`` module.

:func:`check_secrets` tests the repo id, topic, every label, the resolved kind and the body,
in that order. Each is tested as written and after NFKC (raw first); within one form the
patterns run in table order, then the ``mask_env`` values. The first match raises
:class:`SecretDetected`, which names only the class and the field: never the match, never the
value of a variable, never the text.

Linear time: every pattern starts with a literal prefix, has no nested quantifier and no
alternation of overlapping branches. Each repeated class run is bounded by the first
non-class character in the text, and every match start begins with ``-----BEGIN ``, so no
two starts share a run and a search never backtracks over a position twice. The JWT
pattern only starts after a non-token character, so one run of token characters is scanned
once. ``mask_env`` values are compared with ``in`` (a linear substring search). There is no
``re.IGNORECASE`` and no logging, printing or warning in this module.

Precondition for EVERY field (repo, topic, each label, kind, body): the text has already
passed its grammar and length bounds and the combining-mark run bound of body validation
(#12), because NFKC is unbounded in the input size (a topic of millions of U+FDFA takes
seconds). This module does not bound any input: ``add`` (``memoctl.cmd_add``, step 6) runs
those checks on every field before calling :func:`check_secrets`.

A ``mask_env`` value is compared as written (a code-point substring) against both forms of
the text. A variable that is unset, empty or shorter than :data:`MIN_VALUE_LENGTH` code
points is skipped; values are read through the gateway only.

The frame that raises holds only the class and the field; env values live in a frame that
has returned. Traceback frames of the callers hold the text, so callers must never render
tracebacks with locals. ``add`` calls this after body validation (a secret split by a
zero-width or control character is refused there first); ``cli_errors`` maps
:class:`SecretDetected` to exit 20 ``refused``, reason ``secret_detected``, ``details``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memoctl.gateway import Gateway

__all__ = [
    "CLASSES",
    "FIELDS",
    "MIN_VALUE_LENGTH",
    "PATTERNS",
    "PATTERN_SOURCES",
    "SecretDetected",
    "check_secrets",
]

MIN_VALUE_LENGTH = 8
FIELDS = ("repo", "topic", "label", "kind", "body")

# (class, source) in the order of the interface table; mask_env is last and has no pattern.
_TABLE: tuple[tuple[str, str], ...] = (
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("github_token", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("gitlab_token", r"glpat-[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    ("aws_key_id", r"A[KS]IA[0-9A-Z]{16}"),
    ("npm_token", r"npm_[A-Za-z0-9]{36}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("google_api_key", r"AIza[0-9A-Za-z_-]{35}"),
    ("stripe_key", r"sk_live_[0-9A-Za-z]{24,}"),
    ("openai_key", r"sk-proj-[A-Za-z0-9_-]{20,}"),
    ("jwt", r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
    ("slack_webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"),
)

PATTERN_SOURCES = tuple(source for _, source in _TABLE)
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (cls, re.compile(source)) for cls, source in _TABLE
)
CLASSES: tuple[str, ...] = (*dict.fromkeys(cls for cls, _ in _TABLE), "mask_env")


class SecretDetected(Exception):
    """A secret pattern or a ``mask_env`` value was found. Carries the class and the field
    only; the message is fixed text, and nothing about the match is kept."""

    def __init__(self, cls: str, field: str) -> None:
        if cls not in CLASSES:
            raise ValueError(f"unknown secret class {cls!r}")
        if field not in FIELDS:
            raise ValueError(f"unknown secret field {field!r}")
        message = f"the {field} contains a secret ({cls})"
        super().__init__(message)
        self.reason = "secret_detected"
        self.message = message
        self.cls = cls
        self.field = field

    def __reduce__(self) -> tuple[object, ...]:
        return (SecretDetected, (self.cls, self.field))

    @property
    def details(self) -> dict[str, object]:
        return {"class": self.cls, "field": self.field}


def _masked_values(gateway: Gateway, names: Sequence[str]) -> list[str]:
    values: list[str] = []
    for name in names:
        value = gateway.env(name)
        if value is not None and len(value) >= MIN_VALUE_LENGTH:
            values.append(value)
    return values


def _scan(text: str, values: Sequence[str]) -> str | None:
    """The class of the first match in one form of one text, or None."""
    for cls, pattern in PATTERNS:
        if pattern.search(text) is not None:
            return cls
    for value in values:
        if value in text:
            return "mask_env"
    return None


def _find(text: str, values: Sequence[str]) -> str | None:
    found = _scan(text, values)
    if found is not None or text.isascii():
        return found
    normalized = unicodedata.normalize("NFKC", text)
    return None if normalized == text else _scan(normalized, values)


def _locate(
    gateway: Gateway,
    mask_env: Sequence[str],
    inputs: Sequence[tuple[str, str]],
) -> tuple[str, str] | None:
    """The (class, field) of the first match, or None. Env values stay in this frame."""
    values = _masked_values(gateway, mask_env)
    for field, text in inputs:
        cls = _find(text, values)
        if cls is not None:
            return cls, field
    return None


def _refuse(cls: str, field: str) -> None:
    raise SecretDetected(cls, field)


def check_secrets(
    gateway: Gateway,
    mask_env: Sequence[str],
    *,
    repo: str,
    topic: str,
    labels: Sequence[str],
    kind: str,
    body: str,
) -> None:
    """Raise :class:`SecretDetected` when any input holds a secret; return None otherwise.

    Every input must already be bounded (see the module docstring); ``mask_env`` holds the
    variable names of the effective config.
    """
    inputs = [("repo", repo), ("topic", topic), *(("label", s) for s in labels)]
    inputs += [("kind", kind), ("body", body)]
    found = _locate(gateway, mask_env, inputs)
    if found is not None:
        _refuse(*found)
