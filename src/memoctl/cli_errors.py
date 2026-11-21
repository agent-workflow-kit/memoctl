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

from memoctl.config import ConfigError
from memoctl.grammar import GrammarError

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

INTERNAL_MESSAGE = "internal error"


class UsageError(Exception):
    """A usage error with no value in it: ``field`` is a ``[a-z_]{1,32}`` usage field."""

    def __init__(self, field: str = "argv") -> None:
        super().__init__(f"invalid {field}")
        self.field = field


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
        if self.details is not None and not set(self.details) <= DETAIL_KEYS:
            raise ValueError("unknown details key")

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


def usage_failure(field: str) -> Failure:
    if not _USAGE_FIELD.fullmatch(field):
        # A field that is not an identifier would put arbitrary text in the envelope.
        return internal_failure()
    return Failure(USAGE, "usage", f"invalid {field}", details={"field": field})


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


def _register_defaults() -> None:
    register_error(UsageError, lambda e: usage_failure(e.field))
    register_error(GrammarError, lambda e: usage_failure(e.field))
    register_error(
        ConfigError,
        lambda e: Failure(
            USAGE, "config_invalid", "invalid configuration", details={"field": e.field}
        ),
    )
    # Not on this base; each owner adds one register_error line here:
    # todo(#11): bodyread.BodyError -> usage, details {field: "body"}, plus the fixed reason
    #   (never the path or content).
    # todo(#12): body.BodyTextError -> usage, details from .details (class, line, limit).
    # todo(#13): secretscan.SecretDetected -> exit 20 refused, reason secret_detected,
    #   details {class, field}. Never render a traceback: frames hold the caller's text.
    # todo(#8): statedir.UnsafeStateError -> exit 20 refused, reason unsafe_state; plain
    #   OSError from state creation (EROFS, EACCES, EIO) needs its own mapping there.
    # todo(#14): author/lifetime GrammarError is covered above; lifetime.ClockError ->
    #   internal (exit 1): interface.md has no clock code, so a bad clock is an environment
    #   fault. Register it with ``internal_failure`` (decision recorded on issue #6).
    # todo(#33): a crontab GatewayError -> exit 12 failed, reason crontab_failed. A
    #   GatewayError is internal elsewhere (a bug or an environment fault).
    # todo(#9): sqlite failures -> exit 12 db_unreadable, db_locked, db_corrupt, schema_newer.
    # todo(#10): the deadline writes its own pre-built envelope and exits 12 timeout.


_register_defaults()
