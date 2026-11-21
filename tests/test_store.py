"""The SQLite store (interface.md, Database, Migrations, State).

Every test runs on a private temp home. Raw ``sqlite3`` is used here to build hostile files
(the import boundary applies to ``src`` only).
"""

from __future__ import annotations

import os
import pickle
import sqlite3
import stat
import threading
import traceback
from pathlib import Path
from typing import Any

import pytest

from memoctl import store as store_mod
from memoctl.statedir import StateDir, UnsafeStateError, open_state_dir
from memoctl.store import (
    APPLICATION_ID,
    Mode,
    NoteRow,
    SchemaNewerError,
    StoreError,
    check_note_row,
    open_store,
)
from tests.test_statedir import HomeGateway

SHA = "a" * 64


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "h"
    h.mkdir()
    return h


@pytest.fixture
def state(home: Path) -> StateDir:
    sd = open_state_dir(HomeGateway(home), create=True)
    yield_sd = sd
    return yield_sd


def db(state: StateDir) -> Path:
    return state.db_path


def init(state: StateDir) -> None:
    s = open_store(state, Mode.INIT)
    assert s is not None
    s.close()


def raw(state: StateDir) -> sqlite3.Connection:
    state.create_db()  # exclusive 0600; a no-op when it exists
    return sqlite3.connect(str(db(state)), isolation_level=None)


def note_values(**over: Any) -> list[Any]:
    base = dict(
        id=1, repo="example.com/a/b", topic="t", kind="unknown", body="hello", sha=SHA,
        size=5, created=1000, expires=2000, superseded_by=None,
    )  # fmt: skip
    base.update(over)
    return list(base.values())


def insert(conn: sqlite3.Connection, **over: Any) -> None:
    conn.execute(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires,"
        " superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        note_values(**over),
    )


# --- creation and modes ---------------------------------------------------------------------


def test_missing_store_is_empty_for_read_and_write(state: StateDir) -> None:
    assert open_store(state, Mode.READ) is None
    assert open_store(state, Mode.WRITE) is None
    assert not db(state).exists()  # nothing but INIT creates


def test_init_creates_a_private_wal_store_with_the_pragmas(state: StateDir) -> None:
    s = open_store(state, Mode.INIT)
    assert s is not None
    with s:
        assert s.version == 1
        assert stat.S_IMODE(db(state).stat().st_mode) == 0o600
        one = lambda sql: s.execute(sql).fetchone()[0]  # noqa: E731
        assert one("PRAGMA journal_mode") == "wal"
        assert one("PRAGMA foreign_keys") == 1
        assert one("PRAGMA synchronous") == 1
        assert one("PRAGMA trusted_schema") == 0
        assert one("PRAGMA application_id") == APPLICATION_ID == 0x6D656D6F
        assert one("PRAGMA user_version") == 1
        assert one("PRAGMA busy_timeout") == 5000
        names = {r[0] for r in s.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert {"notes", "note_labels", "rate", "sqlite_sequence"} <= names
    for sidecar in ("-wal", "-shm"):
        p = Path(str(db(state)) + sidecar)
        if p.exists():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_tables_are_strict_and_cascade(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        with pytest.raises(StoreError) as e, s.write():
            s.execute("INSERT INTO rate (repo, at) VALUES ('x', 'not a number')")
        assert e.value.reason == "db_corrupt"
        with s.write():
            s.execute(
                "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created,"
                " expires) VALUES ('a/b','t','unknown','x',?,1,1,2)",
                (SHA,),
            )
            s.execute("INSERT INTO note_labels VALUES (1, 'k', 'v')")
            s.execute("DELETE FROM notes WHERE id = 1")
            assert s.execute("SELECT count(*) FROM note_labels").fetchone()[0] == 0
            with pytest.raises(StoreError):  # foreign keys are on
                s.execute("INSERT INTO note_labels VALUES (99, 'k', 'v')")


def test_ids_are_never_reused(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        with s.write():
            for _ in range(2):
                s.execute(
                    "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created,"
                    " expires) VALUES ('a/b','t','unknown','x',?,1,1,2)",
                    (SHA,),
                )
            s.execute("DELETE FROM notes WHERE id = 2")
            s.execute(
                "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created,"
                " expires) VALUES ('a/b','t','unknown','x',?,1,1,2)",
                (SHA,),
            )
            assert s.execute("SELECT max(id) FROM notes").fetchone()[0] == 3


def test_reopen_is_idempotent_and_read_mode_is_read_only(state: StateDir) -> None:
    init(state)
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert s.version == 1
        assert s.execute("PRAGMA busy_timeout").fetchone()[0] == 200
        with pytest.raises(RuntimeError):
            with s.write():
                pass
        with pytest.raises(StoreError) as e:
            s.execute("CREATE TABLE x (a)")
        assert e.value.reason == "db_unreadable"  # SQLITE_READONLY


def test_a_read_open_of_a_wal_store_adds_only_sidecars_and_keeps_the_file(
    state: StateDir,
) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    before = db(state).read_bytes()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        list(s.notes())
        names = {p.name for p in state.path.iterdir()}
        assert names <= {"state.db", "state.db-wal", "state.db-shm"}
    assert db(state).read_bytes() == before


def test_read_leaves_wal_sidecars_at_the_database_mode(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        list(s.notes())
        for sidecar in ("-wal", "-shm"):
            p = Path(str(db(state)) + sidecar)
            if p.exists():
                assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("mode", [Mode.READ, Mode.WRITE])
def test_zero_length_file_is_an_empty_store(state: StateDir, mode: Mode) -> None:
    assert state.create_db()
    assert open_store(state, mode) is None
    assert db(state).stat().st_size == 0  # a reader or deleter does not initialize


def test_init_initializes_a_zero_length_file(state: StateDir) -> None:
    assert state.create_db()
    with open_store(state, Mode.INIT) as s:  # type: ignore[union-attr]
        assert s.version == 1


def test_uninitialized_but_wal_header_is_still_empty(state: StateDir) -> None:
    c = raw(state)
    c.execute("PRAGMA journal_mode=WAL")
    c.close()
    assert open_store(state, Mode.READ) is None


# --- identity, versions and migrations ------------------------------------------------------


def _fake_v2(monkeypatch: pytest.MonkeyPatch, *statements: str) -> None:
    monkeypatch.setitem(store_mod.MIGRATIONS, 2, statements)


def test_migrates_forward_in_one_transaction_for_writers(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(state)
    _fake_v2(monkeypatch, "CREATE TABLE extra (a INTEGER) STRICT")
    with open_store(state, Mode.READ) as r:  # type: ignore[union-attr]
        assert r.version == 1  # readers read older versions and never migrate
    c = raw(state)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    with open_store(state, Mode.WRITE) as w:  # type: ignore[union-attr]
        assert w.version == 2
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    c.close()


def test_a_failing_migration_rolls_back_everything(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(state)
    _fake_v2(monkeypatch, "CREATE TABLE extra (a INTEGER) STRICT", "THIS IS NOT SQL")
    with pytest.raises(StoreError) as e:
        open_store(state, Mode.WRITE)
    assert e.value.reason == "db_corrupt"
    c = raw(state)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM sqlite_schema WHERE name='extra'").fetchone()[0] == 0
    assert not c.in_transaction
    c.close()
    with open_store(state, Mode.READ) as r:  # type: ignore[union-attr]
        assert r.version == 1  # and the store still opens


def test_newer_schema_is_refused_in_every_mode(state: StateDir) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA user_version = 7")
    c.close()
    for mode in Mode:
        with pytest.raises(SchemaNewerError) as e:
            open_store(state, mode)
        assert (e.value.found, e.value.supported, e.value.reason) == (7, 1, "schema_newer")
    assert raw(state).execute("PRAGMA user_version").fetchone()[0] == 7  # never downgraded


def test_exceptions_pickle_and_carry_no_text(state: StateDir) -> None:
    e = pickle.loads(pickle.dumps(SchemaNewerError(5, 1)))  # noqa: S301
    assert (e.found, e.supported, e.reason) == (5, 1, "schema_newer")
    s = pickle.loads(pickle.dumps(StoreError("db_locked")))  # noqa: S301
    assert s.reason == "db_locked" and s.retryable
    assert not StoreError("db_corrupt").retryable


@pytest.mark.parametrize("mode", list(Mode))
def test_foreign_application_id_is_corrupt(state: StateDir, mode: Mode) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA application_id = 12345")
    c.execute("PRAGMA user_version = 99")  # foreign beats newer
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt" and not isinstance(e.value, SchemaNewerError)


@pytest.mark.parametrize("mode", list(Mode))
def test_application_id_zero_with_objects_is_not_ours(state: StateDir, mode: Mode) -> None:
    c = raw(state)
    c.execute("CREATE TABLE other (a)")
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"
    assert "other" not in str(e.value)


def test_application_id_zero_with_user_version_is_not_uninitialized(state: StateDir) -> None:
    c = raw(state)
    c.execute("PRAGMA user_version = 1")
    c.close()
    with pytest.raises(StoreError):
        open_store(state, Mode.READ)


def test_memo_id_with_version_zero_is_corrupt(state: StateDir) -> None:
    c = raw(state)
    c.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, Mode.INIT)
    assert e.value.reason == "db_corrupt"


@pytest.mark.parametrize("mode", list(Mode))
def test_an_old_sqlite_library_is_refused_with_a_fixed_reason(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    init(state)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 9))
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_unreadable"


@pytest.mark.parametrize("mode", [Mode.READ, Mode.WRITE])
def test_an_old_sqlite_library_does_not_hide_a_missing_store(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 9))
    assert open_store(state, mode) is None  # no file will be opened, so no refusal


# --- hostile files --------------------------------------------------------------------------


def test_state_db_as_symlink_dir_or_fifo_is_unsafe(state: StateDir, home: Path) -> None:
    target = home / "elsewhere.db"
    target.write_bytes(b"")
    os.symlink(target, db(state))
    with pytest.raises(UnsafeStateError):
        open_store(state, Mode.READ)
    os.unlink(db(state))
    os.mkdir(db(state), 0o700)
    with pytest.raises(UnsafeStateError):
        open_store(state, Mode.INIT)
    os.rmdir(db(state))
    os.mkfifo(db(state), 0o600)
    with pytest.raises(UnsafeStateError) as e:  # must not block on the FIFO
        open_store(state, Mode.WRITE)
    assert e.value.reason == "not_a_file"


def test_state_db_with_group_bits_is_unsafe(state: StateDir) -> None:
    init(state)
    os.chmod(db(state), 0o640)
    with pytest.raises(UnsafeStateError) as e:
        open_store(state, Mode.READ)
    assert e.value.reason == "mode"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecar_symlink_or_wrong_mode_is_unsafe(state: StateDir, home: Path, suffix: str) -> None:
    init(state)
    side = Path(str(db(state)) + suffix)
    if side.exists():
        side.unlink()
    os.symlink(home / "nowhere", side)
    with pytest.raises(UnsafeStateError) as e:
        open_store(state, Mode.READ)
    assert e.value.reason == "not_a_file"
    side.unlink()
    side.write_bytes(b"")
    os.chmod(side, 0o644)
    with pytest.raises(UnsafeStateError) as e:
        open_store(state, Mode.WRITE)
    assert e.value.reason == "mode"


@pytest.mark.parametrize(
    "content",
    [
        b"x" * 5000,  # garbage
        b"SQLite format 3\x00",  # header prefix only (truncated)
        b"SQLite format 3\x00" + b"\xff" * 4200,  # valid magic, garbage page
        b"\x00" * 100,
    ],
    ids=["garbage", "truncated", "bad-page", "short-zeros"],
)
@pytest.mark.parametrize("mode", list(Mode))
def test_foreign_or_corrupt_files_are_db_corrupt(
    state: StateDir, content: bytes, mode: Mode
) -> None:
    db(state).write_bytes(content)
    os.chmod(db(state), 0o600)
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"
    assert str(db(state)) not in str(e.value)


def test_truncated_real_database_is_db_corrupt(state: StateDir) -> None:
    init(state)
    c = raw(state)
    for i in range(1, 400):
        insert(c, id=i, body="x" * 2000)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    size = db(state).stat().st_size
    with open(db(state), "r+b") as f:
        f.truncate(size // 2)
    with pytest.raises(StoreError) as e:
        with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
            list(s.notes())
    assert e.value.reason == "db_corrupt"


def test_header_with_huge_page_count_does_not_hang_or_allocate(state: StateDir) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    with open(db(state), "r+b") as f:
        f.seek(28)
        f.write((0x7FFFFFFF).to_bytes(4, "big"))  # in-header page count
    try:
        with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
            list(s.notes())
    except StoreError as e:
        assert e.reason in ("db_corrupt", "db_unreadable")


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TRIGGER trg AFTER INSERT ON rate BEGIN DELETE FROM rate; END",
        "CREATE VIEW v AS SELECT 1",
        "CREATE TABLE extra (a)",
        "CREATE INDEX extra_i ON rate (repo)",
        "CREATE VIRTUAL TABLE vt USING fts5(a)",
    ],
)
@pytest.mark.parametrize("mode", list(Mode))
def test_unexpected_schema_objects_are_db_corrupt(state: StateDir, ddl: str, mode: Mode) -> None:
    init(state)
    c = raw(state)
    try:
        c.execute(ddl)
    except sqlite3.Error:
        pytest.skip("this sqlite has no fts5")
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"


def test_triggers_and_views_never_run_even_if_present(state: StateDir) -> None:
    # Defence in depth: the schema check refuses them, and the connection disables them.
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        s.execute("CREATE VIEW v AS SELECT 1 AS a")
        with pytest.raises(StoreError):
            s.execute("SELECT * FROM v").fetchall()
        s.execute(
            "CREATE TRIGGER t AFTER INSERT ON rate BEGIN INSERT INTO rate VALUES ('fired', 1); END"
        )
        with s.write():
            s.execute("INSERT INTO rate VALUES ('a/b', 1)")
        assert s.execute("SELECT count(*) FROM rate").fetchone()[0] == 1


def test_connection_refuses_attach_and_oversized_values(state: StateDir, home: Path) -> None:
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        with pytest.raises(StoreError):
            s.execute("ATTACH DATABASE ':memory:' AS other")
        with s.write(), pytest.raises(StoreError) as e:
            s.execute(
                "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created,"
                " expires) VALUES ('a/b','t','unknown',?,?,1,1,2)",
                ("x" * 1_100_000, SHA),
            )
        assert e.value.reason == "db_corrupt"  # SQLITE_TOOBIG


def test_defensive_blocks_writable_schema(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        with pytest.raises(StoreError):
            s.execute("PRAGMA writable_schema = ON")
            s.execute("UPDATE sqlite_schema SET name = 'zzz' WHERE name = 'rate'")
    with open_store(state, Mode.READ) as r:  # type: ignore[union-attr]
        assert r.version == 1


def test_a_database_made_by_a_rival_midway_is_opened_not_clobbered(state: StateDir) -> None:
    assert state.create_db()
    assert not state.create_db()  # the second creator sees EEXIST
    with open_store(state, Mode.INIT) as s:  # type: ignore[union-attr]
        assert s.version == 1


def test_racing_initializers_all_succeed_once(state: StateDir, home: Path) -> None:
    barrier = threading.Barrier(8)
    errors: list[str] = []
    gw = HomeGateway(home)

    def worker() -> None:
        try:
            sd = open_state_dir(gw, create=True)
            barrier.wait()
            with open_store(sd, Mode.INIT) as s:  # type: ignore[union-attr]
                assert s.version == 1
            sd.close()
        except BaseException:  # noqa: BLE001
            errors.append(traceback.format_exc())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    c = raw(state)
    assert c.execute("SELECT count(*) FROM sqlite_schema WHERE name='notes'").fetchone()[0] == 1
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    c.close()


# --- locks, deadlines, failures of the medium -----------------------------------------------


def test_a_held_write_lock_is_db_locked_for_writers_and_not_for_readers(state: StateDir) -> None:
    init(state)
    holder = raw(state)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with open_store(state, Mode.WRITE, busy_timeout_ms=50) as w:  # type: ignore[union-attr]
            with pytest.raises(StoreError) as e, w.write():
                pass
            assert e.value.reason == "db_locked" and e.value.retryable
        with open_store(state, Mode.READ) as r:  # type: ignore[union-attr]
            assert list(r.notes()) == []  # WAL readers do not wait for the writer
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_held_exclusive_lock_in_rollback_mode_is_db_locked_for_open(state: StateDir) -> None:
    c = raw(state)
    c.execute("CREATE TABLE t (a)")  # not ours, and locked: the first read fails busy
    c.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreError) as e:
            open_store(state, Mode.READ, busy_timeout_ms=30)
        assert e.value.reason == "db_locked"
    finally:
        c.execute("ROLLBACK")
        c.close()


def test_progress_handler_interrupt_is_timeout(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        s.set_progress_handler(lambda: True)
        with pytest.raises(StoreError) as e:
            s.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 10000000)"
                " SELECT count(*) FROM c"
            ).fetchall()
        assert e.value.reason == "timeout" and e.value.retryable
        s.set_progress_handler(None)
        assert s.execute("SELECT 1").fetchone() == (1,)


def test_configure_runs_before_the_first_read(state: StateDir) -> None:
    init(state)
    seen: list[str] = []
    with open_store(state, Mode.READ, configure=lambda s: seen.append("hook")) as s:  # type: ignore[union-attr]
        assert s is not None
    assert seen == ["hook"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_read_only_directory_makes_a_wal_read_db_unreadable(state: StateDir) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    for sidecar in ("-wal", "-shm"):
        Path(str(db(state)) + sidecar).unlink(missing_ok=True)
    os.chmod(state.path, 0o500)
    try:
        with pytest.raises(StoreError) as e:
            open_store(state, Mode.READ)
        assert e.value.reason == "db_unreadable"
    finally:
        os.chmod(state.path, 0o700)


class _NoWalConnection(sqlite3.Connection):
    """A connection on a mount that cannot hold WAL: the pragma answers with the old mode."""

    def execute(self, sql: str, *a: Any) -> sqlite3.Cursor:
        if sql.replace(" ", "").upper() == "PRAGMAJOURNAL_MODE=WAL":
            return super().execute("SELECT 'delete'")
        return super().execute(sql, *a)


def test_when_wal_cannot_be_enabled_the_open_is_refused(state: StateDir) -> None:
    def connect(uri: str, busy_ms: int) -> sqlite3.Connection:
        return sqlite3.connect(
            uri, uri=True, isolation_level=None, factory=_NoWalConnection, timeout=busy_ms / 1000
        )

    with pytest.raises(StoreError) as e:
        open_store(state, Mode.INIT, connect=connect)
    assert e.value.reason == "db_unreadable"
    c = raw(state)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0  # nothing was initialized
    c.close()


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (sqlite3.SQLITE_BUSY, "db_locked"),
        (sqlite3.SQLITE_LOCKED, "db_locked"),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "db_locked"),
        (sqlite3.SQLITE_INTERRUPT, "timeout"),
        (sqlite3.SQLITE_FULL, "db_unreadable"),
        (sqlite3.SQLITE_READONLY, "db_unreadable"),
        (sqlite3.SQLITE_READONLY_DIRECTORY, "db_unreadable"),
        (sqlite3.SQLITE_IOERR, "db_unreadable"),
        (sqlite3.SQLITE_CANTOPEN, "db_unreadable"),
        (sqlite3.SQLITE_PERM, "db_unreadable"),
        (sqlite3.SQLITE_CORRUPT, "db_corrupt"),
        (sqlite3.SQLITE_NOTADB, "db_corrupt"),
        (sqlite3.SQLITE_TOOBIG, "db_corrupt"),
        (sqlite3.SQLITE_ERROR, "db_corrupt"),
    ],
)
def test_translate_by_primary_result_code(code: int, reason: str) -> None:
    exc = sqlite3.OperationalError("secret row text")
    exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    out = store_mod.translate(exc)
    assert out.reason == reason
    assert "secret" not in str(out)


def test_connect_failure_without_a_code_is_corrupt_by_default() -> None:
    assert store_mod.translate(sqlite3.DatabaseError("x")).reason == "db_corrupt"


def test_paths_with_uri_metacharacters_open_the_right_file(tmp_path: Path) -> None:
    h = tmp_path / "h?mode=memory#x%41 y"
    h.mkdir()
    sd = open_state_dir(HomeGateway(h), create=True)
    with open_store(sd, Mode.INIT) as s:  # type: ignore[union-attr]
        assert s.version == 1
    assert (h / ".local/state/memoctl/state.db").stat().st_size > 0


def test_sidecar_check_ignores_absent_files(state: StateDir) -> None:
    state.check_sidecars()


# --- row checks -----------------------------------------------------------------------------


def test_a_good_row_passes() -> None:
    row = check_note_row(note_values())
    assert row == NoteRow(1, "example.com/a/b", "t", "unknown", "hello", SHA, 5, 1000, 2000, None)


def test_a_good_row_with_every_edge_value_passes() -> None:
    row = check_note_row(
        note_values(
            id=999_999_999_999_999_999, repo="a" + "/b" * 126, topic="a" * 64,
            body="é\x00\n" * 3 + "a" * 65524, size=65536, created=0, expires=253_402_300_799,
            superseded_by=5,
        )
    )  # fmt: skip
    assert row is not None  # NUL and controls in a body are text; the sanitizer owns them


@pytest.mark.parametrize(
    "over",
    [
        dict(id=0),
        dict(id=-1),
        dict(id=10**18),
        dict(id=True),
        dict(id=1.0),
        dict(id="1"),
        dict(id=None),
        dict(id=b"1"),
        dict(superseded_by=0),
        dict(superseded_by=1.5),
        dict(superseded_by="2"),
        dict(superseded_by=False),
        dict(created=-1),
        dict(created=253_402_300_800),
        dict(created="1"),
        dict(created=None),
        dict(created=1.0),
        dict(expires=2**62),
        dict(expires=None),
        dict(size=0),
        dict(size=65537),
        dict(size=4),  # not the body's UTF-8 length
        dict(body="é", size=1),  # characters, not bytes
        dict(size="5"),
        dict(size=None),
        dict(repo="Example.com/a"),
        dict(repo="example.com"),
        dict(repo="example.com/../x"),
        dict(repo="example.com/a\n"),
        dict(repo=""),
        dict(repo=b"example.com/a"),
        dict(repo=None),
        dict(repo="a" + "/b" * 200),
        dict(repo="ｅxample.com/a"),
        dict(topic=""),
        dict(topic="A"),
        dict(topic="a" * 65),
        dict(topic="a\n"),
        dict(topic=1),
        dict(topic="-a"),
        dict(kind=""),
        dict(kind="Unknown"),
        dict(kind="a b"),
        dict(kind=None),
        dict(body=""),
        dict(body=None),
        dict(body=b"hello"),
        dict(body=5),
        dict(body="x" * 65537),
        dict(body="é" * 40000),
        dict(sha="A" * 64),
        dict(sha="a" * 63),
        dict(sha="a" * 65),
        dict(sha="g" * 64),
        dict(sha=None),
        dict(sha="a" * 63 + "\n"),
    ],
    ids=lambda d: str(d)[:40],
)
def test_each_failing_column_fails_the_row(over: dict[str, Any]) -> None:
    assert check_note_row(note_values(**over)) is None


def test_wrong_arity_fails() -> None:
    assert check_note_row(note_values()[:9]) is None


def test_a_blank_database_row_is_checked_through_the_store(state: StateDir) -> None:
    init(state)
    c = raw(state)
    insert(c, id=1)
    insert(c, id=2, repo="NOT A REPO")
    insert(c, id=3, body="ok", size=2)
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert [n.id for n in s.notes()] == [1, 3]
        assert s.bad_rows == 1
        with pytest.raises(StoreError) as e:
            list(s.notes(on_bad="raise"))
        assert e.value.reason == "db_corrupt"
        assert "NOT A REPO" not in str(e.value)
        assert [n.id for n in s.notes("WHERE id = ?", (3,))] == [3]
        with pytest.raises(ValueError):
            list(s.notes(on_bad="ignore"))
        with pytest.raises(StoreError):
            s.check_rows()


LOOSE_DDL = (
    "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, repo, topic, kind, body,"
    " body_sha256, size, created, expires, superseded_by)",
    "CREATE INDEX notes_repo_id ON notes (repo, id)",
    "CREATE INDEX notes_repo_topic ON notes (repo, topic)",
    "CREATE INDEX notes_superseded_by ON notes (superseded_by)",
    "CREATE TABLE note_labels (note_id, key, value, PRIMARY KEY (note_id, key, value))",
    "CREATE TABLE rate (repo, at)",
    "CREATE INDEX rate_at ON rate (at)",
    "CREATE INDEX rate_repo_at ON rate (repo, at)",
)


def _loose_db(state: StateDir, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A memoctl-looking store whose tables are not STRICT, so hostile types can be stored.
    The version-1 migration is swapped for the loose DDL so the schema check accepts it."""
    monkeypatch.setitem(
        store_mod.MIGRATIONS,
        1,
        (f"PRAGMA application_id = {APPLICATION_ID}", *LOOSE_DDL),
    )
    c = raw(state)
    c.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    c.execute("PRAGMA user_version = 1")
    for ddl in LOOSE_DDL:
        c.execute(ddl)
    return c


def test_hostile_values_in_a_loose_table_are_skipped_not_crashed(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _loose_db(state, monkeypatch)
    insert(c, id=1)  # good
    insert(c, id=2, body=b"\xff\xfe blob")  # BLOB where text belongs
    insert(c, id=3, created="yesterday")
    insert(c, id=4, created=1.5)
    insert(c, id=5, topic="x" * 100)
    c.execute(  # invalid UTF-8 inside a TEXT value: the driver would raise with the value
        "INSERT INTO notes VALUES (6, 'example.com/a', 't', 'unknown', CAST(x'41ED A080' AS TEXT),"
        " ?, 1, 1, 2, NULL)".replace(" A080", "A080"),
        (SHA,),
    )
    c.execute(  # a lone surrogate, as CESU-8 bytes
        "INSERT INTO notes VALUES (7, 'example.com/a', 't', 'unknown', CAST(x'EDA080' AS TEXT),"
        " ?, 1, 1, 2, NULL)",
        (SHA,),
    )
    insert(c, id=8, body="with\x00nul", size=8)  # text with NUL is text
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        got = [n.id for n in s.notes()]
        assert got == [1, 8]
        assert s.bad_rows == 6


def test_a_body_over_one_mebibyte_fails_the_statement_as_corrupt(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _loose_db(state, monkeypatch)
    insert(c, id=1, body="x" * 2_000_000)
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        with pytest.raises(StoreError) as e:
            list(s.notes())
        assert e.value.reason == "db_corrupt"


def test_labels_are_checked_and_capped(state: StateDir) -> None:
    init(state)
    c = raw(state)
    for i in (1, 2, 3, 4):
        insert(c, id=i)
    c.execute("INSERT INTO note_labels VALUES (1, 'b', 'x'), (1, 'a', '')")
    c.execute("INSERT INTO note_labels VALUES (2, 'Bad', 'x')")
    c.execute("INSERT INTO note_labels VALUES (3, 'k', 'sp ace')")
    c.executemany(
        "INSERT INTO note_labels VALUES (4, ?, 'v')", [(f"k{i}",) for i in range(9)]
    )  # nine labels
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        got = list(s.notes(labels=True))
        assert [(n.id, n.labels) for n in got] == [(1, (("a", ""), ("b", "x")))]
        assert s.bad_rows == 3
        assert [n.id for n in s.notes()] == [1, 2, 3, 4]  # without labels they are not looked at


def test_labels_with_loose_types_fail_the_note(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _loose_db(state, monkeypatch)
    insert(c, id=1)
    c.execute("INSERT INTO note_labels VALUES (1, 'k', 5)")
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert list(s.notes(labels=True)) == []


def test_read_transaction_gives_one_snapshot(state: StateDir) -> None:
    init(state)
    c = raw(state)
    insert(c, id=1)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        with s.read():
            assert len(list(s.notes())) == 1
            insert(c, id=2, body="b", size=1)
            assert len(list(s.notes())) == 1
        assert len(list(s.notes())) == 2
    c.close()


def test_write_commits_and_rolls_back(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.WRITE) as s:  # type: ignore[union-attr]
        with s.write():
            s.execute("INSERT INTO rate VALUES ('a/b', 1)")
        with pytest.raises(KeyError), s.write():
            s.execute("INSERT INTO rate VALUES ('a/b', 2)")
            raise KeyError
        assert s.execute("SELECT count(*) FROM rate").fetchone()[0] == 1
        with s.write():
            with pytest.raises(RuntimeError):
                with s.write():
                    pass


def test_quick_check_and_check_rows_on_a_good_store(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert s.quick_check()
        s.check_rows()


# --- review round 1 -------------------------------------------------------------------------

_BUSY_CODE = sqlite3.SQLITE_BUSY


class _BusyWalConnection(sqlite3.Connection):
    """Answers ``journal_mode = WAL`` with BUSY ``fail_times`` times, then really runs it."""

    fail_times = 0
    wal_calls = 0

    def execute(self, sql: str, *a: Any) -> sqlite3.Cursor:
        if sql.replace(" ", "").upper() == "PRAGMAJOURNAL_MODE=WAL":
            type(self).wal_calls += 1
            if type(self).wal_calls <= type(self).fail_times:
                err = sqlite3.OperationalError("database is locked")
                err.sqlite_errorcode = _BUSY_CODE  # type: ignore[attr-defined]
                raise err
        return super().execute(sql, *a)


def _busy_conn(path: Path, fail_times: int) -> sqlite3.Connection:
    class Conn(_BusyWalConnection):
        pass

    Conn.fail_times, Conn.wal_calls = fail_times, 0
    return sqlite3.connect(str(path), isolation_level=None, factory=Conn)


def test_ensure_wal_retries_busy_until_it_succeeds(tmp_path: Path) -> None:
    conn = _busy_conn(tmp_path / "x.db", store_mod.WAL_ATTEMPTS - 1)
    store_mod._ensure_wal(conn, 500)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500  # restored
    conn.close()


def test_ensure_wal_gives_up_with_db_locked(tmp_path: Path) -> None:
    conn = _busy_conn(tmp_path / "x.db", store_mod.WAL_ATTEMPTS)
    with pytest.raises(sqlite3.OperationalError) as e:
        store_mod._ensure_wal(conn, 500)
    assert store_mod.translate(e.value).reason == "db_locked"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500  # restored on failure too
    conn.close()


def test_wal_wait_stays_within_the_documented_timeout(state: StateDir) -> None:
    import time

    init(state)
    c = raw(state)
    c.execute("PRAGMA journal_mode = DELETE")
    c.close()
    holder = raw(state)
    holder.execute("BEGIN")
    holder.execute("SELECT count(*) FROM notes").fetchall()  # a SHARED lock in rollback mode
    try:
        start = time.monotonic()
        with pytest.raises(StoreError) as e:
            open_store(state, Mode.WRITE, busy_timeout_ms=400)
        elapsed = time.monotonic() - start
        assert e.value.reason == "db_locked"
        assert elapsed < 0.8  # the timeout once, not once per attempt
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_read_returns_the_version_of_the_inspected_snapshot(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(state)
    c = raw(state)
    c.execute("PRAGMA user_version = 1")
    c.close()
    real = store_mod._inspect

    def inspect_then_bump(conn: sqlite3.Connection) -> Any:
        snap = real(conn)
        other = raw(state)
        other.execute("PRAGMA user_version = 9")  # a writer moves on after the snapshot
        other.close()
        return snap

    monkeypatch.setattr(store_mod, "_inspect", inspect_then_bump)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert s.version == 1


# schema drift (R7): each variant of the version-1 DDL is not what this version creates

_DRIFT = {
    "generated-column": ("size INTEGER NOT NULL", "size INTEGER GENERATED ALWAYS AS (5) VIRTUAL"),
    "no-autoincrement": (" AUTOINCREMENT", ""),
    "on-delete": ("ON DELETE CASCADE", "ON DELETE NO ACTION"),
    "check": ("body TEXT NOT NULL,", "body TEXT NOT NULL CHECK (length(body) > 0),"),
    "unique-index": ("CREATE INDEX notes_repo_id", "CREATE UNIQUE INDEX notes_repo_id"),
    "not-strict": (") STRICT", ")"),
    "body-blob": ("body TEXT NOT NULL", "body BLOB NOT NULL"),
    "partial-index": ("ON rate (at)", "ON rate (at) WHERE at > 0"),
}


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("variant", list(_DRIFT))
def test_a_schema_that_differs_from_the_migration_is_db_corrupt(
    state: StateDir, variant: str, mode: Mode
) -> None:
    old, new = _DRIFT[variant]
    c = raw(state)
    changed = False
    for statement in store_mod.MIGRATIONS[1]:
        edited = statement.replace(old, new)
        changed = changed or edited != statement
        c.execute(edited)
    assert changed
    c.execute("PRAGMA user_version = 1")
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"


def test_whitespace_in_the_stored_ddl_does_not_matter(state: StateDir) -> None:
    c = raw(state)
    for statement in store_mod.MIGRATIONS[1]:
        c.execute(statement.replace("\n            ", "\n\t\t ").replace(", ", ",   "))
    c.execute("PRAGMA user_version = 1")
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert s.version == 1


@pytest.mark.parametrize("mode", list(Mode))
def test_an_older_version_is_compared_with_its_own_reference(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    init(state)
    monkeypatch.setitem(store_mod.MIGRATIONS, 2, ("CREATE TABLE extra (a INTEGER) STRICT",))
    if mode is Mode.READ:  # a version-1 store is still read as version 1
        with open_store(state, mode) as s:  # type: ignore[union-attr]
            assert s.version == 1
    c = raw(state)
    c.execute("CREATE INDEX stray ON rate (repo)")
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"


@pytest.mark.parametrize("seq", [10**18 - 1, 10**18, 2**63 - 1])
@pytest.mark.parametrize("mode", list(Mode))
def test_a_planted_sequence_at_the_top_of_the_id_range_is_db_corrupt(
    state: StateDir, seq: int, mode: Mode
) -> None:
    init(state)
    c = raw(state)
    insert(c, id=1)
    c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'notes'", (seq,))
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"


def test_check_rows_finds_labels_of_no_note(state: StateDir) -> None:
    init(state)
    c = raw(state)  # foreign keys are off here, so the cascade does not run
    insert(c, id=1)
    insert(c, id=2)
    c.execute("INSERT INTO note_labels VALUES (2, 'k', 'v')")
    c.execute("DELETE FROM notes WHERE id = 2")
    c.close()
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        with pytest.raises(StoreError) as e:
            s.check_rows()
        assert e.value.reason == "db_corrupt"
    with open_store(state, Mode.WRITE) as w:  # the cheap writer check passes: 2 <= seq  # type: ignore[union-attr]
        assert w is not None


@pytest.mark.parametrize("mode", [Mode.WRITE, Mode.INIT])
def test_a_label_above_the_sequence_is_db_corrupt_for_writers(state: StateDir, mode: Mode) -> None:
    init(state)
    c = raw(state)
    insert(c, id=1)
    c.execute("INSERT INTO note_labels VALUES (5, 'k', 'v')")  # no note, never issued
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, mode)
    assert e.value.reason == "db_corrupt"
    with open_store(state, Mode.READ) as r:  # type: ignore[union-attr]
        assert r is not None  # readers only find it through check_rows


def test_a_label_with_no_sequence_row_is_db_corrupt_for_writers(state: StateDir) -> None:
    init(state)
    c = raw(state)
    c.execute("INSERT INTO note_labels VALUES (1, 'k', 'v')")  # nothing was ever issued
    c.close()
    with pytest.raises(StoreError) as e:
        open_store(state, Mode.INIT)
    assert e.value.reason == "db_corrupt"


# execute and where (R6, R12, R13)


def test_where_must_be_a_plain_where_clause(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        assert list(s.notes("WHERE id = ?", (1,))) == []
        for bad in (
            "id = 1",
            "WHERE id = 1; DELETE FROM notes",
            "WHERE id = 1 -- x",
            "WHERE id = 1 /* x */",
            "WHERE repo = 'a'",
            'WHERE repo = "a"',
        ):
            with pytest.raises(ValueError) as e:
                list(s.notes(bad))  # type: ignore[arg-type]
            assert str(e.value) == "where must be a plain WHERE clause"


def test_a_python_side_error_in_our_own_sql_is_not_db_corrupt(state: StateDir) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        with pytest.raises(sqlite3.ProgrammingError):
            s.execute("SELECT ?", ())  # wrong number of bindings: a bug, exit 1


class _TooBigCursor:
    def fetchone(self) -> Any:
        raise sqlite3.DataError("string or blob too big: secret")

    fetchall = fetchone

    def fetchmany(self, size: int = 1) -> Any:
        return self.fetchone()


class _StubConn:
    def execute(self, sql: str, params: Any = ()) -> Any:
        return _TooBigCursor()


@pytest.mark.parametrize("fetch", ["fetchone", "fetchall", "fetchmany", "iterate"])
def test_a_fetch_failure_becomes_db_corrupt(fetch: str) -> None:
    s = store_mod.Store(_StubConn(), Mode.READ)  # type: ignore[arg-type]
    cursor = s.execute("SELECT 1")
    with pytest.raises(StoreError) as e:
        list(cursor) if fetch == "iterate" else getattr(cursor, fetch)()
    assert e.value.reason == "db_corrupt"
    assert "secret" not in str(e.value)


@pytest.mark.parametrize("value", ["\ud800 lone", 2**63, -(2**63) - 1])
def test_unbindable_parameters_raise_a_fixed_value_error(state: StateDir, value: Any) -> None:
    init(state)
    with open_store(state, Mode.READ) as s:  # type: ignore[union-attr]
        with pytest.raises(ValueError) as e:
            s.execute("SELECT ?", (value,))
        assert str(e.value) == "invalid parameter"
        assert e.value.__suppress_context__
