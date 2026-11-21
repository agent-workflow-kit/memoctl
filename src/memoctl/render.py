"""Terminal-safe human output ([Terminal rendering] and [Human output] of interface.md).

Everything that came from a note (repo, topic, kind, labels, body, and for safety every other
string too) is untrusted. Human output shows the stored text, not the ``context`` form (no NFKC,
no ``<`` rewrite), but no character that could drive a terminal or hide text reaches the
output: each one is shown as a visible ``\\u{XXXX}`` escape (uppercase hex, at least four
digits). The text that remains is printable ASCII, tab-as-spaces, LF, and non-ASCII characters
that are not control, format, private-use, unassigned, surrogate, line or paragraph separators
or default-ignorable (the set [Body rules] item 3 refuses, judged by ``unicodedata`` of the
running interpreter, plus surrogates, which a ``str`` can hold but UTF-8 cannot).

:func:`escape` works one code point at a time, so it is linear, it never splits an escape, and
``escape(a) + escape(b) == escape(a + b)``: text that is harmless in pieces cannot become
dangerous once joined or cut. :func:`escape_field` additionally escapes spaces that could pass
for the two-space column separator (see there), which depends on the neighbours, so only the
safety of its pieces carries over, not the equation. The output of both contains only safe
characters, so applying either again changes nothing. The output is lossy by design: a literal
``\\u{001B}`` in a note looks like an escaped ESC.

Public API (everything returns text; nothing here prints, :func:`emit` writes through the
gateway). Record arguments are the mappings of the ``--json`` schemas, so a command passes the
same dict to both forms:

- :func:`escape` and :func:`escape_field`: the escape for multi-line text and for one field.
- :func:`format_list` (``list`` and ``search``), :func:`format_show`, :func:`format_add`,
  :func:`format_delete`, :func:`format_prune`, :func:`format_validate`, :func:`format_cron`:
  each returns a :class:`Rendered` (stdout text, stderr text, warning text).
- :func:`emit`: writes a :class:`Rendered` through the gateway: stdout, then stderr, then the
  warnings.

Deliberately absent: width handling, truncation, color and tty detection. interface.md defines
none for human output (the text is the same whether or not stdout is a terminal, lines are
never cut and nothing is colored), and a cut or a color would be a rule the interface does not
have. Lines end in LF, never CRLF.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, NamedTuple

from memoctl.gateway import Gateway

# Default-ignorable code points outside category Cf that Body rules item 3 refuses.
_IGNORABLE = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x180E))
    | {0x180F}
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
# Categories that are never shown: control, format, surrogate, private use, unassigned, and the
# line and paragraph separators (U+2028, U+2029).
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Every character that is not printable ASCII; LF and tab are decided in the replacement.
_NOT_PLAIN = re.compile(r"[^\x20-\x7e]")


def is_unsafe(char: str) -> bool:
    """Whether ``char`` is shown as an escape (LF and tab are decided by the caller)."""
    code = ord(char)
    if code < 0x80:
        return code < 0x20 or code == 0x7F
    return code in _IGNORABLE or unicodedata.category(char) in _UNSAFE_CATEGORIES


@lru_cache(maxsize=4096)
def _escape_char(char: str) -> str:
    return f"\\u{{{ord(char):04X}}}" if is_unsafe(char) else char


def _body_repl(match: re.Match[str]) -> str:
    char = match.group()
    if char == "\n":
        return char
    if char == "\t":
        return "    "
    return _escape_char(char)


def _field_repl(match: re.Match[str]) -> str:
    return _escape_char(match.group())


def escape(text: str) -> str:
    """Escape multi-line text: LF is kept, tab becomes four spaces, every other unsafe code
    point becomes ``\\u{XXXX}``. A lone surrogate is escaped like any other code point."""
    return _NOT_PLAIN.sub(_body_repl, text)


_SPACE_RUN = re.compile(r" {2,}|^ | $")


def escape_field(text: str) -> str:
    """Escape text printed inside one line (a topic, a label, a column).

    As :func:`escape`, but LF and tab are escaped too (``\\u{000A}``, ``\\u{0009}``): a value
    that holds a line break must not start a fake line, and a tab must not pass for the
    two-space column separator. For the same reason every space that is part of a run of two or
    more, and every leading or trailing space, is shown as ``\\u{0020}``; single inner spaces
    stay. Only a database written by something else can hold any of these, because the grammars
    exclude them."""
    escaped = _NOT_PLAIN.sub(_field_repl, text)
    return _SPACE_RUN.sub(lambda m: "\\u{0020}" * len(m.group()), escaped)


class Rendered(NamedTuple):
    """The text a command prints, each part empty or ending in LF: ``out`` on stdout, ``err``
    on stderr (notices that ``--quiet`` suppresses) and ``warnings`` on stderr too (lines that
    ``--quiet`` must not suppress, such as the busy warning of ``prune``)."""

    out: str = ""
    err: str = ""
    warnings: str = ""


def emit(gateway: Gateway, rendered: Rendered) -> None:
    """Write ``rendered`` through the gateway: stdout, then err, then warnings. Nothing else
    prints."""
    if rendered.out:
        gateway.write_out(rendered.out)
    if rendered.err:
        gateway.write_err(rendered.err)
    if rendered.warnings:
        gateway.write_err(rendered.warnings)


def _value(value: Any) -> str:
    """One scalar as a column value: ``-`` for a missing (None or empty) one, escaped text of
    ``str(value)`` otherwise."""
    text = "" if value is None else str(value)
    return escape_field(text) if text else "-"


def _labels(labels: Any, sep: str) -> str:
    """Labels joined by ``sep`` (a comma, optionally with a space), ``-`` for none. A bare
    string is one label; a separator inside a label is escaped so it cannot split it."""
    if labels is None:
        return "-"
    items = [labels] if isinstance(labels, str) else list(labels)
    if not items:
        return "-"
    comma = sep.strip()
    return sep.join(
        escape_field(str(label)).replace(comma, f"\\u{{{ord(comma):04X}}}") for label in items
    )


def _date(timestamp: Any) -> str:
    """``YYYY-MM-DD`` of a ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (the first ten characters)."""
    return _value(None if timestamp is None else str(timestamp)[:10])


def format_note_line(note: Mapping[str, Any]) -> str:
    """The line of one note in ``list`` and ``search``, without the final LF."""
    fields = (
        _value(note["id"]),
        _value(note["status"]),
        _date(note["created"]),
        _value(note["repo"]),
        _value(note["topic"]),
        _value(note["kind"]),
        _labels(note["labels"], ","),
    )
    return "  ".join(fields)


def format_list(
    notes: Sequence[Mapping[str, Any]], total: int, shown: int, hidden: int
) -> Rendered:
    """Human output of ``list`` and ``search``: a line per note on stdout; on stderr
    ``<shown> of <total> shown`` when ``shown`` < ``total``, the hidden-count line when
    ``hidden`` is above 0, and ``no notes`` when there is nothing to print. The stderr lines
    carry integers only."""
    out = "".join(f"{format_note_line(note)}\n" for note in notes)
    err = ""
    if not notes:
        err += "no notes\n"
    if shown < total:
        err += f"{int(shown)} of {int(total)} shown\n"
    if hidden > 0:
        err += f"{int(hidden)} superseded or expired notes hidden; use --all\n"
    return Rendered(out, err)


def format_show(note: Mapping[str, Any]) -> Rendered:
    """Human output of ``show``. Each body line is prefixed ``| `` (``|`` when empty); the body
    is split on LF only, so a trailing LF shows as a final ``|`` line."""
    lines = [
        f"note {_value(note['id'])}",
        f"repo: {_value(note['repo'])}",
        f"topic: {_value(note['topic'])}",
        f"kind: {_value(note['kind'])}",
        f"labels: {_labels(note['labels'], ', ')}",
        f"status: {_value(note['status'])}",
        f"created: {_value(note['created'])}",
        f"expires: {_value(note['expires'])}",
        f"superseded_by: {_value(note['superseded_by'])}",
        f"supersedes: {_value(note['supersedes'])}",
        f"size: {_value(note['size'])} bytes",
        f"body_sha256: {_value(note['body_sha256'])}",
        "body:",
    ]
    body = "" if note["body"] is None else str(note["body"])
    for line in escape(body).split("\n"):
        lines.append(f"| {line}" if line else "|")
    return Rendered("".join(f"{line}\n" for line in lines))


def format_add(note_id: int, *, created: bool) -> Rendered:
    """``note <id> created`` or ``note <id> exists``."""
    return Rendered(f"note {int(note_id)} {'created' if created else 'exists'}\n")


DRY_RUN_ADD = "dry run: valid; a note would be created unless it exists or a limit refuses it"


def format_add_dry_run() -> Rendered:
    return Rendered(f"{DRY_RUN_ADD}\n")


def format_delete(
    deleted: Sequence[int], reinstated: Mapping[int, tuple[int, bool]] | None = None
) -> Rendered:
    """One line per deleted id: ``deleted note <id>``, then ``; reinstated note <t>`` (with a
    space and ``(expired)`` appended when ``expires`` <= now) when the deletion of that id
    reinstated a note. interface.md allows one reinstated note per deleted note, so
    ``reinstated`` maps a deleted id to ``(t, expired)``; a key that is not in ``deleted``
    raises ``ValueError``. The ``--json`` document (schemas/cli/delete.json) has a flat
    ``reinstated`` list, so the command derives this association; this function only prints it.
    TODO(#30): the command derives which deleted note each reinstated note belongs to."""
    reinstated = reinstated or {}
    unknown = sorted(set(reinstated) - set(deleted))
    if unknown:
        raise ValueError(f"reinstated names notes that were not deleted: {unknown}")
    lines = []
    for note_id in deleted:
        line = f"deleted note {int(note_id)}"
        if note_id in reinstated:
            target, expired = reinstated[note_id]
            line += f"; reinstated note {int(target)}" + (" (expired)" if expired else "")
        lines.append(line)
    return Rendered("".join(f"{line}\n" for line in lines))


BUSY_WARNING = "warning (prune_busy): another writer held the lock; nothing was pruned"


def format_prune(result: Mapping[str, Any]) -> Rendered:
    """Human output of ``prune`` from its ``--json`` document (counts or ``{outcome: busy}``);
    the busy outcome adds the warning on stderr, which ``--quiet`` does not suppress."""
    if result.get("outcome") == "busy":
        return Rendered("prune skipped: store busy\n", warnings=f"{BUSY_WARNING}\n")
    expired, remaining = int(result["expired"]), int(result["remaining"])
    if result["dry_run"]:
        return Rendered(
            f"dry run: would have pruned {expired} expired notes, {remaining} would remain\n"
        )
    return Rendered(f"pruned {expired} expired notes, {remaining} remain\n")


def format_validate(result: Mapping[str, Any]) -> Rendered:
    """Per check ``ok  <name>  <message>`` or ``FAIL  <name>  <message>``; per warning
    ``warning  <name>``. Messages are fixed strings of the tool but are escaped anyway."""
    lines = []
    for check in result["checks"]:
        label = "ok" if check["ok"] else "FAIL"
        lines.append(f"{label}  {_value(check['name'])}  {_value(check['message'])}")
    for warning in result["warnings"]:
        lines.append(f"warning  {_value(warning)}")
    return Rendered("".join(f"{line}\n" for line in lines))


def format_cron(outcome: str, *, dry_run: bool) -> Rendered:
    """``cron block <outcome>``; dry run ``dry run: cron block would be <outcome>``."""
    outcome = _value(outcome)
    if dry_run:
        return Rendered(f"dry run: cron block would be {outcome}\n")
    return Rendered(f"cron block {outcome}\n")
