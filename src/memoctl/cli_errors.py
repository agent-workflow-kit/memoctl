"""Exit codes, the error envelope and the table that maps exceptions to them.

Every failure a command can raise reaches the user as one :class:`Failure`: an exit code, an
envelope (``code``, ``message``, ``reason``, ``details``, ``retryable``) and one stderr status
line (interface.md, Exit codes and Output on failure). The message is always a fixed string
of the tool: no exception text, input value or note text is ever placed in a failure, so
nothing hostile reaches a terminal and nothing secret reaches a log.

Mappings are table-driven: :data:`MAPPERS` holds one line per exception type. An issue that
adds an exception adds one ``register_error`` line to :func:`_register_defaults` and nothing
else. A type that is not registered is an internal error (exit 1), never rendered.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memoctl.body import BodyTextError
from memoctl.bodyread import BodyError
from memoctl.config import ConfigError
from memoctl.grammar import GrammarError
from memoctl.lifetime import ClockError
from memoctl.secretscan import SecretDetected
from memoctl.statedir import StateAccessError, UnsafeStateError
from memoctl.store import SchemaNewerError, StoreError

OK = 0
INTERNAL = 1
USAGE = 2
NOT_FOUND = 10
FAILED = 12
NEEDS_HUMAN = 13
REFUSED = 20
SIGINT_EXIT = 130  # 128 + SIGINT: no output

# Exit code -> envelope code; exit 20 is code "refused" with a required reason.
_CODES: dict[int, tuple[str, ...]] = {
    INTERNAL: ("internal",),
    USAGE: ("usage", "config_invalid"),
    NOT_FOUND: ("not_found",),
    FAILED: ("failed",),
    NEEDS_HUMAN: ("needs_human",),
    REFUSED: ("refused",),
}
FAILURE_REASONS = frozenset(
    {"db_unreadable", "db_locked", "db_corrupt", "schema_newer", "timeout", "crontab_failed"}
)
REFUSAL_REASONS = frozenset(
    {
        "repo_full",
        "store_full",
        "rate_limited",
        "supersede_mismatch",
        "already_superseded",
        "secret_detected",
        "unsafe_state",
        "cron_block_malformed",
    }
)
RETRYABLE_REASONS = frozenset({"db_locked", "timeout", "rate_limited"})
# The only keys a ``details`` object may carry (schemas/defs.json error_details).
DETAIL_KEYS = frozenset(
    {"field", "id", "line", "class", "limit", "retry_after_s", "scope", "found", "supported"}
)
_USAGE_FIELD = re.compile(r"[a-z_]{1,32}", re.ASCII)
MAX_MESSAGE = 2048
MAX_FIELD = 4096
MAX_NOTE_ID = 999999999999999999
TEXT_CLASSES = frozenset(
    {"encoding", "control", "invisible", "private_use", "unassigned", "combining"}
)
SECRET_CLASSES = frozenset(
    {
        "private_key", "github_token", "gitlab_token", "slack_token", "aws_key_id", "npm_token",
        "anthropic_key", "google_api_key", "stripe_key", "openai_key", "jwt", "slack_webhook",
        "mask_env",
    }
)  # fmt: skip
SECRET_FIELDS = frozenset({"repo", "topic", "label", "kind", "body"})
# Per code or reason (the reason wins when there is one): details must be null, or must carry
# these keys (schemas/defs.json, ``error`` allOf).
NULL_DETAILS = frozenset(
    {
        "internal", "needs_human", "db_unreadable", "db_locked", "db_corrupt", "timeout",
        "crontab_failed", "supersede_mismatch", "already_superseded", "unsafe_state",
        "cron_block_malformed",
    }
)  # fmt: skip
REQUIRED_DETAILS: dict[str, frozenset[str]] = {
    "usage": frozenset({"field"}),
    "config_invalid": frozenset({"field"}),
    "not_found": frozenset({"id"}),
    "rate_limited": frozenset({"retry_after_s", "limit", "scope"}),
    "secret_detected": frozenset({"class", "field"}),
    "repo_full": frozenset({"limit"}),
    "store_full": frozenset({"limit"}),
    "schema_newer": frozenset({"found", "supported"}),
}


def _is_int(value: object, low: int, high: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= low
        and (high is None or value <= high)
    )


def _check_detail_values(details: object) -> None:
    """Keys and value types of one ``error_details`` object."""
    if not isinstance(details, dict):
        raise ValueError("details must be an object")
    if not set(details) <= DETAIL_KEYS:
        raise ValueError("unknown details key")
    checks: dict[str, Callable[[Any], bool]] = {
        "field": lambda v: isinstance(v, str) and len(v) <= MAX_FIELD,
        "id": lambda v: _is_int(v, 1, MAX_NOTE_ID),
        "line": lambda v: _is_int(v, 1),
        "class": lambda v: v in TEXT_CLASSES | SECRET_CLASSES,
        "limit": lambda v: _is_int(v, 0),
        "retry_after_s": lambda v: _is_int(v, 1),
        "scope": lambda v: v in ("repo", "store"),
        "found": lambda v: _is_int(v, 0),
        "supported": lambda v: _is_int(v, 1),
    }
    for key, value in details.items():
        if not checks[key](value):
            raise ValueError("invalid details value")


INTERNAL_MESSAGE = "internal error"


class UsageError(Exception):
    """A usage error with no value in it: ``field`` is a ``[a-z_]{1,32}`` usage field."""

    def __init__(self, field: str = "argv", limit: int | None = None) -> None:
        super().__init__(f"invalid {field}")
        self.field = field
        self.limit = limit  # details.limit where one applies (the label count)


class NoteNotFoundError(Exception):
    """An id that names no note (exit 10 ``not_found``); carries the id only."""

    def __init__(self, note_id: int) -> None:
        super().__init__("note not found")
        self.note_id = note_id


class SupersedeMismatchError(Exception):
    """``--supersedes`` names a note of another repo (exit 20 ``supersede_mismatch``)."""

    def __init__(self) -> None:
        super().__init__("supersede target is in another repo")


class AlreadySupersededError(Exception):
    """``--supersedes`` names a note that is already superseded (exit 20 ``already_superseded``)."""

    def __init__(self) -> None:
        super().__init__("supersede target is already superseded")


@dataclass(frozen=True)
class Failure:
    """One failed command, ready to render. Built only from fixed strings and identifiers."""

    exit_code: int
    code: str
    message: str
    reason: str | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # A mapper bug must fail loudly (and become an internal error), not print a bad envelope.
        if self.code not in _CODES.get(self.exit_code, ()):
            raise ValueError("code does not belong to the exit code")
        if (self.exit_code in (FAILED, REFUSED)) != (self.reason is not None):
            raise ValueError("reason is set exactly on exits 12 and 20")
        allowed = FAILURE_REASONS if self.exit_code == FAILED else REFUSAL_REASONS
        if self.reason is not None and self.reason not in allowed:
            raise ValueError("unknown reason")
        if not isinstance(self.message, str) or len(self.message) > MAX_MESSAGE:
            raise ValueError("message must be a string of at most 2048 characters")
        self._check_details()

    def _check_details(self) -> None:
        """schemas/defs.json: ``error_details`` and the per-code and per-reason rules."""
        details = self.details
        key = self.reason if self.reason is not None else self.code
        if key in NULL_DETAILS:
            if details is not None:
                raise ValueError("details must be null here")
            return
        required = REQUIRED_DETAILS.get(key)
        if required is None:
            if details is not None:
                _check_detail_values(details)
            return
        if details is None:
            raise ValueError("details are required here")
        _check_detail_values(details)
        if not required <= set(details):
            raise ValueError("a required details key is missing")
        if key == "usage" and not _USAGE_FIELD.fullmatch(details["field"]):
            raise ValueError("a usage field is a [a-z_]{1,32} identifier")
        if key == "secret_detected" and (
            details["class"] not in SECRET_CLASSES or details["field"] not in SECRET_FIELDS
        ):
            raise ValueError("secret_detected needs a secret class and a note field")

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE_REASONS

    def envelope(self) -> str:
        """The stdout document: one JSON object and LF, ASCII only (``ensure_ascii``)."""
        body = {
            "error": {
                "code": self.code,
                "message": self.message,
                "reason": self.reason,
                "details": self.details,
                "retryable": self.retryable,
            }
        }
        return json.dumps(body, ensure_ascii=True, separators=(", ", ": ")) + "\n"

    def status_line(self) -> str:
        """The stderr line: ``<code>: <message>``, ``failed (<reason>): ...``, ``refused (...)``."""
        if self.exit_code == FAILED:
            return f"failed ({self.reason}): {self.message}\n"
        if self.exit_code == REFUSED:
            return f"refused ({self.reason}): {self.message}\n"
        return f"{self.code}: {self.message}\n"


def internal_failure() -> Failure:
    return Failure(INTERNAL, "internal", INTERNAL_MESSAGE)


Mapper = Callable[[Any], Failure]
MAPPERS: dict[type[BaseException], Mapper] = {}


def register_error(exc_type: type[BaseException], mapper: Mapper) -> None:
    """Map ``exc_type`` (and its subclasses without an own entry) with ``mapper``."""
    MAPPERS[exc_type] = mapper


def usage_failure(field: str, limit: int | None = None) -> Failure:
    if not _USAGE_FIELD.fullmatch(field):
        # A field that is not an identifier would put arbitrary text in the envelope.
        return internal_failure()
    details: dict[str, Any] = {"field": field}
    if limit is not None:
        details["limit"] = limit
    return Failure(USAGE, "usage", f"invalid {field}", details=details)


def failure_for(exc: BaseException) -> Failure:
    """The failure for ``exc``: the nearest registered type in its MRO, else internal.

    A mapper that itself raises, or returns something else, is an internal error too.
    """
    for klass in type(exc).__mro__:
        mapper = MAPPERS.get(klass)
        if mapper is None:
            continue
        try:
            failure = mapper(exc)
        except Exception:
            return internal_failure()
        return failure if isinstance(failure, Failure) else internal_failure()
    return internal_failure()


def _store_failure(exc: StoreError) -> Failure:
    if exc.reason == "timeout":
        return Failure(FAILED, "failed", "deadline passed", "timeout")
    return Failure(FAILED, "failed", _STORE_MESSAGES[exc.reason], exc.reason)


_STORE_MESSAGES = {
    "db_unreadable": "database cannot be read",
    "db_locked": "database is locked",
    "db_corrupt": "database is corrupt",
}


def _register_defaults() -> None:
    register_error(UsageError, lambda e: usage_failure(e.field, e.limit))
    register_error(GrammarError, lambda e: usage_failure(e.field))
    register_error(
        ConfigError,
        lambda e: Failure(
            USAGE,
            "config_invalid",
            "invalid configuration",
            details={"field": e.field[:MAX_FIELD]},  # the schema caps a field at 4096
        ),
    )
    register_error(BodyError, lambda e: Failure(USAGE, "usage", e.message, details=e.details))
    register_error(BodyTextError, lambda e: Failure(USAGE, "usage", e.message, details=e.details))
    register_error(
        SecretDetected,
        lambda e: Failure(REFUSED, "refused", e.message, "secret_detected", details=e.details),
    )
    register_error(
        UnsafeStateError,
        lambda e: Failure(REFUSED, "refused", "state directory is not safe to use", "unsafe_state"),
    )
    # A failure to create or reach the state directory (EROFS, EACCES, EIO, ...) is not an
    # unsafe_state refusal (interface.md, State): the state cannot be used, so exit 12
    # db_unreadable with no errno or path text.
    register_error(
        StateAccessError,
        lambda e: Failure(FAILED, "failed", "state cannot be accessed", "db_unreadable"),
    )
    register_error(
        NoteNotFoundError,
        lambda e: Failure(NOT_FOUND, "not_found", "note not found", details={"id": e.note_id}),
    )
    register_error(
        SupersedeMismatchError,
        lambda e: Failure(
            REFUSED, "refused", "supersede target is in another repo", "supersede_mismatch"
        ),
    )
    register_error(
        AlreadySupersededError,
        lambda e: Failure(
            REFUSED, "refused", "supersede target is already superseded", "already_superseded"
        ),
    )
    # A bad clock is an environment fault with no code of its own (interface.md, Exit codes).
    register_error(ClockError, lambda e: internal_failure())
    # Tracebacks must never render locals: the frames of BodyTextError and SecretDetected hold
    # the caller's text. Nothing here renders a traceback at all.
    # todo(#33): a crontab GatewayError -> exit 12 failed, reason crontab_failed. A
    #   GatewayError is internal elsewhere (a bug or an environment fault).
    # The deadline's own timer writes a pre-built envelope and exits 12 (memoctl.deadline); a
    # progress-handler interrupt arrives as StoreError("timeout") and maps to the same failure.
    register_error(StoreError, _store_failure)
    register_error(
        SchemaNewerError,
        lambda e: Failure(
            FAILED,
            "failed",
            "database schema is newer",
            "schema_newer",
            details={"found": e.found, "supported": e.supported},
        ),
    )


_register_defaults()
