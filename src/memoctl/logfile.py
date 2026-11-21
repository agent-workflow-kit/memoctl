"""The change log: build one record and append it to ``log.jsonl`` (interface.md, Log).

A record is one JSON object on one line: fixed key order, compact separators, ASCII only
(``ensure_ascii``), ``\\n`` terminated. It never holds a body, a label value or any note text;
every field is checked against its grammar before it is written, so a hostile value is refused
(:class:`LogError` ``invalid_record``) instead of being escaped into the file. A line is at most
:data:`MAX_LINE_BYTES` bytes.

Writing (:func:`append_record`), all relative to the state directory descriptor:

- ``log.jsonl`` is opened ``O_APPEND | O_NOFOLLOW | O_NONBLOCK`` at mode 0600; it must be a
  regular file owned by the user, and a wrong mode of an own file is set back to 0600.
- The line goes out in exactly one ``write`` call, so concurrent writers never interleave
  inside a line. A short write or a write error is a :class:`LogError`; the file may then end
  in a partial line, so the next append starts its line with ``\\n`` when the last byte of the
  file is not one (still one write). A reader skips blank lines (concurrent writers may add
  one) and lines that do not parse.
- Rotation happens only here: with 10 MiB or more in the file, an exclusive ``flock`` on
  ``log.lock`` (polled up to 1 s) is taken, the size is read again by name, and ``log.jsonl``
  is renamed to ``log.jsonl.1`` (replacing an older one) only if it is still that big. The
  writer then opens a fresh ``log.jsonl`` and appends there, still under the lock. A writer
  that cannot get the lock in time, or whose rotation fails (read-only file system, no space,
  a directory called ``log.jsonl.1``), appends to the current file without rotating. A crash
  after the rename leaves every line in ``log.jsonl.1`` and creates the new file on the next
  append: no line is lost or written twice. A writer that opened the old file just before
  another process renamed it appends its one line to ``log.jsonl.1``; it is not lost.

The failure reasons are fixed words, never a path or a value. todo(#16): ``add`` calls
:func:`append_record` after its commit and turns a :class:`LogError` into the stderr warning.
todo(#31): ``prune`` logs one record per removed note through the same function.
todo(#20): the concurrent-writer process tests of the plan's stage 3.
"""

from __future__ import annotations

import errno
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memoctl import __version__
from memoctl.gateway import FileHandle, PathInfo
from memoctl.grammar import is_add_repo_id, matches
from memoctl.statedir import FILE_MODE, StateDir

__all__ = [
    "LOG_NAME",
    "LOCK_NAME",
    "MAX_LINE_BYTES",
    "ROTATED_NAME",
    "ROTATE_BYTES",
    "LogError",
    "LogRecord",
    "append_record",
    "encode_record",
    "format_ts",
]

LOG_NAME = "log.jsonl"
ROTATED_NAME = "log.jsonl.1"
LOCK_NAME = "log.lock"
ROTATE_BYTES = 10 * 1024 * 1024
LOCK_WAIT_SECONDS = 1.0
MAX_LINE_BYTES = 4095  # "under 4096 bytes", the newline included
MAX_VERSION_LENGTH = 64
_VERSION = re.compile(rf"[\x21-\x7e]{{1,{MAX_VERSION_LENGTH}}}")  # printable ASCII, no space
MAX_SIZE = 65536
EVENTS = ("add", "delete", "prune")
ACTOR_SOURCES = ("flag", "tty", "default")
MAX_ID = 999999999999999999
_SHA16 = frozenset("0123456789abcdef")


class LogError(Exception):
    """The record was not written. ``reason`` is a fixed word: ``invalid_record``,
    ``too_long``, ``unsafe_file`` (not a regular file of ours), ``open_failed``,
    ``write_failed`` or ``short_write``. The message never has a path or a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"log not written: {reason}")
        self.reason = reason

    def __reduce__(self) -> tuple[type[LogError], tuple[str]]:
        return (LogError, (self.reason,))


@dataclass(frozen=True)
class LogRecord:
    """The fields of one record in file order. ``None`` is written as JSON ``null``."""

    event: str
    note_id: int
    actor: str
    actor_source: str
    repo: str | None = None
    topic: str | None = None
    kind: str | None = None
    size: int | None = None
    body_sha256: str | None = None
    supersedes: int | None = None
    reinstated: int | None = None


def format_ts(now: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC, floored to milliseconds. ``now`` must be aware."""
    if now.tzinfo is None:
        raise LogError("invalid_record")
    utc = now.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond // 1000:03d}Z"
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check(record: LogRecord) -> None:
    """Refuse anything the log schema (and its per-event rules) would. Never echoes a value."""
    ok = (
        record.event in EVENTS
        and _is_int(record.note_id)
        and 1 <= record.note_id <= MAX_ID
        and matches("actor", record.actor)
        and record.actor_source in ACTOR_SOURCES
        and (record.repo is None or is_add_repo_id(record.repo))
        and (record.topic is None or matches("topic", record.topic))
        and (record.kind is None or matches("kind", record.kind))
        and (record.size is None or (_is_int(record.size) and 1 <= record.size <= MAX_SIZE))
        and (
            record.body_sha256 is None
            or (
                isinstance(record.body_sha256, str)
                and len(record.body_sha256) == 16
                and set(record.body_sha256) <= _SHA16
            )
        )
        and (
            record.supersedes is None
            or (_is_int(record.supersedes) and 1 <= record.supersedes <= MAX_ID)
        )
        and (
            record.reinstated is None
            or (_is_int(record.reinstated) and 1 <= record.reinstated <= MAX_ID)
        )
    )
    if record.event == "add":
        required = (record.repo, record.topic, record.kind, record.size, record.body_sha256)
        ok = ok and None not in required and record.reinstated is None
    elif record.event == "delete":
        ok = ok and record.supersedes is None
    else:
        ok = ok and record.supersedes is None and record.reinstated is None
    if not ok:
        raise LogError("invalid_record")


def encode_record(record: LogRecord, now: datetime, version: str = __version__) -> bytes:
    """The bytes of one log line (the newline included). Raises :class:`LogError`
    ``invalid_record`` or ``too_long``."""
    _check(record)
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise LogError("invalid_record")
    fields: Mapping[str, Any] = {
        "ts": format_ts(now),
        "tool": "memoctl",
        "version": version,
        "event": record.event,
        "note_id": record.note_id,
        "repo": record.repo,
        "topic": record.topic,
        "kind": record.kind,
        "size": record.size,
        "body_sha256": record.body_sha256,
        "supersedes": record.supersedes,
        "reinstated": record.reinstated,
        "actor": record.actor,
        "actor_source": record.actor_source,
    }
    try:
        text = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise LogError("invalid_record") from None
    line = text.encode("ascii") + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise LogError("too_long")
    return line


def _open_checked(state: StateDir, name: str) -> FileHandle:
    """Open ``name`` below the state directory (created at 0600 when missing) and require a
    regular file owned by the user; an own file with another mode is set back to 0600."""
    try:
        try:
            handle = state.handle.open_file_at(name, FILE_MODE, create=True)
        except PermissionError as exc:
            if exc.errno != errno.EACCES:
                raise
            _repair_mode(state, name)
            handle = state.handle.open_file_at(name, FILE_MODE, create=True)
    except LogError:
        raise
    except (OSError, ValueError, NotImplementedError):
        raise LogError("open_failed") from None
    if handle is None:  # only without create; kept for the type
        raise LogError("open_failed")
    try:
        info: PathInfo = handle.stat()
        if info.kind != "file" or info.uid != state.uid:
            raise LogError("unsafe_file")
        if info.mode != FILE_MODE:
            handle.set_mode(FILE_MODE)
    except OSError:
        handle.close()
        raise LogError("open_failed") from None
    except BaseException:
        handle.close()
        raise
    return handle


def _repair_mode(state: StateDir, name: str) -> None:
    """``name`` exists but its mode lacks owner read/write (the ``O_RDWR`` open got ``EACCES``):
    set an own regular file to 0600 so the caller can retry once. A symlink, a FIFO or another
    owner's file is refused; any other failure is an ``OSError``."""
    if not state.handle.repair_file_mode(name, state.uid, FILE_MODE):
        raise LogError("unsafe_file")


def _write_line(handle: FileHandle, line: bytes) -> None:
    """One ``write``. When the file does not end in a newline (a crashed writer's partial
    line), the line starts with one so it cannot merge into the partial one."""
    try:
        size = handle.size()
        if size > 0 and handle.read_at(size - 1, 1) != b"\n":
            line = b"\n" + line
        written = handle.append(line)
    except OSError:
        raise LogError("write_failed") from None
    if written != len(line):
        raise LogError("short_write")


def append_record(state: StateDir, record: LogRecord, now: datetime) -> None:
    """Append ``record`` (time ``now``, read once by the caller) to ``log.jsonl``, rotating
    first when it holds :data:`ROTATE_BYTES` or more. Raises :class:`LogError`; the caller
    warns and keeps its exit code."""
    line = encode_record(record, now)
    log = _open_checked(state, LOG_NAME)
    lock: FileHandle | None = None
    try:
        if _too_big(log):
            lock = _take_lock(state)
            if lock is not None and _rotate(state):
                log.close()
                log = _open_checked(state, LOG_NAME)
        _write_line(log, line)
    finally:
        log.close()
        if lock is not None:
            lock.close()


def _too_big(log: FileHandle) -> bool:
    try:
        return log.size() >= ROTATE_BYTES
    except OSError:
        return False


def _take_lock(state: StateDir) -> FileHandle | None:
    """The exclusive lock on ``log.lock``, or ``None`` when it cannot be had in time (or the
    lock file is unusable): the caller then appends without rotating."""
    try:
        lock = _open_checked(state, LOCK_NAME)
    except LogError:
        return None
    try:
        if lock.lock_exclusive(LOCK_WAIT_SECONDS):
            return lock
    except OSError:
        pass
    lock.close()
    return None


def _rotate(state: StateDir) -> bool:
    """Under the lock: rename ``log.jsonl`` if it is still big (another process may have
    renamed it first, then it is gone or small and nothing is renamed). True when the caller
    must reopen the log by name; False when the rename failed and the caller keeps appending
    to the file it has."""
    try:
        size = state.handle.size_of(LOG_NAME)
        if size is not None and size >= ROTATE_BYTES:
            state.handle.rename(LOG_NAME, ROTATED_NAME)
    except OSError:
        return False
    return True
