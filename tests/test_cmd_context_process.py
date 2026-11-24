"""``context`` through the real entry point in a child process.

What a fake gateway cannot show: closed descriptors, EPIPE, a pipe nobody reads, a terminal on
stdin, hostile argv bytes and environment, and the real timer. Children get a private home (the
tool reads the password database, not ``HOME``; ``tests/private_home.py``), a minimal environment
and a timeout; a child that hangs is killed through its own ``Popen`` handle, never by name.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pty
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from memoctl.gateway import WRITE_WAIT_SECONDS
from tests import private_home
from tests.child_env import child_path
from tests.fake_gateway import FakeGateway
from tests.test_cmd_context import SHA, Store, assert_block
from tests.test_validation import make_store, raw, state_path, write_config

REPO = "example.com/a/b"
CREATED = 1700000000  # 2023-11-14 UTC
EXPIRES = 4000000000  # year 2096
HEAD1 = f"memoctl notes for {REPO} (1 of 1 shown)\n"
BLOCK1 = HEAD1 + "--- note 1 | topic: t | kind: implement | 2023-11-14 ---\n" + "| hello\n"
TIMEOUT_LINE = b"failed (timeout): deadline passed\n"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "fake")


def seeded(gw: FakeGateway, *, keep: bool = False, **over: Any) -> Store:
    """A store with one note (a connection left open would hold locks: closed unless ``keep``)."""
    s = Store(gw)
    s.add(created=CREATED, expires=EXPIRES, **over)
    if not keep:
        s.conn.close()
    return s


def env_for(home: Path) -> dict[str, str]:
    return {
        "PATH": child_path(home.resolve().parents[1], tools=["sh"]),  # outside the fake root
        "LC_ALL": "C",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
    }


def child(
    home: Path,
    args: Sequence[bytes | str],
    *,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.PIPE,
    prefix: str = "",
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[bytes]:
    cmd: list[bytes | str] = [*private_home.argv(home, "context"), *args]
    if prefix:
        cmd = ["sh", "-c", f'exec "$0" "$@" {prefix}', *cmd]  # type: ignore[list-item]
    proc = subprocess.Popen(
        cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        env=env if env is not None else env_for(home),
        cwd=home,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def test_block_exact_bytes_and_exit_0(gw: FakeGateway) -> None:
    seeded(gw)
    r = child(gw.home(), ["--repo", REPO])
    assert (r.returncode, r.stdout, r.stderr) == (0, BLOCK1.encode(), b"")


def test_ci_like_environment_is_enough(gw: FakeGateway) -> None:
    seeded(gw)
    r = child(
        gw.home(), [f"--repo={REPO}"], env={"LC_ALL": "C", "PATH": child_path(gw.root.parent)}
    )
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())


@pytest.mark.parametrize("flags", [[], ["--fence"], ["--json"], ["--fence", "--json"]])
def test_stdout_is_bytes_only_the_block_even_for_non_ascii(
    gw: FakeGateway, flags: list[str]
) -> None:
    seeded(gw, body="café \U0001f600 ☃")
    r = child(gw.home(), ["--repo", REPO, *flags])
    assert r.returncode == 0 and r.stderr == b""
    if "--json" in flags:
        assert r.stdout.isascii()
        text = json.loads(r.stdout)["text"]
    else:
        text = r.stdout.decode("utf-8")  # strict: valid UTF-8, no replacement characters
        assert "�" not in text
        assert "café \U0001f600 ☃\n" in text
    assert "café \U0001f600 ☃" in text


def test_exit_10_prints_nothing_anywhere(gw: FakeGateway) -> None:
    r = child(gw.home(), ["--repo", REPO])
    assert (r.returncode, r.stdout, r.stderr) == (10, b"", b"")
    r = child(gw.home(), [])
    assert (r.returncode, r.stdout, r.stderr) == (10, b"", b"")
    r = child(gw.home(), ["--json"])
    assert r.returncode == 10 and json.loads(r.stdout)["eligible"] == 0 and r.stderr == b""


# --- hostile argv -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        b"example.com/\xff\xfe",
        b"\xed\xa0\x80",
        b"ex\xc3\x28.com/a",
        b"example.com/a\xf0\x28\x8c\x28",
    ],
    ids=["ff-fe", "encoded-surrogate", "bad-continuation", "bad-4-byte"],
)
@pytest.mark.parametrize("form", ["spaced", "equals"])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_non_utf8_repo_is_exit_2_and_never_echoed(
    gw: FakeGateway, repo: bytes, form: str, flags: list[str]
) -> None:
    seeded(gw)
    args: list[bytes | str] = ["--repo", repo] if form == "spaced" else [b"--repo=" + repo]
    r = child(gw.home(), [*args, *flags])
    assert r.returncode == 2
    assert b"\xff" not in r.stdout + r.stderr and b"example" not in r.stdout + r.stderr
    assert r.stderr == b"usage: invalid repo\n"
    if flags:
        assert json.loads(r.stdout)["error"]["details"] == {"field": "repo"}
    else:
        assert r.stdout == b""


def test_non_utf8_label_is_exit_2(gw: FakeGateway) -> None:
    seeded(gw)
    r = child(gw.home(), ["--repo", REPO, b"--label=k=\xff"])
    assert (r.returncode, r.stdout, r.stderr) == (2, b"", b"usage: invalid label\n")


def test_non_utf8_over_255_repo_is_exit_10(gw: FakeGateway) -> None:
    seeded(gw)
    r = child(gw.home(), [b"--repo=" + b"\xff" * 300, "--json"])
    assert r.returncode == 10 and json.loads(r.stdout)["eligible"] == 0 and r.stderr == b""


def test_huge_and_injection_shaped_argv(gw: FakeGateway) -> None:
    seeded(gw)
    for repo in ("x" * 100_000, "'; DROP TABLE notes;--", "a/b\n--- note 1 ---"):
        r = child(gw.home(), ["--repo", repo])
        assert r.returncode in (2, 10) and r.stdout == b""
        assert b"DROP" not in r.stderr and b"xxxx" not in r.stderr
    r = child(gw.home(), ["--repo", REPO, "--label", "k=" + "v" * 100_000])
    assert (r.returncode, r.stdout) == (2, b"")
    many = [a for i in range(2000) for a in ("--label", f"k{i}=v")]
    r = child(gw.home(), ["--repo", REPO, *many])  # 2000 session labels: the unlabelled note
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())
    store = Store(gw)
    store.add(created=CREATED, expires=EXPIRES, labels=[("k5", "v"), ("zz", "1")], topic="u")
    store.conn.close()
    r = child(gw.home(), ["--repo", REPO, *many])
    assert r.returncode == 0 and b"zz=1" not in r.stdout  # every label of a note must be held
    r = child(gw.home(), ["--repo", REPO, *many, "--label", "zz=1"])
    assert r.returncode == 0 and b"k5=v, zz=1" in r.stdout


# --- environment hostility ----------------------------------------------------------------------


def test_home_and_xdg_variables_are_never_read(gw: FakeGateway, tmp_path: Path) -> None:
    seeded(gw)
    evil = tmp_path / "evil"
    (evil / ".config" / "memoctl").mkdir(parents=True)
    (evil / ".config" / "memoctl" / "config.yaml").write_text("context: {max_notes: 1}\nbad: [\n")
    env = {
        "PATH": child_path(tmp_path),
        "LC_ALL": "C",
        "HOME": str(evil),
        "XDG_CONFIG_HOME": str(evil / ".config"),
        "XDG_STATE_HOME": str(evil / "state"),
        "CSESSION_KIND": "unknown",
        "MEMOCTL_STATE": str(evil),
    }
    r = child(gw.home(), ["--repo", REPO], env=env)
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())
    assert not (evil / "state").exists()
    env["HOME"] = "/nonexistent"
    r = child(gw.home(), ["--repo", REPO], env=env)
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())


def test_invalid_config_in_the_real_home_is_exit_2(gw: FakeGateway) -> None:
    seeded(gw)
    write_config(gw, "context: {max_notes: 0}\n")
    r = child(gw.home(), ["--repo", REPO, "--json"])
    assert r.returncode == 2 and r.stderr == b"config_invalid: invalid configuration\n"
    assert json.loads(r.stdout)["error"]["code"] == "config_invalid"


# --- standard descriptors -----------------------------------------------------------------------


_CLOSED_SETS = [
    ("", True),
    ("<&-", True),
    (">&-", False),
    ("2>&-", True),
    ("<&- >&-", False),
    ("<&- 2>&-", True),
    (">&- 2>&-", False),
    ("<&- >&- 2>&-", False),
]


def _db_files(gw: FakeGateway) -> dict[str, str | None]:
    """sha256 of state.db, -wal and -shm (None when absent)."""
    base = state_path(gw) / "state.db"
    out: dict[str, str | None] = {}
    for suffix in ("", "-wal", "-shm"):
        f = base.with_name(base.name + suffix)
        out[suffix or "db"] = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
    return out


@pytest.mark.parametrize(("prefix", "expect_out"), _CLOSED_SETS)
def test_closed_standard_descriptors_keep_the_exit_code_and_the_database_files(
    gw: FakeGateway, prefix: str, expect_out: bool
) -> None:
    # A live writer connection keeps the -wal (with the note in it) and the -shm in place.
    store = seeded(gw, keep=True)
    try:
        before = _db_files(gw)
        assert before["-wal"] is not None and before["-shm"] is not None
        r = child(gw.home(), ["--repo", REPO], prefix=prefix)
        assert r.returncode == 0
        if expect_out:
            assert r.stdout == BLOCK1.encode()
        else:
            assert r.stdout == b""
        r = child(gw.home(), ["--repo", "example.com/none"], prefix=prefix)
        assert r.returncode == 10 and r.stdout == b""
        r = child(gw.home(), ["--repo", "Bad"], prefix=prefix)
        assert r.returncode == 2 and r.stdout == b""
        # state.db and -wal are compared by content hash and must be unchanged. The -shm is
        # shared-memory bookkeeping (read marks) that any reader attached to a live WAL
        # database updates, and interface.md lets it appear, so only its presence is checked.
        after = _db_files(gw)
        assert (after["db"], after["-wal"]) == (before["db"], before["-wal"])
        assert after["-shm"] is not None
    finally:
        store.conn.close()


def test_a_closed_stdout_never_sends_the_block_into_the_database_file(gw: FakeGateway) -> None:
    """With fd 1 closed, the first file the tool opens would take descriptor 1; the block would
    then be written into it. The standard descriptors are refilled first (interface.md)."""
    seeded(gw)
    db = gw.home() / ".local" / "state" / "memoctl" / "state.db"
    before = db.read_bytes()
    r = child(gw.home(), ["--repo", REPO, "--json"], prefix=">&-")
    assert r.returncode == 0
    assert db.read_bytes() == before
    assert not list(gw.home().rglob("*.json"))


def test_a_terminal_on_stdin_is_never_read_or_prompted(gw: FakeGateway) -> None:
    seeded(gw)
    master, slave = pty.openpty()
    try:
        r = child(gw.home(), ["--repo", REPO], stdin=slave, timeout=30)
    finally:
        os.close(slave)
        os.close(master)
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())


def test_stdin_at_end_of_file_or_an_endless_source_is_never_read(gw: FakeGateway) -> None:
    seeded(gw)
    with open("/dev/zero", "rb") as zero:
        r = child(gw.home(), ["--repo", REPO], stdin=zero, timeout=30)
    assert (r.returncode, r.stdout) == (0, BLOCK1.encode())


# --- broken and full pipes ----------------------------------------------------------------------


def test_broken_pipe_keeps_the_exit_code_for_every_form(gw: FakeGateway) -> None:
    seeded(gw)
    for args, code in (
        (["--repo", REPO], 0),
        (["--repo", REPO, "--fence"], 0),
        (["--repo", REPO, "--json"], 0),
        (["--repo", "example.com/none"], 10),
        (["--repo", "example.com/none", "--json"], 10),
        (["--repo", "Bad", "--json"], 2),
    ):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            r = child(gw.home(), args, stdout=write_end)
        finally:
            os.close(write_end)
        assert r.returncode == code, args
        assert b"Traceback" not in r.stderr and b"BrokenPipe" not in r.stderr


def test_a_reader_that_starts_late_still_gets_the_complete_validated_block(
    gw: FakeGateway,
) -> None:
    """The deadline is disarmed before the first byte is written and a stalled reader is waited
    on without limit: a full pipe costs time, never exit 12 and never a cut block."""
    write_config(gw, "context:\n  max_bytes: 65536\n  max_chars: 9000\n  max_notes: 100\n")
    store = seeded(gw, keep=True, body="x" * 100, topic="t0")
    for i in range(1, 80):
        store.add(created=CREATED, expires=EXPIRES, body="y" * 100, topic=f"t{i}")
    store.conn.close()
    read_end, write_end = os.pipe()
    flags = fcntl.fcntl(write_end, fcntl.F_GETFL)
    fcntl.fcntl(write_end, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    filled = 0
    try:
        while True:
            filled += os.write(write_end, b"z" * 4096)
    except OSError as exc:
        assert exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    fcntl.fcntl(write_end, fcntl.F_SETFL, flags)  # the child blocks like a normal pipe
    cmd = [*private_home.argv(gw.home(), "context"), "--repo", REPO]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=write_end,
        stderr=subprocess.PIPE,
        env=env_for(gw.home()),
        cwd=gw.home(),
    )
    os.close(write_end)  # the child holds the only write end
    try:
        time.sleep(WRITE_WAIT_SECONDS + 0.5)
        assert proc.poll() is None  # still waiting for the reader after the give-up time
        data = b""
        while chunk := os.read(read_end, 65536):  # the pre-filled bytes come first
            data += chunk
        proc.wait(timeout=30)
        assert proc.returncode == 0
        assert proc.stderr is not None and proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stderr is not None:
            proc.stderr.close()
        os.close(read_end)
    assert data[:filled] == b"z" * filled
    assert_block(data[filled:].decode("utf-8"))


# --- the real deadline --------------------------------------------------------------------------


def _bulk(gw: FakeGateway, count: int, body: str) -> None:
    store = Store(gw)
    store.conn.execute("BEGIN")
    store.conn.executemany(
        "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (?, ?, 'implement', ?, ?, ?, ?, ?)",
        (
            (REPO, f"t{i % 50}", body, SHA, len(body.encode()), CREATED, EXPIRES)
            for i in range(count)
        ),
    )
    store.conn.execute("COMMIT")
    store.conn.close()


@pytest.fixture(scope="module")
def bulk(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FakeGateway]:
    """100,000 notes (about 60 MB on disk, built once): a scan the 1 s deadline cannot finish."""
    root = tmp_path_factory.mktemp("bulk")
    try:
        gw = FakeGateway(root)
        _bulk(gw, 100_000, "word " * 80)
        yield gw
    finally:
        shutil.rmtree(root, ignore_errors=True)


def set_config(gw: FakeGateway, text: str) -> None:
    d = gw.home() / ".config" / "memoctl"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    f = d / "config.yaml"
    f.write_text(text)
    f.chmod(0o600)


def test_the_progress_handler_stops_a_long_scan_at_the_deadline(bulk: FakeGateway) -> None:
    set_config(bulk, "context:\n  deadline: 1\n")
    for flags in ([], ["--json"]):
        start = time.monotonic()
        r = child(bulk.home(), ["--repo", REPO, *flags])
        took = time.monotonic() - start
        if r.returncode == 0:  # the scan must outlast the 1 s deadline for this test to mean it
            pytest.skip("scan finished inside the deadline on this machine")
        assert r.returncode == 12, (r.returncode, took)
        assert r.stderr == TIMEOUT_LINE
        if flags:
            err = json.loads(r.stdout)["error"]
            assert (err["code"], err["reason"], err["retryable"]) == ("failed", "timeout", True)
        else:
            assert r.stdout == b""
        # Generous bound (a loaded machine): the point is exit 12 with no partial output; the
        # deadline in each phase is pinned exactly by the fake-clock tests.
        assert took < 10, took


def test_the_same_scan_inside_a_roomy_deadline_prints_one_well_formed_block(
    bulk: FakeGateway,
) -> None:
    set_config(bulk, "context:\n  deadline: 10\n")
    r = child(bulk.home(), ["--repo", REPO, "--json"])
    assert r.returncode == 0 and r.stderr == b""
    doc = json.loads(r.stdout)
    assert doc["eligible"] == 100_000 and doc["shown"] > 0
    assert_block(doc["text"])


def test_a_locked_database_is_exit_12_db_locked_within_the_busy_timeout(
    gw: FakeGateway,
) -> None:
    seeded(gw)
    c: sqlite3.Connection = raw(gw.home() / ".local" / "state" / "memoctl" / "state.db")
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        r = child(gw.home(), ["--repo", REPO, "--json"])
        took = time.monotonic() - start
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert r.returncode == 12 and took < 5
    err = json.loads(r.stdout)["error"]
    assert (err["reason"], err["retryable"]) == ("db_locked", True)
    assert r.stderr.startswith(b"failed (db_locked)")


def test_missing_store_creates_nothing_in_the_real_home(gw: FakeGateway) -> None:
    before = sorted(p.relative_to(gw.root).as_posix() for p in gw.root.rglob("*"))
    r = child(gw.home(), ["--repo", REPO])
    assert r.returncode == 10
    assert sorted(p.relative_to(gw.root).as_posix() for p in gw.root.rglob("*")) == before


def test_an_initialized_empty_store_is_exit_10_with_nothing_printed(gw: FakeGateway) -> None:
    make_store(gw)
    r = child(gw.home(), ["--repo", REPO])
    assert (r.returncode, r.stdout) == (10, b"")


# --- the failure rows of the matrix through the real process ------------------------------------

DB_CORRUPT = b"failed (db_corrupt): database is corrupt\n"
DB_NEWER = b"failed (schema_newer): database schema is newer\n"
DB_UNREADABLE = b"failed (db_unreadable): database cannot be read\n"


def _corrupt(gw: FakeGateway) -> None:
    make_store(gw)
    db = state_path(gw) / "state.db"
    db.write_bytes(os.urandom(8192))
    db.chmod(0o600)


def _newer(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA user_version = 99")
    c.close()


def _foreign(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA application_id = 12345")
    c.close()


@pytest.mark.parametrize(
    ("prepare", "stderr"),
    [(_corrupt, DB_CORRUPT), (_foreign, DB_CORRUPT), (_newer, DB_NEWER)],
    ids=["corrupt", "foreign-application-id", "newer-schema"],
)
def test_a_bad_database_is_exit_12_with_empty_stdout_quickly(
    gw: FakeGateway, prepare: Any, stderr: bytes
) -> None:
    prepare(gw)
    start = time.monotonic()
    r = child(gw.home(), ["--repo", REPO], timeout=30)
    took = time.monotonic() - start
    assert (r.returncode, r.stdout, r.stderr) == (12, b"", stderr)
    assert took < 2.25, took


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.parametrize("what", ["state-dir-0500", "db-0000"])
def test_an_unreadable_database_is_exit_12_with_empty_stdout_quickly(
    gw: FakeGateway, what: str
) -> None:
    make_store(gw)
    sd = state_path(gw)
    db = sd / "state.db"
    target = sd if what == "state-dir-0500" else db
    original = target.stat().st_mode & 0o777
    target.chmod(0o500 if target is sd else 0)
    try:
        start = time.monotonic()
        r = child(gw.home(), ["--repo", REPO], timeout=30)
        took = time.monotonic() - start
    finally:
        target.chmod(original)
    assert (r.returncode, r.stdout, r.stderr) == (12, b"", DB_UNREADABLE)
    assert took < 2.25, took
