"""Validate a note body or search text (interface.md, Body rules items 1 to 6).

Pure functions over bytes: no I/O, so nothing here goes through the gateway. The bounded
reader (:mod:`memoctl.bodyread`) returns the raw bytes; this module decides whether they are
acceptable text. memoctl never rewrites a body: :func:`validate_body` returns the text as
written or raises.

Checked in the interface's order, the first failure reported:

1. at most ``max_bytes`` UTF-8 bytes as written (:class:`memoctl.bodyread.BodyError`, the same
   error the reader raises, so the caller maps one shape);
2. strict UTF-8 (class ``encoding``);
3. no refused character (classes ``control``, ``invisible``, ``private_use``, ``unassigned``);
4. no run of more than 30 combining code points in the NFKD form (class ``combining``);
5. at least one non-space character (reason ``whitespace_only``);
6. at most ``max_bytes`` UTF-8 bytes after NFKC (reason ``nfkc_too_long``).

Bounded work: ``max_bytes`` is capped at :data:`MAX_LIMIT`, item 1 runs before any decoding,
every pass is linear, and NFKC (quadratic in a long run of combining marks) only runs on text
that item 4 has shown to hold no run over 30. An error carries a fixed reason, never any
input; the 1-based ``line`` counts LF in the text as written.

Search text takes items 2 to 4 only (:func:`check_text`).

The ``unassigned`` class is judged by the running interpreter's Unicode database: a body
accepted by a newer interpreter may be stripped by an older one at read time; accepted.

Traceback frames of a raised :class:`BodyTextError` hold the decoded text, so callers must
never render tracebacks with locals (see todo(#6) for the error mapping).

todo(#16): the refused-character classes are defined here from the spec, independently of
``memoctl.sanitize.refused_class`` (#24). The first issue that merges both branches (#16)
may deduplicate them; until then both must follow Body rules item 3.
todo(#13): item 7, the secret check, runs after this validation.
todo(#6): the CLI maps :class:`BodyTextError` to exit 2 ``usage`` with ``details``.
todo(#5): ``max_bytes`` comes from the effective config (256 to 65536).
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "CLASSES",
    "MAX_COMBINING_RUN",
    "MAX_LIMIT",
    "BodyTextError",
    "check_text",
    "classify",
    "validate_body",
]

MAX_LIMIT = 65536
MAX_COMBINING_RUN = 30

CLASSES = ("encoding", "control", "invisible", "private_use", "unassigned", "combining")

_MESSAGES = {
    "encoding": "the text is not valid UTF-8",
    "control": "the text contains a control character",
    "invisible": "the text contains an invisible character",
    "private_use": "the text contains a private-use character",
    "unassigned": "the text contains an unassigned character",
    "combining": "the text contains too long a run of combining marks",
    "whitespace_only": "the text has no visible character",
    "nfkc_too_long": "the text is over the size limit after normalization",
}


class BodyTextError(Exception):
    """The text broke a body rule. ``reason`` is a key of the fixed messages.

    ``cls`` is the refused-text class (one of :data:`CLASSES`) or None; ``line`` is set for a
    class; ``limit`` for ``nfkc_too_long``. The message never contains the input.
    """

    REASONS = tuple(_MESSAGES)

    def __init__(
        self,
        reason: str,
        *,
        field: str = "body",
        line: int | None = None,
        limit: int | None = None,
    ) -> None:
        if reason not in _MESSAGES:
            raise ValueError(f"unknown body text error reason {reason!r}")
        super().__init__(_MESSAGES[reason])
        self.reason = reason
        self.message = _MESSAGES[reason]
        self.field = field
        self.line = line
        self.limit = limit
        self.cls: str | None = reason if reason in CLASSES else None

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild, (self.reason, self.field, self.line, self.limit))

    @property
    def details(self) -> dict[str, object]:
        out: dict[str, object] = {"field": self.field}
        if self.cls is not None:
            out["class"] = self.cls
            out["line"] = self.line
        elif self.reason == "nfkc_too_long":
            out["limit"] = self.limit
        return out


def _rebuild(reason: str, field: str, line: int | None, limit: int | None) -> BodyTextError:
    return BodyTextError(reason, field=field, line=line, limit=limit)


# Default-ignorable code points outside category Cf (Body rules item 3).
_IGNORABLE = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F, 0x2800, 0x3164}
    | set(range(0xFE00, 0xFE10))
    | {0xFFA0}
    | set(range(0xE0100, 0xE01F0))
)
_LINE_SEPARATORS = frozenset({0x2028, 0x2029})


def classify(ch: str) -> str | None:
    """The refused class of one code point, or None when it may appear in a body."""
    cp = ord(ch)
    if cp < 128:
        # Only LF and tab are allowed among the controls; DEL is a control too.
        return "control" if (cp < 32 and cp not in (9, 10)) or cp == 127 else None
    if cp in _LINE_SEPARATORS:
        return "control"
    if 0xE0000 <= cp <= 0xE007F or cp in _IGNORABLE:
        return "invisible"  # the whole tag block, assigned or not
    cat = unicodedata.category(ch)
    if cat == "Cc":
        return "control"
    if cat == "Cf":
        return "invisible"
    if cat == "Co":
        return "private_use"
    if cat == "Cn":
        return "unassigned"
    return None


def _decode(data: bytes, field: str) -> str:
    try:
        return data.decode("utf-8")  # strict: overlongs and surrogates are errors
    except UnicodeDecodeError as exc:
        start = exc.start
    # Raised outside the handler: a chained decoder error would carry the input bytes.
    raise BodyTextError("encoding", field=field, line=data.count(b"\n", 0, start) + 1)


def _check_characters(text: str, field: str) -> None:
    line = 1
    for ch in text:
        if ch == "\n":
            line += 1
            continue
        cls = classify(ch)
        if cls is not None:
            raise BodyTextError(cls, field=field, line=line)


def _check_combining(text: str, field: str) -> None:
    # Per-code-point NFKD concatenated equals the NFKD of the whole text, so this is linear
    # and never normalizes more than one code point at a time.
    line = 1
    run = 0
    for ch in text:
        if ch == "\n":
            line += 1
            run = 0
            continue
        if ch < "\x80":
            run = 0
            continue
        for part in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(part):
                run += 1
                if run > MAX_COMBINING_RUN:
                    raise BodyTextError("combining", field=field, line=line)
            else:
                run = 0


def check_text(data: bytes, *, field: str = "text") -> str:
    """Items 2 to 4 only (search text): strict UTF-8, refused characters, combining runs.

    The size cap is the reader's; emptiness and the NFKC bound are not checked here.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    text = _decode(data, field)
    _check_characters(text, field)
    _check_combining(text, field)
    return text


def validate_body(data: bytes, max_bytes: int, *, field: str = "body") -> str:
    """Items 1 to 6: return the body as text, unchanged, or raise.

    Raises :class:`memoctl.bodyread.BodyError` (``too_long``) for item 1 and
    :class:`BodyTextError` for the others. ``max_bytes`` is 1 to :data:`MAX_LIMIT`.
    """
    # Imported here so a caller of check_text alone does not need the reader.
    from memoctl.bodyread import BodyError

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        raise ValueError("max_bytes must be an integer")
    if not 1 <= max_bytes <= MAX_LIMIT:
        raise ValueError(f"max_bytes must be 1 to {MAX_LIMIT}")
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > max_bytes:
        raise BodyError("too_long", max_bytes, field)
    text = check_text(data, field=field)
    if not text or text.isspace():
        raise BodyTextError("whitespace_only", field=field)
    if len(unicodedata.normalize("NFKC", text).encode("utf-8")) > max_bytes:
        raise BodyTextError("nfkc_too_long", field=field, limit=max_bytes)
    return text
