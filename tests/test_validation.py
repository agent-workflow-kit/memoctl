"""``validate_store`` (interface.md, validate): the four checks, reasons, warnings, read-only."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import jsonschema
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

# todo(#60): human-form goldens for validate (the rendered text) once render.py lands.
GOLDEN_DIR = Path(__file__).parent / "golden" / "validate"
SCHEMA = json.loads(
    (Path(__file__).parent.parent / "schemas" / "cli" / "validate.json").read_text()
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
VALIDATOR.check_schema(SCHEMA)


def checked(gateway: Any, **kw: Any) -> ValidateResult:
    """``validate_store`` plus a schema check of its ``--json`` document."""
    result = validate_store(gateway, **kw)
    VALIDATOR.validate(result.document())
    return result


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
    r = checked(gw)
    assert r.ok and r.exit_code == 0
    assert names(r) == list(CHECK_NAMES)
    messages = [c.message for c in r.checks][1:]
    assert messages == ["not created yet", "no database yet", "no database yet"]
    assert r.warnings == ()
    assert not state_path(gw).exists()  # nothing created, not even a parent
    assert not (gw.home() / ".local").exists()


def test_missing_database_passes(gw: FakeGateway) -> None:
    make_state(gw)
    r = checked(gw)
    assert r.ok
    assert r.checks[2].message == r.checks[3].message == "no database yet"
    assert not (state_path(gw) / "state.db").exists()


def test_zero_length_database_is_empty_and_untouched(gw: FakeGateway) -> None:
    sd = make_state(gw)
    (sd / "state.db").touch(mode=0o600)
    before = snapshot(sd)
    r = checked(gw)
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
    r = checked(gw)
    assert r.ok and r.exit_code == 0 and r.warnings == ()
    assert reasons(r) == [None] * 4
    assert snapshot(sd) == before


def test_wal_sidecars_left_by_a_read_open_are_private_and_empty(gw: FakeGateway) -> None:
    db = make_store(gw)
    assert checked(gw).ok
    for suffix in ("-shm", "-wal"):
        side = Path(str(db) + suffix)
        if side.exists():
            assert side.stat().st_mode & 0o077 == 0
    wal = Path(str(db) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


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
    r = checked(gw)
    assert not r.ok and r.exit_code == 2
    assert names(r) == ["config"]
    assert r.checks[0].reason == "config_invalid"
    assert "total" not in r.checks[0].message and str(gw.home()) not in r.checks[0].message
    assert r.warnings == ()


def test_config_garbage_is_reported_not_raised(gw: FakeGateway) -> None:
    write_config(gw, "\x00\xff: [unterminated")
    assert checked(gw).exit_code == 2


def test_bad_home_is_config_invalid(gw: FakeGateway) -> None:
    gw.home().rmdir()
    r = checked(gw)
    assert r.exit_code == 2 and names(r) == ["config"]


# --- state_dir ------------------------------------------------------------------------------


def test_state_dir_wrong_mode_is_unsafe_state(gw: FakeGateway) -> None:
    sd = make_state(gw)
    sd.chmod(0o755)
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir"]
    assert r.checks[1].reason == "unsafe_state"
    assert str(sd) not in r.checks[1].message


def test_state_dir_foreign_owner_is_unsafe_state(gw: FakeGateway) -> None:
    make_state(gw)
    gw.uid_value += 1
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"


def test_state_dir_symlink_is_unsafe_state(gw: FakeGateway) -> None:
    make_state(gw)
    sd = state_path(gw)
    target = gw.home() / "elsewhere"
    target.mkdir(mode=0o700)
    sd.rmdir()
    sd.symlink_to(target)
    r = checked(gw)
    assert r.exit_code == 20
    assert target.exists() and list(target.iterdir()) == []


def test_parent_that_is_a_file_is_unsafe_state(gw: FakeGateway) -> None:
    (gw.home() / ".local").write_text("x")
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert (gw.home() / ".local").read_text() == "x"


def test_unwritable_ancestor_fails_when_state_dir_missing(gw: FakeGateway) -> None:
    gw.denied_access.add(gw.home().resolve())
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert names(r) == ["config", "state_dir"]


def test_nearest_ancestor_is_used_not_the_home(gw: FakeGateway) -> None:
    (gw.home() / ".local").mkdir(mode=0o700)
    gw.denied_access.add((gw.home() / ".local").resolve())
    assert checked(gw).exit_code == 20
    gw.denied_access.clear()
    gw.denied_access.add(gw.home().resolve())  # home is not the nearest ancestor now
    assert checked(gw).ok


def test_unwritable_ancestor_ignored_when_state_dir_exists(gw: FakeGateway) -> None:
    make_state(gw)
    gw.denied_access.add(state_path(gw).parent.resolve())
    assert checked(gw).ok


@pytest.mark.skipif(os.getuid() == 0, reason="root passes os.access")
def test_real_access_check_with_unwritable_home(tmp_path: Path) -> None:
    from memoctl.gateway import RealGateway

    class Home(RealGateway):
        def home(self) -> Path:
            return tmp_path / "h"

    (tmp_path / "h").mkdir(mode=0o500)
    try:
        r = checked(Home())
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
        c.close()
    db.chmod(0o600)
    before = snapshot(sd)
    r = checked(gw)
    assert not r.ok and r.exit_code == 12
    assert names(r) == ["config", "state_dir", "database"]
    assert r.checks[2].reason == "db_corrupt"
    assert str(sd) not in json.dumps(r.document())
    assert snapshot(sd) == before


def test_failing_quick_check_is_db_corrupt(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    assert checked(gw).ok  # healthy: only the stub below changes the outcome
    monkeypatch.setattr(Store, "quick_check", lambda self: False)
    r = checked(gw)
    assert r.exit_code == 12 and names(r) == ["config", "state_dir", "database"]
    assert r.checks[2].reason == "db_corrupt"


def test_database_with_unsafe_mode_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    db.chmod(0o644)
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[2].reason == "unsafe_state"
    assert names(r) == ["config", "state_dir", "database"]


def test_database_symlink_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    other = db.parent / "other"
    db.rename(other)
    db.symlink_to(other)
    assert checked(gw).exit_code == 20


def test_unsafe_sidecar_is_unsafe_state(gw: FakeGateway) -> None:
    db = make_store(gw)
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"")
    wal.chmod(0o666)
    assert checked(gw).exit_code == 20


def test_locked_database_is_reported_within_the_busy_timeout(gw: FakeGateway) -> None:
    db = make_store(gw)
    c = raw(db)
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        r = checked(gw)
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
        checked(gw, configure=hook)
    assert info.value.reason == "timeout"


def test_configure_hook_is_called_once(gw: FakeGateway) -> None:
    make_store(gw)
    calls: list[Store] = []
    assert checked(gw, configure=calls.append).ok
    assert len(calls) == 1


def test_old_sqlite_is_db_unreadable(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    make_store(gw)
    monkeypatch.setattr("memoctl.store.MIN_SQLITE", (99, 0, 0))
    r = checked(gw)
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
        assert checked(gw).ok
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
    r = checked(gw)
    assert r.exit_code == 12
    assert names(r) == list(CHECK_NAMES)
    assert [c.ok for c in r.checks] == [True, True, True, False]
    assert r.checks[3].reason == "schema_newer"
    assert "99" not in r.checks[3].message
    assert snapshot(sd) == before


def test_older_known_version_is_not_migrated(gw: FakeGateway) -> None:
    # only version 1 exists in 0.1, so "older" is the current one: the version stays as found
    db = make_store(gw)
    r = checked(gw)
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
    r = checked(gw)
    assert r.ok and r.exit_code == 0
    assert r.warnings == ("unsafe_filesystem",)
    assert r.document()["warnings"] == ["unsafe_filesystem"]


def test_warning_for_missing_state_dir_uses_nearest_parent(gw: FakeGateway) -> None:
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="9p").encode()
    r = checked(gw)
    assert r.ok and r.warnings == ("unsafe_filesystem",)


def test_warning_survives_a_failed_database_check(gw: FakeGateway) -> None:
    db = make_store(gw)
    db.write_bytes(b"not a database" * 100)
    db.chmod(0o600)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs4").encode()
    r = checked(gw)
    assert r.exit_code == 12 and r.warnings == ("unsafe_filesystem",)


def test_safe_filesystems_and_missing_mountinfo_do_not_warn(gw: FakeGateway) -> None:
    make_store(gw)
    assert checked(gw).warnings == ()  # no mountinfo
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="ext4").encode()
    assert checked(gw).warnings == ()
    gw.mountinfo = b""
    assert checked(gw).warnings == ()


def test_longest_mount_point_wins(gw: FakeGateway) -> None:
    make_store(gw)
    home = gw.home().resolve()
    # an unsafe mount above, a safe one nearer the state dir: safe wins
    text = f"1 0 0:1 / {home.parent} rw - 9p x rw\n2 1 0:2 / {home} rw - ext4 y rw\n"
    gw.mountinfo = text.encode()
    assert checked(gw).warnings == ()
    # the reverse
    text = f"1 0 0:1 / / rw - ext4 x rw\n2 1 0:2 / {home} rw - cifs y rw\n"
    gw.mountinfo = text.encode()
    assert checked(gw).warnings == ("unsafe_filesystem",)


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
    docs = [json.dumps(checked(gw).document()) for _ in range(3)]
    assert len(set(docs)) == 1
    assert list(json.loads(docs[0])) == ["ok", "checks", "warnings"]
    assert list(json.loads(docs[0])["checks"][0]) == ["name", "ok", "reason", "message"]


# --- state_dir failures keep the warning, OSError paths, dangling parents -------------------


def test_unsafe_state_dir_still_warns_on_unsafe_filesystem(gw: FakeGateway) -> None:
    sd = make_state(gw)
    sd.chmod(0o755)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert r.warnings == ("unsafe_filesystem",)


def test_config_failure_has_no_warning(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  total: 0\n")
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()
    assert checked(gw).warnings == ()


def test_state_dir_open_error_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(gw)

    def boom(path: Path, *, follow: bool) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(gw, "open_dir", boom)
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir"]
    assert r.checks[1].reason == "unsafe_state"


def test_state_dir_open_error_after_the_check_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    real_open = gw.open_dir
    calls: list[int] = []

    def flaky(path: Path, *, follow: bool) -> Any:
        calls.append(1)
        if len(calls) > 1:  # the state_dir check opened it; the database check cannot
            raise OSError(errno.EIO, "io")
        return real_open(path, follow=follow)

    monkeypatch.setattr(gw, "open_dir", flaky)
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir", "database"]
    assert r.checks[2].reason == "unsafe_state"


def test_state_dir_open_error_still_warns_on_unsafe_filesystem(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(gw)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()

    def boom(path: Path, *, follow: bool) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(gw, "open_dir", boom)
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert r.warnings == ("unsafe_filesystem",)


def test_missing_state_dir_with_unsafe_parent_warns_from_the_nearest_existing_ancestor(
    gw: FakeGateway,
) -> None:
    # open_state_dir refuses (".local" is a file), the state dir does not exist, so the warning
    # is judged on the nearest existing ancestor (_warning_base fallback)
    (gw.home() / ".local").write_text("x")
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()
    r = checked(gw)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert r.warnings == ("unsafe_filesystem",)


@pytest.mark.parametrize("which", ["access", "stat_path"])
def test_os_error_while_checking_a_missing_state_dir_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    real = getattr(gw, which)

    def boom(*args: Any, **kw: Any) -> Any:
        if which == "stat_path" and args[0] != gw.home().joinpath(".local"):
            return real(*args, **kw)
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(gw, which, boom)
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir"]
    assert r.checks[1].reason == "unsafe_state"
    assert r.checks[1].message == "the state directory cannot be created"


def test_database_lstat_error_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memoctl import gateway as gateway_module

    make_store(gw)
    real_lstat = gateway_module._RealDirHandle.lstat

    def lstat(self: Any, name: str) -> Any:
        if name == "state.db":
            raise OSError(errno.EIO, "io")
        return real_lstat(self, name)

    monkeypatch.setattr(gateway_module._RealDirHandle, "lstat", lstat)
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir", "database"]
    assert r.checks[2].reason == "unsafe_state"
    assert r.checks[2].message == "the database file is not safe to use"


class _RealHome:
    @staticmethod
    def make(tmp_path: Path) -> Any:
        from memoctl.gateway import RealGateway

        class Home(RealGateway):
            def home(self) -> Path:
                return tmp_path / "h"

        (tmp_path / "h").mkdir(mode=0o700)
        return Home()


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_unsearchable_parent_of_existing_state_dir_is_unsafe_state(tmp_path: Path) -> None:
    real = _RealHome.make(tmp_path)
    sd = open_state_dir(real, create=True)
    sd.close()
    local = tmp_path / "h" / ".local"
    local.chmod(0)
    try:
        r = checked(real)
    finally:
        local.chmod(0o700)
    assert r.exit_code == 20 and r.checks[1].reason == "unsafe_state"
    assert names(r) == ["config", "state_dir"]


@pytest.mark.parametrize("level", [".local", ".local/state"])
def test_dangling_symlink_parent_is_unsafe_state(gw: FakeGateway, level: str) -> None:
    home = gw.home()
    if level == ".local/state":
        (home / ".local").mkdir(mode=0o700)
    (home / level).symlink_to(home / "nowhere")
    r = checked(gw)
    assert r.exit_code == 20 and names(r) == ["config", "state_dir"]
    assert r.checks[1].reason == "unsafe_state"
    assert not (home / "nowhere").exists()


def test_dangling_symlink_parent_matches_add_refusal(gw: FakeGateway) -> None:
    from memoctl.statedir import UnsafeStateError

    (gw.home() / ".local").symlink_to(gw.home() / "nowhere")
    with pytest.raises(UnsafeStateError):
        open_state_dir(gw, create=True)
    assert checked(gw).exit_code == 20


def test_symlink_parent_to_a_directory_is_fine(gw: FakeGateway) -> None:
    (gw.home() / "real").mkdir(mode=0o700)
    (gw.home() / ".local").symlink_to(gw.home() / "real")
    assert checked(gw).ok


# --- mountinfo cap --------------------------------------------------------------------------


def test_cut_last_mountinfo_line_is_ignored(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    home = gw.home().resolve()
    head = f"1 0 0:1 / / rw - ext4 x rw\n2 1 0:2 / {home} rw - nfs".encode()
    gw.mountinfo = head + b"d x rw\n"  # the last field is nfsd, a type that is not unsafe
    monkeypatch.setattr("memoctl.validation.MOUNTINFO_LIMIT", len(head))
    assert len(gw.read_mountinfo(len(head)) or b"") == len(head)  # exactly the cap
    assert checked(gw).warnings == ()
    # only an unterminated tail is dropped: a complete last line that ends exactly at the cap
    # (its LF is the last byte) is whole and counts
    full = f"1 0 0:1 / / rw - ext4 x rw\n2 1 0:2 / {home} rw - nfs x rw\n".encode()
    gw.mountinfo = full
    monkeypatch.setattr("memoctl.validation.MOUNTINFO_LIMIT", len(full))
    assert checked(gw).warnings == ("unsafe_filesystem",)
    # one byte less and the LF is cut off: the last line is unterminated and ignored
    monkeypatch.setattr("memoctl.validation.MOUNTINFO_LIMIT", len(full) - 1)
    assert checked(gw).warnings == ()


# --- real gateway helpers -------------------------------------------------------------------


def test_real_access_semantics(tmp_path: Path) -> None:
    from memoctl.gateway import RealGateway

    real = RealGateway()
    assert real.access(tmp_path, write=True, search=True)
    assert not real.access(tmp_path / "missing", write=False, search=False)
    assert not real.access(tmp_path / "a\0b", write=True, search=True)
    if os.getuid() != 0:
        ro = tmp_path / "ro"
        ro.mkdir(mode=0o500)
        try:
            assert real.access(ro, write=False, search=True)
            assert not real.access(ro, write=True, search=True)
        finally:
            ro.chmod(0o700)


def test_fake_access_is_not_more_permissive(tmp_path: Path) -> None:
    fake = FakeGateway(tmp_path / "f")
    assert not fake.access(fake.root / "missing", write=False, search=False)
    from memoctl.gateway import GatewayError

    with pytest.raises(GatewayError):  # stricter than real (False), never more permissive
        fake.access(fake.root / "a\0b", write=True, search=True)


def test_real_resolve_path(tmp_path: Path) -> None:
    from memoctl.gateway import RealGateway

    (tmp_path / "d").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "d")
    real = RealGateway()
    base = tmp_path.resolve()
    assert real.resolve_path(tmp_path / "link") == base / "d"
    assert real.resolve_path(tmp_path / "link" / "no" / "such") == base / "d" / "no" / "such"
    fake = FakeGateway(tmp_path / "f")
    (fake.root / "d").mkdir()
    (fake.root / "link").symlink_to(fake.root / "d")
    assert fake.resolve_path(fake.root / "link" / "no") == fake.root / "d" / "no"


def test_real_read_mountinfo_limit_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from memoctl import gateway as gateway_module
    from memoctl.gateway import RealGateway

    real = RealGateway()
    if real.read_mountinfo(1) is not None:
        assert real.read_mountinfo(0) == b""
        assert len(real.read_mountinfo(7) or b"") <= 7
    orig = os.open

    def no_proc(path: Any, *a: Any, **k: Any) -> int:
        if str(path) == "/proc/self/mountinfo":
            raise FileNotFoundError(path)
        return orig(path, *a, **k)

    monkeypatch.setattr(gateway_module.os, "open", no_proc)
    assert real.read_mountinfo(100) is None
    fake = FakeGateway(Path("/tmp") / "unused-fake-never-created")
    assert fake.read_mountinfo(0) is None  # no mountinfo served


# --- goldens (--json documents) -------------------------------------------------------------


def _g_clean(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)


def _g_config_invalid(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    write_config(gw, "limits:\n  total: 0\n")


def _g_unsafe_state(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_state(gw).chmod(0o755)


def _g_db_unreadable(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)
    mp.setattr("memoctl.store.MIN_SQLITE", (99, 0, 0))


def _g_db_corrupt(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    db = make_store(gw)
    db.write_bytes(os.urandom(8192))
    db.chmod(0o600)


def _g_schema_newer(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA user_version = 99")
    c.close()


def _g_unsafe_filesystem(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()


GOLDEN_CASES = {
    "clean": _g_clean,
    "config_invalid": _g_config_invalid,
    "unsafe_state": _g_unsafe_state,
    "db_unreadable": _g_db_unreadable,
    "db_corrupt": _g_db_corrupt,
    "schema_newer": _g_schema_newer,
    "unsafe_filesystem": _g_unsafe_filesystem,
}


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_golden_json_document(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    GOLDEN_CASES[case](gw, monkeypatch)
    doc = checked(gw).document()
    golden = json.loads((GOLDEN_DIR / f"{case}.json").read_text())
    VALIDATOR.validate(golden)
    assert doc == golden


def test_golden_json_db_locked(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        doc = checked(gw).document()
    finally:
        c.execute("ROLLBACK")
        c.close()
    golden = json.loads((GOLDEN_DIR / "db_locked.json").read_text())
    VALIDATOR.validate(golden)
    assert doc == golden


def test_golden_files_are_all_used() -> None:
    expected = {f"{c}.json" for c in GOLDEN_CASES} | {"db_locked.json"}
    assert {p.name for p in GOLDEN_DIR.glob("*.json")} == expected
