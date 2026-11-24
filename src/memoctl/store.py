"""The SQLite store: open it in a mode, check it, migrate it, never trust a row
(interface.md, Database, Migrations and State).

This is the only module that imports ``sqlite3`` (the import-boundary test pins that with an
allowlist), so no sqlite exception, cursor or connection reaches the rest of the tool:
failures leave as :class:`StoreError` (reasons ``db_unreadable``, ``db_locked``,
``db_corrupt``, ``timeout``) or :class:`SchemaNewerError` (``schema_newer``), whose messages
are fixed text with no path, no row content and no sqlite message.

Modes (:class:`Mode`):

- ``READ``: ``context``, ``list``, ``show``, ``search``, ``validate``. ``mode=ro``, busy timeout
  200 ms, never creates or migrates; reads every older schema version. A missing or
  uninitialized store opens as ``None`` (an empty store).
- ``WRITE``: ``delete``, ``prune``. Read-write, busy timeout 5 s, WAL confirmed, migrates an
  older version inside ``BEGIN IMMEDIATE``. A missing or uninitialized store is ``None``.
- ``INIT``: ``add``. Like ``WRITE``, but creates ``state.db`` (exclusively, 0600, through the
  state directory) and initializes an uninitialized one (migration 1).

The file is opened by path only after :meth:`StateDir.check_db` (and the sidecar check). A
same-user process that swaps it in between is outside the boundary (interface.md, Residual
risks).

Every connection: ``trusted_schema`` off, ``SQLITE_DBCONFIG_DEFENSIVE``, triggers and views
disabled (the schema has none), no ATTACH, ``SQLITE_LIMIT_LENGTH`` 1 MiB, ``foreign_keys`` on,
``synchronous=NORMAL``, ``cell_size_check``, and TEXT read through a strict UTF-8 decoder (an
undecodable value cannot be returned as ``str``, so a row check rejects it instead of the
driver raising with the value in its message).

The CLI maps :class:`StoreError` and :class:`SchemaNewerError` to exit 12 (``cli_errors``). The
deadline installs its progress handler through ``open_store(configure=deadline.configure_store)``;
an interrupted statement arrives as ``StoreError("timeout")``.
todo(#15): the change log is appended after :meth:`Store.write` returns (after the commit).
``add``'s write is :meth:`Store.insert_note` (the note, its labels and its rate row), run inside
``Store.write()``. The dedupe and supersede queries (:meth:`Store.live_with_key`,
:meth:`Store.get_note`, :meth:`Store.replaced_note_id`, :meth:`Store.mark_superseded`) run inside
the same transaction, as do the limit queries (:meth:`Store.count_live`,
:meth:`Store.count_notes`, :meth:`Store.count_rate`, :meth:`Store.rate_instant`); the rules that
use them are ``notes.py``'s.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, LiteralString, NamedTuple

from memoctl.grammar import matches
from memoctl.statedir import StateDir, UnsafeStateError

__all__ = [
    "APPLICATION_ID",
    "MIGRATIONS",
    "NoteRow",
    "Mode",
    "SchemaNewerError",
    "Store",
    "StoreError",
    "check_note_row",
    "open_store",
    "supported_version",
]

APPLICATION_ID = 0x6D656D6F  # ASCII "memo"
MIN_SQLITE = (3, 37, 0)
LENGTH_LIMIT = 1_048_576
READ_BUSY_MS = 200
WRITE_BUSY_MS = 5000
PROGRESS_STEPS = 1000
MAX_SCHEMA_OBJECTS = 64  # the real schema has 11; reading stops past this and refuses
MAX_LABELS = 8  # config limits.max_labels is 0 to 8
MAX_TIMESTAMP = 253_402_300_799  # 9999-12-31T23:59:59Z, the last second the output can print
MAX_ID = 999_999_999_999_999_999
MAX_REPO_LENGTH = 255
MAX_BODY_BYTES = 65536
MAX_SIZE = 65536

# Reasons (schemas/defs.json); retryable is true for the first and last only.
DB_UNREADABLE = "db_unreadable"
DB_LOCKED = "db_locked"
DB_CORRUPT = "db_corrupt"
TIMEOUT = "timeout"
_RETRYABLE = {DB_LOCKED, TIMEOUT}

# SQLite primary result codes (sqlite.org/rescode.html).
_BUSY, _LOCKED, _INTERRUPT = 5, 6, 9
_UNREADABLE_CODES = {3, 7, 8, 10, 13, 14, 15, 23, 24}  # PERM NOMEM READONLY IOERR FULL CANTOPEN
# PROTOCOL AUTH FORMAT. Everything else (CORRUPT, NOTADB, TOOBIG, ERROR, CONSTRAINT, ...) is
# corruption: the statement is ours and fixed, so a failure means the file is not what we wrote.


class StoreError(Exception):
    """The store cannot be used (exit 12 ``failed``). ``reason`` is ``db_unreadable``,
    ``db_locked``, ``db_corrupt`` or ``timeout``; the message never carries sqlite's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"store failure: {reason}")
        self.reason = reason

    @property
    def retryable(self) -> bool:
        return self.reason in _RETRYABLE

    def __reduce__(self) -> tuple[Any, ...]:
        return (StoreError, (self.reason,))


class SchemaNewerError(StoreError):
    """``user_version`` is newer than this tool knows (exit 12 ``schema_newer``)."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__("schema_newer")
        self.found = found
        self.supported = supported

    def __reduce__(self) -> tuple[Any, ...]:
        return (SchemaNewerError, (self.found, self.supported))


def translate(exc: sqlite3.Error) -> StoreError:
    """The :class:`StoreError` for a sqlite failure, by primary result code."""
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    if primary in (_BUSY, _LOCKED):
        return StoreError(DB_LOCKED)
    if primary == _INTERRUPT:
        return StoreError(TIMEOUT)
    if primary in _UNREADABLE_CODES:
        return StoreError(DB_UNREADABLE)
    return StoreError(DB_CORRUPT)


def _failure(exc: sqlite3.Error, owner: Store | None = None) -> BaseException:
    """What to raise for a sqlite failure. A Python-side error that carries no result code (a
    wrong parameter count, say) is a bug in this tool's own call, not a damaged file: it
    propagates unchanged (exit 1) instead of becoming ``db_corrupt``. An interrupt caused by
    the progress callback raising is that exception (an internal error), not a ``timeout``."""
    if owner is not None and owner._callback_error is not None:  # pyright: ignore[reportPrivateUsage]
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF == _INTERRUPT:
            error, owner._callback_error = owner._callback_error, None  # pyright: ignore[reportPrivateUsage]
            return error
    if getattr(exc, "sqlite_errorcode", None) is None and isinstance(
        exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)
    ):
        return exc
    return translate(exc)


class Mode(Enum):
    READ = "read"
    WRITE = "write"
    INIT = "init"


# --- schema ---------------------------------------------------------------------------------

# Version -> the statements that bring a store from the previous version to it, run inside one
# BEGIN IMMEDIATE together with the version bump. Forward only, additive. A new version adds
# an entry here, its objects to ``_OBJECTS`` and keeps its readers for every older version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"PRAGMA application_id = {APPLICATION_ID}",
        """CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            topic TEXT NOT NULL,
            kind TEXT NOT NULL,
            body TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            created INTEGER NOT NULL,
            expires INTEGER NOT NULL,
            superseded_by INTEGER
        ) STRICT""",
        "CREATE INDEX notes_repo_id ON notes (repo, id)",
        "CREATE INDEX notes_repo_topic ON notes (repo, topic)",
        "CREATE INDEX notes_superseded_by ON notes (superseded_by)",
        """CREATE TABLE note_labels (
            note_id INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (note_id, key, value)
        ) STRICT""",
        """CREATE TABLE rate (
            repo TEXT NOT NULL,
            at INTEGER NOT NULL
        ) STRICT""",
        "CREATE INDEX rate_at ON rate (at)",
        "CREATE INDEX rate_repo_at ON rate (repo, at)",
    ),
}


def _normalize(sql: object) -> object:
    return " ".join(sql.split()) if isinstance(sql, str) else sql


def _reference(version: int) -> frozenset[tuple[object, ...]]:
    """What ``sqlite_schema`` holds for ``version``: ``(type, name, tbl_name, normalized sql)``
    of every object, built by running migrations 1..version on an in-memory database, so the
    DDL is written once. Anything in a store that differs (a column, a constraint, an index,
    STRICT, an extra object) is not what this version creates: ``db_corrupt``."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for target in range(1, version + 1):
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
        rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema").fetchall()
    finally:
        conn.close()
    return frozenset((t, n, tb, _normalize(q)) for t, n, tb, q in rows)


def supported_version() -> int:
    """The schema version this tool writes (the newest migration)."""
    return max(MIGRATIONS)


# --- reading values -------------------------------------------------------------------------


class _Undecodable:
    """Stands in for a TEXT value that is not valid UTF-8; never a ``str``, carries nothing."""

    __slots__ = ()


UNDECODABLE = _Undecodable()


def _text_factory(data: bytes) -> str | _Undecodable:
    try:
        return data.decode("utf-8")  # strict: surrogates (CESU) and overlong forms fail
    except UnicodeDecodeError:
        return UNDECODABLE


def _is_int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One ``notes`` row that passed :func:`check_note_row`, with its labels when asked for."""

    id: int
    repo: str
    topic: str
    kind: str
    body: str
    body_sha256: str
    size: int
    created: int
    expires: int
    superseded_by: int | None
    labels: tuple[tuple[str, str], ...] = ()


NOTE_COLUMNS: Final = (
    "id, repo, topic, kind, body, body_sha256, size, created, expires, superseded_by"
)
_HEX64 = frozenset("0123456789abcdef")


def check_label_row(key: object, value: object) -> tuple[str, str] | None:
    if type(key) is str and type(value) is str:
        if matches("label_key", key) and matches("label_value", value):
            return key, value
    return None


def _as_int(value: object, low: int, high: int) -> int | None:
    """``value`` when it is an ``int`` (not a ``bool``) within ``low`` to ``high``."""
    if type(value) is int and low <= value <= high:
        return value
    return None


def check_note_row(
    values: Sequence[object], labels: tuple[tuple[str, str], ...] = ()
) -> NoteRow | None:
    """The row as a :class:`NoteRow`, or ``None`` when any column fails its rule.

    Beyond the interface's list (integers, grammars, text), the bounds of the output schemas
    apply, so nothing a command prints can be invalid: timestamps through year 9999, ``size``
    1 to 65536 and equal to the body's UTF-8 length, a body of 1 to 65536 UTF-8 bytes, a repo
    of at most 255 characters and a 64-character lowercase hex ``body_sha256``. A ``bool``, a
    float, a blob or a text that is not valid UTF-8 where an integer or text belongs fails. The
    hash is not recomputed. ``labels`` (already checked by the caller) go into the row.
    """
    if len(values) != 10:
        return None
    id_, repo, topic, kind, body, sha, size, created, expires, superseded = values
    note_id = _as_int(id_, 1, MAX_ID)
    created_at = _as_int(created, 0, MAX_TIMESTAMP)
    expires_at = _as_int(expires, 0, MAX_TIMESTAMP)
    size_bytes = _as_int(size, 1, MAX_SIZE)
    if note_id is None or created_at is None or expires_at is None or size_bytes is None:
        return None
    by = None
    if superseded is not None:
        by = _as_int(superseded, 1, MAX_ID)
        if by is None:
            return None
    if type(repo) is not str or type(topic) is not str or type(kind) is not str:
        return None
    if type(body) is not str or type(sha) is not str:
        return None
    if len(repo) > MAX_REPO_LENGTH or len(sha) != 64 or not set(sha) <= _HEX64:
        return None
    if not (matches("repo", repo) and matches("topic", topic) and matches("kind", kind)):
        return None
    if not 1 <= len(body) <= MAX_BODY_BYTES:
        return None
    if len(body.encode("utf-8")) != size_bytes:  # also bounds the body to MAX_BODY_BYTES
        return None
    return NoteRow(
        note_id, repo, topic, kind, body, sha, size_bytes, created_at, expires_at, by, labels
    )


def _merge_labels(
    note_id: Any,
    pairs: Iterator[tuple[Any, ...]],
    peeked: tuple[Any, ...] | None,
    descending: bool,
) -> tuple[tuple[tuple[str, str], ...] | None, tuple[Any, ...] | None]:
    """The labels of ``note_id`` from ``pairs`` (ordered by ``note_id`` like the rows), sorted
    by key then value, or ``None`` when one is bad or there are more than ``MAX_LABELS``; and
    the first pair of a later note (consumed but not yet used). Pairs of an earlier id (only
    possible outside one snapshot) are dropped."""
    out: list[tuple[str, str]] = []
    bad = False
    count = 0
    item = peeked if peeked is not None else next(pairs, None)
    while item is not None:
        nid = item[0]
        if nid == note_id:
            count += 1
            if count <= MAX_LABELS + 1 and not bad:
                pair = check_label_row(item[1], item[2])
                if pair is None:
                    bad = True
                else:
                    out.append(pair)
        elif (nid > note_id) if descending else (nid < note_id):
            pass  # an earlier note's pair: not ours
        else:
            break
        item = next(pairs, None)
    found = None if bad or count > MAX_LABELS else tuple(sorted(out))
    return found, item


# --- the connection -------------------------------------------------------------------------


class Cursor:
    """A result cursor whose fetches translate sqlite failures (a raw ``DataError`` for a
    value too big to hand to Python would otherwise carry sqlite's text out of the module)."""

    __slots__ = ("_cursor", "_owner")

    def __init__(self, cursor: sqlite3.Cursor, owner: Store | None = None) -> None:
        self._cursor = cursor
        self._owner = owner

    def fetchone(self) -> Any:
        try:
            return self._cursor.fetchone()
        except sqlite3.Error as exc:
            raise _failure(exc, self._owner) from None

    def fetchall(self) -> list[Any]:
        try:
            return self._cursor.fetchall()
        except sqlite3.Error as exc:
            raise _failure(exc, self._owner) from None

    def fetchmany(self, size: int = 1) -> list[Any]:
        try:
            return self._cursor.fetchmany(size)
        except sqlite3.Error as exc:
            raise _failure(exc, self._owner) from None

    def __iter__(self) -> Cursor:
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def close(self) -> None:
        try:
            self._cursor.close()
        except sqlite3.Error:
            pass


_UNSAFE_FRAGMENT = (";", "--", "/*", "'", '"')


class Store:
    """An open, checked store. Use it as a context manager or call :meth:`close`."""

    def __init__(self, conn: sqlite3.Connection, mode: Mode) -> None:
        self._conn = conn
        self.mode = mode
        self.version = 0  # user_version after open (after migration for writers)
        self.bad_rows = 0  # rows :meth:`notes` skipped on this store
        self._busy_ms = 0  # the configured busy timeout (set by open_store)
        self._busy_cap: Callable[[], int | None] | None = None
        self._callback_error: BaseException | None = None  # raised by the progress callback
        self._progress: Callable[[], int] | None = None  # the installed progress callback

    # -- lifecycle
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- the deadline seam (#10)
    def set_progress_handler(self, handler: Callable[[], bool] | None) -> None:
        """Call ``handler`` every :data:`PROGRESS_STEPS` virtual-machine steps; a true return
        interrupts the running statement, which then raises ``StoreError("timeout")``."""
        if handler is None:
            self._progress = None
            self._conn.set_progress_handler(None, 0)
            return

        def callback() -> int:
            try:
                return 1 if handler() else 0
            except BaseException as exc:  # noqa: BLE001 - re-raised by _failure, not a timeout
                self._callback_error = exc
                return 1

        self._progress = callback
        self._conn.set_progress_handler(callback, PROGRESS_STEPS)

    def set_busy_cap(self, cap: Callable[[], int | None] | None) -> None:
        """Cap every busy timeout to ``cap()`` milliseconds (``None``: no cap), asked again
        before each wait that can block (the deadline's time left)."""
        self._busy_cap = cap

    def _budget(self, ms: int) -> int:
        """``ms`` limited by the cap (never negative)."""
        if self._busy_cap is None:
            return ms
        left = self._busy_cap()
        return ms if left is None else max(0, min(ms, left))

    def _apply_busy(self) -> None:
        """Set the connection's busy timeout to the configured one, capped. Without a cap the
        value set at open stays."""
        if self._busy_cap is not None:
            self._conn.execute(f"PRAGMA busy_timeout = {self._budget(self._busy_ms)}")

    # -- statements
    def execute(self, sql: LiteralString, params: Sequence[Any] = ()) -> Cursor:
        """Run one statement; a sqlite failure becomes :class:`StoreError`. ``sql`` is code,
        never input (the type is ``LiteralString``): values go in ``params``, and a value
        sqlite cannot bind raises ``ValueError("invalid parameter")`` without the value. TEXT
        comes back as ``str`` (or the undecodable marker), so rows read this way go through
        :func:`check_note_row`."""
        self._callback_error = None  # one a statement never raised must not leak into this one
        try:
            return Cursor(self._conn.execute(sql, params), self)
        except sqlite3.Error as exc:
            raise _failure(exc, self) from None
        except (UnicodeEncodeError, OverflowError):
            # a lone surrogate, or an integer beyond 64 bits; both messages echo the value
            raise ValueError("invalid parameter") from None

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    @contextmanager
    def write(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` (the lock is taken at once, so the busy timeout applies), commit
        on success, roll back on any exception. Only for ``WRITE`` and ``INIT``."""
        if self.mode is Mode.READ:
            raise RuntimeError("write transaction on a read-only store")
        with self._transaction("BEGIN IMMEDIATE"):
            self._recheck()
            yield

    def _recheck(self) -> None:
        """Under the write lock, read the identity and version again: another process may have
        migrated or replaced the store since it was opened (a refusal rolls the transaction
        back)."""
        try:
            found = _classify(_inspect(self._conn))
        except sqlite3.Error as exc:
            raise _failure(exc, self) from None
        # Any non-CURRENT state after taking the lock (e.g. the file replaced by an empty,
        # uninitialised db) is reported as db_corrupt, deliberately.
        if found != CURRENT:
            raise StoreError(DB_CORRUPT)

    @contextmanager
    def read(self) -> Iterator[None]:
        """One read transaction: every read inside sees one snapshot."""
        with self._transaction("BEGIN"):
            yield

    @contextmanager
    def _transaction(self, begin: LiteralString) -> Iterator[None]:
        if self._conn.in_transaction:
            raise RuntimeError("transaction already open")
        self._callback_error = None
        try:
            self._apply_busy()  # the lock wait of BEGIN IMMEDIATE ends with the deadline
        except sqlite3.Error as exc:
            raise _failure(exc, self) from None
        self.execute(begin)
        try:
            yield
            if self._conn.in_transaction:
                self.execute("COMMIT")
        except BaseException as exc:
            if self._conn.in_transaction:
                # An interrupted COMMIT leaves the transaction open, and the progress handler
                # would interrupt the ROLLBACK as well: take it off for the rollback only, then
                # put the same callback back so later statements stay interruptible.
                self._conn.set_progress_handler(None, 0)
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                finally:
                    if self._progress is not None:
                        self._conn.set_progress_handler(self._progress, PROGRESS_STEPS)
            if isinstance(exc, sqlite3.Error):
                raise _failure(exc, self) from None
            raise

    # -- add
    def insert_note(
        self,
        *,
        repo: str,
        topic: str,
        kind: str,
        body: str,
        body_sha256: str,
        size: int,
        created: int,
        expires: int,
        labels: Sequence[tuple[str, str]],
    ) -> int:
        """Insert one note, its labels and its rate row (``add`` step 13); the new id.

        Call it inside :meth:`write` so that all three inserts commit or none does. Every value
        is bound (never formatted into SQL). The id is checked against the output range (an id
        above 999999999999999999 cannot be printed): ``StoreError("db_corrupt")``, which rolls
        the transaction back, never a stored note the tool cannot show.
        """
        if self.mode is Mode.READ:
            raise RuntimeError("insert on a read-only store")
        cursor = self.execute(
            "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, topic, kind, body, body_sha256, size, created, expires),
        )
        note_id = cursor.lastrowid
        cursor.close()
        if type(note_id) is not int or not 1 <= note_id <= MAX_ID:
            raise StoreError(DB_CORRUPT)
        for key, value in labels:
            self.execute(
                "INSERT INTO note_labels (note_id, key, value) VALUES (?, ?, ?)",
                (note_id, key, value),
            ).close()
        self.execute("INSERT INTO rate (repo, at) VALUES (?, ?)", (repo, created)).close()
        return note_id

    def mark_superseded(self, target: int, by: int) -> None:
        """Set ``superseded_by`` of the live-pointer note ``target`` to ``by`` (``add`` step 13).

        Inside :meth:`write`, after the checks of the same transaction: the note must still be
        unmarked, else ``StoreError("db_corrupt")`` and the transaction rolls back (the checks
        and this write share one lock, so this means the row changed under the tool).
        """
        if self.mode is Mode.READ:
            raise RuntimeError("update on a read-only store")
        cursor = self.execute(
            "UPDATE notes SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
            (by, target),
        )
        changed = cursor.rowcount
        cursor.close()
        if changed != 1:
            raise StoreError(DB_CORRUPT)

    # -- the add checks (read inside the write transaction)
    def get_note(self, note_id: int) -> NoteRow | None:
        """The note ``note_id`` with its labels, or ``None`` when no such row exists. A row that
        fails its checks raises ``StoreError("db_corrupt")``: a write never builds on it."""
        for row in self.notes("WHERE id = ?", (note_id,), labels=True, on_bad="raise"):
            return row
        return None

    def live_with_key(
        self, repo: str, topic: str, kind: str, body_sha256: str, now: int
    ) -> Iterator[NoteRow]:
        """The notes of ``repo`` and ``topic`` with this kind and ``body_sha256`` that are live
        at ``now`` (not superseded, ``expires`` after ``now``), highest id first, with their
        labels. A row that fails its checks is skipped. The caller compares the label sets."""
        return self.notes(
            "WHERE repo = ? AND topic = ? AND kind = ? AND body_sha256 = ?"
            " AND superseded_by IS NULL AND expires > ?",
            (repo, topic, kind, body_sha256, now),
            labels=True,
            descending=True,
        )

    # -- the limit counts (read inside the write transaction; interface.md, Limits)
    def count_live(self, repo: str, now: int, cap: int) -> int:
        """Live notes of ``repo`` at ``now`` (not superseded, ``expires`` after ``now``), counted
        up to ``cap``: the work is bounded by ``cap`` live rows, whatever the store holds."""
        cursor = self.execute(
            "SELECT count(*) FROM (SELECT 1 FROM notes WHERE repo = ? AND superseded_by IS NULL"
            " AND expires > ? LIMIT ?)",
            (repo, now, cap),
        )
        return self._count(cursor)

    def count_notes(self, cap: int) -> int:
        """Notes of any status in the store, counted up to ``cap``."""
        cursor = self.execute("SELECT count(*) FROM (SELECT 1 FROM notes LIMIT ?)", (cap,))
        return self._count(cursor)

    def count_rate(self, repo: str | None, now: int, window: int) -> int:
        """Rate rows (of ``repo``, or of every repo when ``None``) with ``now - window < at <=
        now``: an index range scan; rows dated after ``now`` are not counted."""
        low = now - window
        if repo is None:
            cursor = self.execute("SELECT count(*) FROM rate WHERE at > ? AND at <= ?", (low, now))
        else:
            cursor = self.execute(
                "SELECT count(*) FROM rate WHERE repo = ? AND at > ? AND at <= ?",
                (repo, low, now),
            )
        return self._count(cursor)

    def rate_instant(self, repo: str | None, now: int, window: int, index: int) -> int:
        """The ``at`` of the row at 0-based ``index`` among the rows counted by
        :meth:`count_rate`, oldest first. ``StoreError("db_corrupt")`` when there is none (the
        count and this read share one transaction, so that means the table changed under us)."""
        low = now - window
        if repo is None:
            cursor = self.execute(
                "SELECT at FROM rate WHERE at > ? AND at <= ? ORDER BY at LIMIT 1 OFFSET ?",
                (low, now, index),
            )
        else:
            cursor = self.execute(
                "SELECT at FROM rate WHERE repo = ? AND at > ? AND at <= ? ORDER BY at"
                " LIMIT 1 OFFSET ?",
                (repo, low, now, index),
            )
        found = cursor.fetchone()
        cursor.close()
        if found is None or type(found[0]) is not int:
            raise StoreError(DB_CORRUPT)
        return found[0]

    @staticmethod
    def _count(cursor: Cursor) -> int:
        found = cursor.fetchone()
        cursor.close()
        if found is None or type(found[0]) is not int or found[0] < 0:
            raise StoreError(DB_CORRUPT)
        return found[0]

    def replaced_note_id(self, note_id: int, repo: str | None = None) -> int | None:
        """The smallest valid id of a note whose ``superseded_by`` is ``note_id`` (one at most
        in a store the tool wrote): the note ``note_id`` replaced. With ``repo`` only a note of
        that repo counts, so a planted pointer from another repo never shows its id."""
        if repo is None:
            cursor = self.execute(
                "SELECT id FROM notes WHERE superseded_by = ? AND id <= ? ORDER BY id LIMIT 1",
                (note_id, MAX_ID),
            )
        else:
            cursor = self.execute(
                "SELECT id FROM notes WHERE superseded_by = ? AND id <= ? AND repo = ?"
                " ORDER BY id LIMIT 1",
                (note_id, MAX_ID, repo),
            )
        found = cursor.fetchone()
        cursor.close()
        return found[0] if found is not None and type(found[0]) is int else None

    # -- rows
    def notes(
        self,
        where: LiteralString = "",
        params: Sequence[Any] = (),
        *,
        labels: bool = False,
        on_bad: str = "skip",
        descending: bool = False,
    ) -> Iterator[NoteRow]:
        """The ``notes`` rows (ordered by id, highest first with ``descending``) that pass the
        row checks. The rows stream: a caller that stops early never reads the rest.

        ``where`` is a fixed SQL fragment such as ``"WHERE repo = ?"`` (never input; values go
        in ``params``; the type is ``LiteralString`` and a fragment that does not start with
        ``WHERE `` or holds ``;``, ``--``, ``/*`` or a quote raises ``ValueError``). With
        ``labels`` each row's labels are read and checked too (a note with more than 8 labels
        or a bad one fails). A failing row is skipped and counted in
        :attr:`bad_rows` (``on_bad="skip"``, for ``context``, ``list`` and ``search``) or
        raises ``StoreError("db_corrupt")`` (``on_bad="raise"``, for ``show`` and
        ``validate``). A value over the 1 MiB length limit fails the statement:
        ``StoreError("db_corrupt")`` either way.
        """
        if on_bad not in ("skip", "raise"):
            raise ValueError("on_bad must be skip or raise")
        if where and (not where.startswith("WHERE ") or any(t in where for t in _UNSAFE_FRAGMENT)):
            raise ValueError("where must be a plain WHERE clause")
        direction: LiteralString = "DESC" if descending else "ASC"
        cursor = self.execute(
            f"SELECT {NOTE_COLUMNS} FROM notes {where} ORDER BY id {direction}", params
        )
        pairs: Iterator[tuple[Any, ...]] | None = None
        label_cursor = None
        if labels:  # one statement for every label, merged with the rows (same id order)
            label_cursor = self.execute(
                "SELECT note_id, key, value FROM note_labels"
                f" WHERE note_id IN (SELECT id FROM notes {where}) ORDER BY note_id {direction}",
                tuple(params),
            )
            pairs = self._rows(label_cursor)
        peeked: tuple[Any, ...] | None = None
        try:
            for raw in self._rows(cursor):
                found: tuple[tuple[str, str], ...] | None = ()
                if pairs is not None:
                    found, peeked = _merge_labels(raw[0], pairs, peeked, descending)
                row = None if found is None else check_note_row(raw, found)
                if row is None:
                    if on_bad == "raise":
                        raise StoreError(DB_CORRUPT)
                    self.bad_rows += 1
                    continue
                yield row
        finally:
            cursor.close()
            if label_cursor is not None:
                label_cursor.close()

    @staticmethod
    def _rows(cursor: Cursor) -> Iterator[tuple[Any, ...]]:
        while True:
            raw = cursor.fetchone()
            if raw is None:
                return
            yield raw

    def check_rows(self) -> None:
        """Run every row check over every note and label, and look for labels of no note
        (``validate``); raises
        ``StoreError("db_corrupt")`` for the first failure."""
        for _ in self.notes(labels=True, on_bad="raise"):
            pass
        orphan = self.execute(
            "SELECT 1 FROM note_labels l"
            " WHERE NOT EXISTS (SELECT 1 FROM notes n WHERE n.id = l.note_id) LIMIT 1"
        ).fetchone()
        if orphan is not None:  # a label of no note: a new note could inherit it
            raise StoreError(DB_CORRUPT)

    def quick_check(self) -> bool:
        """``PRAGMA quick_check``: true when SQLite finds the file consistent."""
        rows = list(self._rows(self.execute("PRAGMA quick_check(1)")))
        return rows == [("ok",)]


# --- opening --------------------------------------------------------------------------------


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def _pragma_int(conn: sqlite3.Connection, name: str) -> int:
    value = _scalar(conn, f"PRAGMA {name}")
    if type(value) is not int:
        raise StoreError(DB_CORRUPT)
    return value


def _set_pragma(conn: sqlite3.Connection, name: str, value: str, expect: int) -> None:
    """Set a connection pragma and read it back: a setting that did not take is a failure."""
    conn.execute(f"PRAGMA {name} = {value}")
    if _scalar(conn, f"PRAGMA {name}") != expect:
        raise StoreError(DB_UNREADABLE)


def _configure(conn: sqlite3.Connection, busy_ms: int) -> None:
    """``busy_ms`` is the value to set (capped by the deadline when there is one)."""
    conn.text_factory = _text_factory
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
    if not conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE):
        raise StoreError(DB_UNREADABLE)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, LENGTH_LIMIT)
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    if (
        conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) != LENGTH_LIMIT
        or conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) != 0
    ):
        raise StoreError(DB_UNREADABLE)
    _set_pragma(conn, "trusted_schema", "OFF", 0)
    _set_pragma(conn, "foreign_keys", "ON", 1)
    _set_pragma(conn, "synchronous", "NORMAL", 1)
    _set_pragma(conn, "cell_size_check", "ON", 1)
    _set_pragma(conn, "busy_timeout", str(busy_ms), busy_ms)


class _Snapshot(NamedTuple):
    app: int
    version: int
    schema: frozenset[tuple[object, ...]]  # (type, name, tbl_name, normalized sql)
    sequences: tuple[object, ...]  # sqlite_sequence.seq of "notes" (empty: no such row)


def _inspect(conn: sqlite3.Connection) -> _Snapshot:
    """The identity, schema and id sequence read in one snapshot (a migration committing
    between separate reads would otherwise show a half-set identity). The first query reads the
    file, so a file that is not a database fails here (``db_corrupt``).

    The schema parse runs under the progress handler the ``configure`` hook installed (a planted
    schema with hundreds of thousands of objects is interrupted: ``StoreError("timeout")``), so
    no size cap is needed."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        app = _pragma_int(conn, "application_id")
        version = _pragma_int(conn, "user_version")
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema LIMIT ?", (MAX_SCHEMA_OBJECTS + 1,)
        ).fetchall()
        sequences: tuple[object, ...] = ()
        has_sequence = any(r[0] == "table" and r[1] == "sqlite_sequence" for r in rows)
        if app == APPLICATION_ID and has_sequence:
            found = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'notes' LIMIT 8"
            ).fetchall()
            sequences = tuple(r[0] for r in found)
    finally:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
    if len(rows) > MAX_SCHEMA_OBJECTS:
        raise StoreError(DB_CORRUPT)
    schema = frozenset((t, n, tb, _normalize(q)) for t, n, tb, q in rows)
    return _Snapshot(app, version, schema, sequences)


EMPTY, OLD, CURRENT = "empty", "old", "current"


def _classify(snap: _Snapshot) -> str:
    """The state of what was found, or the refusal. Uninitialized is exactly application_id 0,
    user_version 0 and nothing in the schema; any other ``application_id`` than ``memo`` is not
    ours (a foreign database, or application_id 0 with objects) and is refused before the
    version is looked at. The schema must equal what that version's migrations create."""
    latest = supported_version()
    if snap.app == 0 and snap.version == 0 and not snap.schema:
        return EMPTY
    if snap.app != APPLICATION_ID:
        raise StoreError(DB_CORRUPT)
    if snap.version > latest:
        raise SchemaNewerError(snap.version, latest)
    if snap.version < 1 or snap.version not in MIGRATIONS:
        raise StoreError(DB_CORRUPT)
    if snap.schema != _reference(snap.version):
        raise StoreError(DB_CORRUPT)
    # a planted sequence at the top of the id range would make ``add`` write an id the output
    # cannot hold, or fail for good
    if any(type(q) is not int or q >= MAX_ID for q in snap.sequences):
        raise StoreError(DB_CORRUPT)
    return CURRENT if snap.version == latest else OLD


def _check_labels_against_sequence(conn: sqlite3.Connection) -> None:
    """No label may name a note id the sequence has not issued: otherwise the next note would
    inherit planted labels. ``note_id > seq``, or any label when there is no sequence row."""
    # one statement, so one snapshot: two reads would let a concurrent ``add`` commit between them
    bad = _scalar(
        conn,
        "SELECT EXISTS(SELECT 1 FROM note_labels WHERE note_id >"
        " COALESCE((SELECT max(seq) FROM sqlite_sequence WHERE name = 'notes'), 0))",
    )
    if bad != 0:
        raise StoreError(DB_CORRUPT)


WAL_ATTEMPTS = 5


def _ensure_wal(
    conn: sqlite3.Connection, busy_ms: int, budget: Callable[[int], int] = lambda ms: ms
) -> None:
    """Switch to WAL (before the migration transaction). A mount that cannot hold it leaves
    another mode, or an error: both refuse with ``db_unreadable`` (interface.md does not allow
    a silent fallback to a rollback journal; ``validate`` warns about such mounts).

    Changing the journal mode needs a lock that the busy timeout does not always wait for, so
    several first writers can see ``database is locked`` at once. A busy answer is retried
    (a mode another process already set is accepted); between attempts a ``BEGIN IMMEDIATE``
    waits out the other writer. Each wait uses ``busy_ms // WAL_ATTEMPTS`` so the total stays
    within the documented timeout; each wait is also capped to the deadline's time left. Still
    busy after the attempts: the ``db_locked`` the caller maps."""
    try:
        for attempt in range(WAL_ATTEMPTS):
            conn.execute(f"PRAGMA busy_timeout = {budget(max(busy_ms // WAL_ATTEMPTS, 1))}")
            try:
                mode = _scalar(conn, "PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                if translate(exc).reason != DB_LOCKED or attempt == WAL_ATTEMPTS - 1:
                    raise
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                continue
            if mode != "wal":
                raise StoreError(DB_UNREADABLE)
            return
    finally:
        conn.execute(f"PRAGMA busy_timeout = {budget(busy_ms)}")


def _migrate(conn: sqlite3.Connection) -> int:
    """Bring the store to the newest version in one ``BEGIN IMMEDIATE``; the state is re-read
    under the lock because another process may have migrated since. Returns the version."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _classify(_inspect(conn))
        version = _pragma_int(conn, "user_version")
        if state != CURRENT:
            for target in range(version + 1, supported_version() + 1):
                for statement in MIGRATIONS[target]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
            _classify(_inspect(conn))  # what we built is what we expect
            version = supported_version()
        conn.execute("COMMIT")
        return version
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _uri(path: Path, mode: str) -> str:
    # as_uri percent-encodes ``?``, ``#`` and ``%`` so the path cannot add URI parameters.
    return f"{path.as_uri()}?mode={mode}"


def _connect(uri: str, busy_ms: int) -> sqlite3.Connection:
    return sqlite3.connect(
        uri, uri=True, timeout=busy_ms / 1000, isolation_level=None, check_same_thread=True
    )


def open_store(
    state: StateDir,
    mode: Mode,
    *,
    busy_timeout_ms: int | None = None,
    configure: Callable[[Store], None] | None = None,
    connect: Callable[[str, int], sqlite3.Connection] = _connect,
) -> Store | None:
    """Open ``state.db`` in ``mode``; ``None`` for an empty store (missing or uninitialized)
    when ``mode`` may not create or initialize it.

    Raises :class:`memoctl.statedir.UnsafeStateError` (``state.db`` or a sidecar is not a
    private regular file), :class:`StoreError` and :class:`SchemaNewerError`.
    ``configure`` is called with the :class:`Store` before anything touches the file (the
    deadline installs its progress handler there). ``connect`` is for tests.
    """
    exists = state.check_db()
    if not exists and mode is not Mode.INIT:
        return None
    if sqlite3.sqlite_version_info < MIN_SQLITE:  # only when a database file will be opened
        raise StoreError(DB_UNREADABLE)
    if not exists:
        state.create_db()  # False: another process created it first, which is fine
        if not state.check_db():
            raise UnsafeStateError("race")  # created, then gone again
    state.check_sidecars()
    busy_ms = busy_timeout_ms
    if busy_ms is None:
        busy_ms = READ_BUSY_MS if mode is Mode.READ else WRITE_BUSY_MS
    uri = _uri(state.db_path, "ro" if mode is Mode.READ else "rw")
    try:
        conn = connect(uri, busy_ms)
    except sqlite3.Error as exc:
        raise _failure(exc) from None
    store = Store(conn, mode)
    store._busy_ms = busy_ms  # pyright: ignore[reportPrivateUsage]
    try:
        if configure is not None:
            configure(store)  # first: some pragmas below already parse the schema
        _configure(conn, store._budget(busy_ms))  # pyright: ignore[reportPrivateUsage]
        snap = _inspect(conn)
        found = _classify(snap)
        if found == EMPTY and mode is not Mode.INIT:
            store.close()
            return None
        if mode is Mode.READ:
            store.version = snap.version  # the snapshot the checks ran on
            return store
        _ensure_wal(conn, busy_ms, store._budget)  # pyright: ignore[reportPrivateUsage]
        if found != EMPTY:
            _check_labels_against_sequence(conn)
        if found != CURRENT:
            store._apply_busy()  # pyright: ignore[reportPrivateUsage]
            store.version = _migrate(conn)
        else:
            store.version = supported_version()
        return store
    except sqlite3.Error as exc:
        store.close()
        raise _failure(exc, store) from None
    except BaseException:
        store.close()
        raise
