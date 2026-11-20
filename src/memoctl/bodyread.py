"""Read a note body (or search text) from a file or standard input, bounded.

All file and descriptor access goes through the gateway. The reader returns raw bytes: the
text rules (strict UTF-8, refused characters, whitespace-only, NFKC length) belong to the
body validator, todo(#12). Refusals carry a fixed reason and never the path or the content.

Source: a ``Path`` is a file (a file literally named ``-`` is passed as ``Path("./-")``);
the string ``"-"`` is standard input.
"""

from __future__ import annotations

from pathlib import Path

from memoctl.gateway import BodySourceError, Gateway

STDIN = "-"

# Fixed messages, one per reason. todo(#6): the CLI maps BodyError to exit 2 ``usage`` with
# ``details`` (field, and limit for too_long).
_MESSAGES = {
    "too_long": "the body is over the size limit",
    "symlink": "the body file is a symbolic link",
    "not_regular": "the body source is not a regular file",
    "terminal": "standard input is a terminal",
    "unreadable": "the body source cannot be read",
}


class BodyError(Exception):
    """The body source was refused. ``reason`` is one of ``REASONS``."""

    REASONS = tuple(_MESSAGES)

    def __init__(self, reason: str, limit: int, field: str = "body") -> None:
        if reason not in _MESSAGES:
            raise ValueError(f"unknown body error reason {reason!r}")
        super().__init__(_MESSAGES[reason])
        self.reason = reason
        self.message = _MESSAGES[reason]
        self.limit = limit
        self.field = field

    @property
    def details(self) -> dict[str, object]:
        out: dict[str, object] = {"field": self.field}
        if self.reason == "too_long":
            out["limit"] = self.limit
        return out


def read_body(
    gateway: Gateway, source: Path | str, max_bytes: int, *, field: str = "body"
) -> bytes:
    """Return the bytes of ``source``, at most ``max_bytes`` of them.

    Reads ``max_bytes + 1`` so that exactly the limit is accepted and one more is refused;
    nothing is ever truncated. Raises ``BodyError``.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        if isinstance(source, Path):
            data = gateway.read_file_bounded(source, max_bytes + 1)
        elif source == STDIN:
            data = gateway.read_stdin_bounded(max_bytes + 1)
        else:
            raise TypeError("source must be a Path or '-'")
    except BodySourceError as exc:
        raise BodyError(exc.reason, max_bytes, field) from None
    if len(data) > max_bytes:
        raise BodyError("too_long", max_bytes, field)
    return data
