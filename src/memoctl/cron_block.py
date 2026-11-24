"""The managed block of the user's crontab (interface.md, ``cron``): pure functions on bytes.

The crontab is bytes in and bytes out: it is never decoded, so non-UTF-8 bytes, NUL, CR and
long lines outside the block come back byte for byte. Lines are split on LF only. The block is
three lines: the start marker, one fixed entry that runs ``memoctl prune``, the end marker.
Only the home directory is variable, and it must match :data:`HOME_RE` (cron treats ``%``,
whitespace and other characters specially, so nothing outside that set is ever quoted: it is
refused).

A start or end line is recognised only when it is exactly the marker. A line that merely looks
like one (other spacing, a trailing space or CR, indentation) makes the crontab malformed
rather than being skipped, because replacing or removing around it could cut a hand-edited
block in two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

START = b"# memoctl managed"
END = b"# memoctl managed end"
HOME_RE = re.compile(r"/[A-Za-z0-9._/@+-]*")  # absolute; interface.md also allows the set only
ENTRY_TEMPLATE = (
    "17 3 * * * PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    " memoctl prune --quiet --actor auto:memoctl-cron"
)
MAX_BLOCK_LINE = 4096  # schemas/cli/cron-uninstall.json: the hand-edited middle line

INSTALLED = "installed"
REPLACED = "replaced"
UNCHANGED = "unchanged"
REMOVED = "removed"


class BlockMalformedError(Exception):
    """The managed block is not exactly three well-formed lines (exit 20)."""

    def __init__(self) -> None:
        super().__init__("cron block is malformed")


class CrontabFailedError(Exception):
    """``crontab`` could not be read or written (exit 12). Carries no text of the failure."""

    def __init__(self) -> None:
        super().__init__("crontab failed")


def home_is_safe(home: str) -> bool:
    return HOME_RE.fullmatch(home) is not None


def entry_line(home: str) -> bytes:
    """The middle line for ``home``; the caller has checked :func:`home_is_safe`."""
    if not home_is_safe(home):
        raise ValueError("home cannot be written into a crontab safely")
    return ENTRY_TEMPLATE.format(home=home).encode("ascii")


def block_bytes(home: str) -> bytes:
    return START + b"\n" + entry_line(home) + b"\n" + END + b"\n"


@dataclass(frozen=True)
class Parsed:
    """``lines`` are the LF-terminated pieces of the crontab (the last may lack its LF).
    ``start`` is the index of the start line, or ``None`` when there is no block."""

    lines: list[bytes]
    start: int | None

    @property
    def middle(self) -> bytes | None:
        if self.start is None:
            return None
        return self.lines[self.start + 1].rstrip(b"\n")


def _looks_like(line: bytes, marker: bytes) -> bool:
    """Whether ``line`` is the marker up to spacing (not exactly it): whitespace, CR and the
    space after ``#`` are ignored."""
    words = line.replace(b"\r", b" ").replace(b"\t", b" ").split()
    if not words or not words[0].startswith(b"#"):
        return False
    words[0] = words[0][1:]
    return [w for w in words if w] == marker[2:].split()


def parse(table: bytes | None) -> Parsed:
    """Find the block. Raises :class:`BlockMalformedError` unless there is none, or exactly one
    start line and one end line with the end line two lines after the start line."""
    lines = _split(table or b"")
    starts: list[int] = []
    ends: list[int] = []
    for index, line in enumerate(lines):
        bare = line[:-1] if line.endswith(b"\n") else line
        if bare == START:
            starts.append(index)
        elif bare == END:
            ends.append(index)
        elif _looks_like(bare, START) or _looks_like(bare, END):
            raise BlockMalformedError
    if not starts and not ends:
        return Parsed(lines, None)
    if len(starts) != 1 or len(ends) != 1 or ends[0] != starts[0] + 2:
        raise BlockMalformedError
    return Parsed(lines, starts[0])


def _split(table: bytes) -> list[bytes]:
    """Split on LF only, keeping each LF; ``bytes.splitlines`` would also split on CR, FF, VT."""
    lines = table.split(b"\n")
    pieces = [line + b"\n" for line in lines[:-1]]
    if lines[-1]:
        pieces.append(lines[-1])
    return pieces


def plan_install(table: bytes | None, home: str) -> tuple[str, bytes | None, list[bytes]]:
    """``(outcome, new table or None when nothing is written, block lines)``."""
    parsed = parse(table)
    middle = entry_line(home)
    block = [START, middle, END]
    if parsed.start is None:
        base = table or b""
        if base and not base.endswith(b"\n"):
            base += b"\n"
        return INSTALLED, base + block_bytes(home), block
    if parsed.middle == middle:
        return UNCHANGED, None, block
    at = parsed.start
    new = b"".join(parsed.lines[:at]) + block_bytes(home) + b"".join(parsed.lines[at + 3 :])
    return REPLACED, new, block


def plan_uninstall(table: bytes | None) -> tuple[str, bytes | None, list[bytes]]:
    """``(outcome, new table or None, the block as found (empty when absent))``."""
    parsed = parse(table)
    if parsed.start is None:
        return UNCHANGED, None, []
    at = parsed.start
    found = [line.rstrip(b"\n") for line in parsed.lines[at : at + 3]]
    new = b"".join(parsed.lines[:at]) + b"".join(parsed.lines[at + 3 :])
    return REMOVED, new, found
