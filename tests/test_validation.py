"""``validate_store`` (interface.md, validate): the four checks, reasons, warnings, read-only."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from memoctl.statedir import STATE_PARTS, open_state_dir
from memoctl.store import APPLICATION_ID, Mode, Store, StoreError, open_store
from memoctl.validation import (
    CHECK_NAMES,
    ValidateResult,
    filesystem_type,
    validate_store,
)
from tests.fake_gateway import FakeGateway

SHA = "a" * 64


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "fake")


def state_path(gw: FakeGateway) -> Path:
    return gw.home().joinpath(*STATE_PARTS)


def make_state(gw: FakeGateway) -> Path:
    sd = open_state_dir(gw, create=True)
    sd.close()
    return state_path(gw)


def make_store(gw: FakeGateway) -> Path:
    sd = open_state_dir(gw, create=True)
    try:
        s = open_store(sd, Mode.INIT)
        assert s is not None
        s.close()
    finally:
        sd.close()
    return state_path(gw) / "state.db"


def raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db), isolation_level=None)


def insert(conn: sqlite3.Connection, **over: Any) -> None:
    base: dict[str, Any] = dict(
        id=1, repo="example.com/a/b", topic="t", kind="unknown", body="hello", sha=SHA,
        size=5, created=1000, expires=2000, superseded_by=None,
    )  # fmt: skip
    base.update(over)
    conn.execute(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires,"
        " superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        list(base.values()),
    )


def snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every entry under ``root``: mode, mtime and bytes (files). SQLite's own ``-shm`` and
    ``-wal`` are left out: opening a WAL database read-only may create them (interface.md,
    Database); :func:`test_wal_sidecars_left_by_a_read_open_are_private_and_empty` pins them."""
    out: dict[str, tuple[int, int, bytes]] = {}
    if not root.exists():
        return out
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(base) / name
            if name in ("state.db-shm", "state.db-wal"):
                continue
            st = p.lstat()
            data = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
            out[str(p.relative_to(root))] = (st.st_mode, st.st_mtime_ns, data)
    return out


def names(result: ValidateResult) -> list[str]:
    return [c.name for c in result.checks]


def reasons(result: ValidateResult) -> list[str | None]:
    return [c.reason for c in result.checks]


# --- success shapes -------------------------------------------------------------------------


def test_missing_state_dir_passes_with_messages(gw: FakeGateway) -> None:
    r = validate_store(gw)
    assert r.ok and r.exit_code == 0
    assert names(r) == list(CHECK_NAMES)
    messages = [c.message for c in r.checks][1:]
    assert messages == ["not created yet", "no database yet", "no database yet"]
    assert r.warnings == ()
    assert not state_path(gw).exists()  # nothing created, not even a parent
    assert not (gw.home() / ".local").exists()


def test_missing_database_passes(gw: FakeGateway) -> None:
    make_state(gw)
    r = validate_store(gw)
    assert r.ok
    assert r.checks[2].message == r.checks[3].message == "no database yet"
    assert not (state_path(gw) / "state.db").exists()


def test_zero_length_database_is_empty_and_untouched(gw: FakeGateway) -> None:
    sd = make_state(gw)
    (sd / "state.db").touch(mode=0o600)
    before = snapshot(sd)
    r = validate_store(gw)
    assert r.ok and r.checks[3].message == "no database yet"
    assert snapshot(sd) == before


def test_healthy_database_passes_and_is_read_only(gw: FakeGateway) -> None:
    db = make_store(gw)
    c = raw(db)
    insert(c)
    c.execute("INSERT INTO note_labels VALUES (1, 'k', 'v')")
    c.close()
    sd = state_path(gw)
    before = snapshot(sd)
    r = validate_store(gw)
    assert r.ok and r.exit_code == 0 and r.warnings == ()
    assert reasons(r) == [None] * 4
    assert snapshot(sd) == before


def test_wal_sidecars_left_by_a_read_open_are_private_and_empty(gw: FakeGateway) -> None:
    db = make_store(gw)
    assert validate_store(gw).ok
    for suffix in ("-shm", "-wal"):
        side = Path(str(db) + suffix)
        if side.exists():
            assert side.stat().st_mode & 0o077 == 0
    wal = Path(str(db) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_document_matches_schema(gw: FakeGateway) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (Path(__file__).parent.parent / "schemas" / "cli" / "validate.json").read_text()
    )
    make_store(gw)
    jsonschema.validate(validate_store(gw).document(), schema)
    gw2 = FakeGateway(gw.root / "other")
    sd = make_state(gw2)
    (sd / "state.db").write_bytes(b"x" * 5000)
    os.chmod(sd / "state.db", 0o600)
    doc = validate_store(gw2).document()
    jsonschema.validate(doc, schema)
    assert doc["ok"] is False
    json.dumps(doc)


# --- config ---------------------------------------------------------------------------------


def write_config(gw: FakeGateway, text: str) -> None:
    d = gw.home() / ".config" / "memoctl"
    d.mkdir(parents=True)
    d.chmod(0o700)
    f = d / "config.yaml"
    f.write_text(text)
    f.chmod(0o600)


def test_invalid_config_fails_first_and_ends_the_run(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  total: 0\n")
    r = validate_store(gw)
    assert not r.ok and r.exit_code == 2
    assert names(r) == ["config"]
    assert r.checks[0].reason == "config_invalid"
    assert "total" not in r.checks[0].message and str(gw.home()) not in r.checks[0].message
    assert r.warnings == ()


def test_config_garbage_is_reported_not_raised(gw: FakeGateway) -> None:
    write_config(gw, "\x00\xff: [unterminated")
    assert validate_store(gw).exit_code == 2


def test_bad_home_is_config_invalid(gw: FakeGateway) -> None:
    gw.home().rmdir()
    r = validate_store(gw)
    assert r.exit_code == 2 and names(r) == ["config"]


# --- state_dir ------------------------------------------------------------------------------


def test_state_dir_wrong_mode_is_unsafe_state(gw: FakeGateway) -> None:
    sd = make_state(gw)
    sd.chmod(0o755)
    r = validate_store(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir"]
    assert r.checks[1].reason == "unsafe_state"
    assert str(sd) not in r.checks[1].message


def test_state_dir_foreign_owner_is_unsafe_state(gw: FakeGateway) -> None:
    make_state(gw)
    gw.uid_value += 1
    r = validate_store(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"


def test_state_dir_symlink_is_unsafe_state(gw: FakeGateway) -> None:
    make_state(gw)
    sd = state_path(gw)
    target = gw.home() / "elsewhere"
    target.mkdir(mode=0o700)
    sd.rmdir()
    sd.symlink_to(target)
    r = validate_store(gw)
    assert r.exit_code == 20
    assert target.exists() and list(target.iterdir()) == []


def test_parent_that_is_a_file_is_unsafe_state(gw: FakeGateway) -> None:
    (gw.home() / ".local").write_text("x")
    r = validate_store(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert (gw.home() / ".local").read_text() == "x"


def test_unwritable_ancestor_fails_when_state_dir_missing(gw: FakeGateway) -> None:
    gw.denied_access.add(gw.home().resolve())
    r = validate_store(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert names(r) == ["config", "state_dir"]


def test_nearest_ancestor_is_used_not_the_home(gw: FakeGateway) -> None:
    (gw.home() / ".local").mkdir(mode=0o700)
    gw.denied_access.add((gw.home() / ".local").resolve())
    assert validate_store(gw).exit_code == 20
    gw.denied_access.clear()
    gw.denied_access.add(gw.home().resolve())  # home is not the nearest ancestor now
    assert validate_store(gw).ok


def test_unwritable_ancestor_ignored_when_state_dir_exists(gw: FakeGateway) -> None:
    make_state(gw)
    gw.denied_access.add(state_path(gw).parent.resolve())
    assert validate_store(gw).ok


@pytest.mark.skipif(os.getuid() == 0, reason="root passes os.access")
def test_real_access_check_with_unwritable_home(tmp_path: Path) -> None:
    from memoctl.gateway import RealGateway

    class Home(RealGateway):
        def home(self) -> Path:
            return tmp_path / "h"

    (tmp_path / "h").mkdir(mode=0o500)
    try:
        r = validate_store(Home())
    finally:
        (tmp_path / "h").chmod(0o700)
    assert r.exit_code == 20


# --- database -------------------------------------------------------------------------------


def corrupt_cases() -> list[str]:
    return [
        "foreign_app_id",
        "app_id_zero_with_objects",
        "garbage_bytes",
        "truncated",
        "trigger",
        "view",
        "extra_table",
        "bad_row_grammar",
        "bad_row_size",
        "invalid_utf8_body",
        "orphan_label",
        "huge_sequence",
        "user_version_zero",
        "quick_check",
    ]


@pytest.mark.parametrize("case", corrupt_cases())
def test_hostile_database_is_db_corrupt(gw: FakeGateway, case: str) -> None:
    db = make_store(gw)
    sd = state_path(gw)
    if case in ("garbage_bytes", "truncated"):
        data = db.read_bytes()
        db.write_bytes(os.urandom(8192) if case == "garbage_bytes" else data[:1500])
    else:
        c = raw(db)
        if case == "foreign_app_id":
            c.execute("PRAGMA application_id = 12345")
        elif case == "app_id_zero_with_objects":
            c.execute("PRAGMA application_id = 0")
        elif case == "trigger":
            c.execute("CREATE TRIGGER t AFTER INSERT ON rate BEGIN SELECT 1; END")
        elif case == "view":
            c.execute("CREATE VIEW v AS SELECT 1")
        elif case == "extra_table":
            c.execute("CREATE TABLE extra (a)")
        elif case == "bad_row_grammar":
            insert(c, repo="BAD REPO\n")
        elif case == "bad_row_size":
            insert(c, size=99)
        elif case == "invalid_utf8_body":
            insert(c)  # STRICT refuses a BLOB; invalid UTF-8 text is the way to plant one
            c.execute("UPDATE notes SET body = CAST(x'fffe' AS TEXT), size = 2")
        elif case == "orphan_label":
            c.execute("PRAGMA foreign_keys = OFF")
            c.execute("INSERT INTO note_labels VALUES (77, 'k', 'v')")
        elif case == "huge_sequence":
            insert(c)
            c.execute("UPDATE sqlite_sequence SET seq = 999999999999999999")
        elif case == "user_version_zero":
            c.execute("PRAGMA user_version = 0")
        elif case == "quick_check":
            pass
        c.close()
        if case == "quick_check":
            # damage an index page: break the file's consistency after the header
            c = raw(db)
            for i in range(30):
                insert(c, id=i + 1, topic=f"t{i}", body=f"b{i}", size=len(f"b{i}"))
            c.close()
            data = bytearray(db.read_bytes())
            for off in range(4096 * 2, len(data), 97):
                data[off] ^= 0xFF
            db.write_bytes(bytes(data))
    db.chmod(0o600)
    before = snapshot(sd)
    r = validate_store(gw)
    assert not r.ok and r.exit_code == 12
    assert names(r) == ["config", "state_dir", "database"]
    assert r.checks[2].reason in ("db_corrupt", "db_unreadable")
    if case != "quick_check":
        assert r.checks[2].reason == "db_corrupt"
    assert str(sd) not in json.dumps(r.document())
    assert snapshot(sd) == before


def test_database_with_unsafe_mode_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    db.chmod(0o644)
    r = validate_store(gw)
    assert r.exit_code == 20 and r.checks[2].reason == "unsafe_state"
    assert names(r) == ["config", "state_dir", "database"]


def test_database_symlink_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    other = db.parent / "other"
    db.rename(other)
    db.symlink_to(other)
    assert validate_store(gw).exit_code == 20


def test_unsafe_sidecar_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"")
    wal.chmod(0o666)
    assert validate_store(gw).exit_code == 20


def test_locked_database_is_reported_within_the_busy_timeout(gw: FakeGateway) -> None:
    db = make_store(gw)
    c = raw(db)
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        r = validate_store(gw)
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert r.exit_code == 12
    assert r.checks[2].reason == "db_locked"


def test_deadline_propagates_instead_of_a_check(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    for i in range(200):
        insert(c, id=i + 1, topic=f"t{i}", body=f"b{i}", size=len(f"b{i}"))
    c.close()

    def hook(store: Store) -> None:
        store.set_progress_handler(lambda: True)

    with pytest.raises(StoreError) as info:
        validate_store(gw, configure=hook)
    assert info.value.reason == "timeout"


def test_configure_hook_is_called_once(gw: FakeGateway) -> None:
    make_store(gw)
    calls: list[Store] = []
    assert validate_store(gw, configure=calls.append).ok
    assert len(calls) == 1


def test_old_sqlite_is_db_unreadable(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    make_store(gw)
    monkeypatch.setattr("memoctl.store.MIN_SQLITE", (99, 0, 0))
    r = validate_store(gw)
    assert r.exit_code == 12 and r.checks[2].reason == "db_unreadable"


def test_concurrent_reader_does_not_block_validate(gw: FakeGateway) -> None:
    db = make_store(gw)
    started = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        c = raw(db)
        c.execute("BEGIN")
        c.execute("SELECT count(*) FROM notes").fetchall()
        started.set()
        stop.wait(5)
        c.close()

    t = threading.Thread(target=reader)
    t.start()
    started.wait(5)
    try:
        assert validate_store(gw).ok
    finally:
        stop.set()
        t.join()


# --- schema ---------------------------------------------------------------------------------


def test_newer_schema_fails_schema_check_only(gw: FakeGateway) -> None:
    db = make_store(gw)
    c = raw(db)
    c.execute("PRAGMA user_version = 99")
    c.close()
    sd = state_path(gw)
    before = snapshot(sd)
    r = validate_store(gw)
    assert r.exit_code == 12
    assert names(r) == list(CHECK_NAMES)
    assert [c.ok for c in r.checks] == [True, True, True, False]
    assert r.checks[3].reason == "schema_newer"
    assert "99" not in r.checks[3].message
    assert snapshot(sd) == before


def test_older_known_version_is_not_migrated(gw: FakeGateway) -> None:
    # only version 1 exists in 0.1, so "older" is the current one: the version stays as found
    db = make_store(gw)
    r = validate_store(gw)
    assert r.ok
    c = raw(db)
    assert c.execute("PRAGMA user_version").fetchone() == (1,)
    assert c.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
    c.close()


# --- unsafe_filesystem ----------------------------------------------------------------------

MOUNTINFO = (
    "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "40 22 0:35 / {mnt} rw - {fs} C:\\134 rw\n"
)


@pytest.mark.parametrize(
    "fs", ["drvfs", "9p", "v9fs", "nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs"]
)
def test_unsafe_filesystem_warns_without_failing(gw: FakeGateway, fs: str) -> None:
    make_store(gw)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs=fs).encode()
    r = validate_store(gw)
    assert r.ok and r.exit_code == 0
    assert r.warnings == ("unsafe_filesystem",)
    assert r.document()["warnings"] == ["unsafe_filesystem"]


def test_warning_for_missing_state_dir_uses_nearest_parent(gw: FakeGateway) -> None:
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="9p").encode()
    r = validate_store(gw)
    assert r.ok and r.warnings == ("unsafe_filesystem",)


def test_warning_survives_a_failed_database_check(gw: FakeGateway) -> None:
    db = make_store(gw)
    db.write_bytes(b"not a database" * 100)
    db.chmod(0o600)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs4").encode()
    r = validate_store(gw)
    assert r.exit_code == 12 and r.warnings == ("unsafe_filesystem",)


def test_safe_filesystems_and_missing_mountinfo_do_not_warn(gw: FakeGateway) -> None:
    make_store(gw)
    assert validate_store(gw).warnings == ()  # no mountinfo
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="ext4").encode()
    assert validate_store(gw).warnings == ()
    gw.mountinfo = b""
    assert validate_store(gw).warnings == ()


def test_longest_mount_point_wins(gw: FakeGateway) -> None:
    make_store(gw)
    home = gw.home().resolve()
    # an unsafe mount above, a safe one nearer the state dir: safe wins
    text = f"1 0 0:1 / {home.parent} rw - 9p x rw\n2 1 0:2 / {home} rw - ext4 y rw\n"
    gw.mountinfo = text.encode()
    assert validate_store(gw).warnings == ()
    # the reverse
    text = f"1 0 0:1 / / rw - ext4 x rw\n2 1 0:2 / {home} rw - cifs y rw\n"
    gw.mountinfo = text.encode()
    assert validate_store(gw).warnings == ("unsafe_filesystem",)


def test_mount_point_prefix_is_by_component() -> None:
    info = b"1 0 0:1 / /mnt/data rw - 9p x rw\n"
    assert filesystem_type(info, Path("/mnt/data2/x")) is None
    assert filesystem_type(info, Path("/mnt/data/x")) == "9p"
    assert filesystem_type(info, Path("/mnt/data")) == "9p"


def test_mountinfo_escapes_optional_fields_and_garbage() -> None:
    info = (
        b"garbage line\n\n"
        b"1 0 0:1 / /mnt/my\\040disk rw shared:3 master:4 - cifs src rw\n"
        b"short - x\n"
        b"\xff\xfe broken \xff - nfs x rw\n"
    )
    assert filesystem_type(info, Path("/mnt/my disk/a")) == "cifs"
    assert filesystem_type(b"", Path("/a")) is None
    assert filesystem_type(b"x" * 100000, Path("/a")) is None


def test_covered_mount_last_entry_wins() -> None:
    info = b"1 0 0:1 / /m rw - ext4 a rw\n2 1 0:2 / /m rw - nfs b rw\n"
    assert filesystem_type(info, Path("/m/x")) == "nfs"


def test_real_gateway_mountinfo_is_readable_or_none() -> None:
    from memoctl.gateway import RealGateway

    data = RealGateway().read_mountinfo(1 << 20)
    assert data is None or b" - " in data
    assert len(RealGateway().read_mountinfo(10) or b"") <= 10


# --- result ---------------------------------------------------------------------------------


def test_result_ordering_is_deterministic(gw: FakeGateway) -> None:
    make_store(gw)
    docs = [json.dumps(validate_store(gw).document()) for _ in range(3)]
    assert len(set(docs)) == 1
    assert list(json.loads(docs[0])) == ["ok", "checks", "warnings"]
    assert list(json.loads(docs[0])["checks"][0]) == ["name", "ok", "reason", "message"]
