"""The log writer: record bytes, single-write append, modes, hostile files, rotation."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from memoctl import logfile
from memoctl.gateway import RealGateway
from memoctl.logfile import (
    LOG_NAME,
    MAX_LINE_BYTES,
    ROTATE_BYTES,
    ROTATED_NAME,
    LogError,
    LogRecord,
    append_record,
    encode_record,
    format_ts,
)
from memoctl.statedir import StateDir
from tests.fake_gateway import FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import validate

NOW = datetime(2026, 3, 4, 5, 6, 7, 891999, tzinfo=UTC)
SHA = "0123456789abcdef"


def add_record(**over: Any) -> LogRecord:
    base: dict[str, Any] = {
        "event": "add",
        "note_id": 7,
        "actor": "auto:unknown",
        "actor_source": "default",
        "repo": "example.org/team/app",
        "topic": "build",
        "kind": "unknown",
        "size": 120,
        "body_sha256": SHA,
    }
    base.update(over)
    return LogRecord(**base)


@pytest.fixture
def sdir(tmp_path: Path) -> Iterator[StateDir]:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    gw = RealGateway()
    handle = gw.open_dir(d, follow=False)
    assert handle is not None
    with StateDir(gw, handle, d) as state:
        yield state


def lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


# ---- record bytes -------------------------------------------------------------------------


def test_add_record_golden() -> None:
    check_golden(
        "log/add.json", encode_record(add_record(supersedes=3), NOW, "0.0.1"), schema="log"
    )


def test_delete_record_golden() -> None:
    rec = LogRecord(
        event="delete",
        note_id=9,
        actor="human:jake",
        actor_source="tty",
        repo="example.org/a",
        topic="t",
        kind="k",
        size=5,
        body_sha256=SHA,
        reinstated=4,
    )
    check_golden("log/delete.json", encode_record(rec, NOW, "0.0.1"), schema="log")


def test_prune_record_with_failed_row_check_golden() -> None:
    rec = LogRecord(event="prune", note_id=11, actor="auto:cron", actor_source="flag")
    check_golden("log/prune-nulls.json", encode_record(rec, NOW, "0.0.1"), schema="log")


def test_line_is_compact_ascii_ordered_and_newline_terminated() -> None:
    line = encode_record(add_record(), NOW)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    line.decode("ascii")
    keys = list(json.loads(line))
    assert keys == [
        *("ts", "tool", "version", "event", "note_id", "repo", "topic", "kind", "size"),
        *("body_sha256", "supersedes", "reinstated", "actor", "actor_source"),
    ]
    validate("log", json.loads(line))


def test_timestamp_floors_to_milliseconds_in_utc() -> None:
    assert format_ts(NOW) == "2026-03-04T05:06:07.891Z"
    other = NOW.astimezone(timezone(timedelta(hours=-5)))
    assert format_ts(other) == "2026-03-04T05:06:07.891Z"
    assert format_ts(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000Z"


def test_naive_time_is_refused() -> None:
    with pytest.raises(LogError) as exc:
        format_ts(datetime(2026, 1, 1))
    assert exc.value.reason == "invalid_record"


HOSTILE = [
    "a\nb",
    "a\x00b",
    "\ud800",
    "x" * 10_000,
    "",
    "é",
    '"}\n{"event":"add"',
    "‮",
]


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", ["repo", "topic", "kind", "actor", "body_sha256"])
def test_hostile_field_values_are_refused_not_escaped(field: str, value: str) -> None:
    with pytest.raises(LogError) as exc:
        encode_record(add_record(**{field: value}), NOW)
    assert exc.value.reason == "invalid_record"
    assert value not in str(exc.value) or value == ""


@pytest.mark.parametrize(
    "over",
    [
        {"event": "update"},
        {"note_id": 0},
        {"note_id": True},
        {"note_id": 10**18},
        {"note_id": "7"},
        {"size": 0},
        {"size": 65537},
        {"size": True},
        {"actor_source": "robot"},
        {"repo": None},
        {"topic": None},
        {"kind": None},
        {"size": None},
        {"body_sha256": None},
        {"body_sha256": "A" * 16},
        {"body_sha256": "0" * 64},
        {"reinstated": 3},
        {"supersedes": 0},
        {"repo": "example.org/" + "a" * 300},
    ],
)
def test_schema_violating_records_are_refused(over: dict[str, Any]) -> None:
    with pytest.raises(LogError) as exc:
        encode_record(add_record(**over), NOW)
    assert exc.value.reason == "invalid_record"


def test_event_specific_rules() -> None:
    with pytest.raises(LogError):
        encode_record(LogRecord("delete", 1, "auto:x", "flag", supersedes=2), NOW)
    with pytest.raises(LogError):
        encode_record(LogRecord("prune", 1, "auto:x", "flag", reinstated=2), NOW)
    encode_record(LogRecord("delete", 1, "auto:x", "flag", reinstated=2), NOW)


def test_overlong_version_is_refused_and_a_lone_surrogate_version_is_escaped() -> None:
    with pytest.raises(LogError):
        encode_record(add_record(), NOW, "v" * 65)
    line = encode_record(add_record(), NOW, "\ud800")
    line.decode("ascii")
    assert json.loads(line)["version"] == "\ud800"


def test_maximal_record_fits_the_line_cap() -> None:
    rec = add_record(
        repo="a" * 3 + "/" + "b" * 251,
        topic="t" * 64,
        kind="k" * 64,
        actor="human:" + "a" * 64,
        supersedes=10**18 - 1,
        note_id=10**18 - 1,
    )
    assert len(encode_record(rec, NOW, "v" * 64)) <= MAX_LINE_BYTES


def test_a_line_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logfile, "MAX_LINE_BYTES", 100)
    with pytest.raises(LogError) as exc:
        encode_record(add_record(), NOW)
    assert exc.value.reason == "too_long"


# ---- appending ----------------------------------------------------------------------------


def test_append_creates_log_at_0600_and_appends_lines(sdir: StateDir) -> None:
    append_record(sdir, add_record(note_id=1), NOW)
    append_record(sdir, add_record(note_id=2), NOW)
    path = sdir.path / LOG_NAME
    assert os.stat(path).st_mode & 0o7777 == 0o600
    assert [json.loads(x)["note_id"] for x in lines(path)] == [1, 2]


@pytest.mark.parametrize("mask", [0o000, 0o077, 0o777])
def test_mode_is_exact_under_any_umask(sdir: StateDir, mask: int) -> None:
    old = os.umask(mask)
    try:
        append_record(sdir, add_record(), NOW)
    finally:
        os.umask(old)
    assert os.stat(sdir.path / LOG_NAME).st_mode & 0o7777 == 0o600
    assert len(lines(sdir.path / LOG_NAME)) == 1


def test_an_own_file_with_a_wrong_mode_is_set_back_to_0600(sdir: StateDir) -> None:
    path = sdir.path / LOG_NAME
    path.write_bytes(b'{"old":1}\n')
    path.chmod(0o666)
    append_record(sdir, add_record(), NOW)
    assert os.stat(path).st_mode & 0o7777 == 0o600
    assert len(lines(path)) == 2


def test_a_symlinked_log_is_refused_and_its_target_untouched(
    sdir: StateDir, tmp_path: Path
) -> None:
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    (sdir.path / LOG_NAME).symlink_to(target)
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert exc.value.reason == "open_failed"
    assert target.read_bytes() == b"keep"


def test_a_dangling_symlinked_log_is_not_created_through(sdir: StateDir, tmp_path: Path) -> None:
    (sdir.path / LOG_NAME).symlink_to(tmp_path / "new")
    with pytest.raises(LogError):
        append_record(sdir, add_record(), NOW)
    assert not (tmp_path / "new").exists()


def test_a_fifo_log_is_refused_without_blocking(sdir: StateDir) -> None:
    os.mkfifo(sdir.path / LOG_NAME)
    start = time.monotonic()
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert exc.value.reason == "unsafe_file"
    assert time.monotonic() - start < 2


def test_a_directory_log_is_refused(sdir: StateDir) -> None:
    (sdir.path / LOG_NAME).mkdir()
    with pytest.raises(LogError):
        append_record(sdir, add_record(), NOW)


def test_a_foreign_owner_log_is_refused(sdir: StateDir, monkeypatch: pytest.MonkeyPatch) -> None:
    (sdir.path / LOG_NAME).write_bytes(b"")
    monkeypatch.setattr(StateDir, "uid", property(lambda self: os.getuid() + 1))
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert exc.value.reason == "unsafe_file"
    assert (sdir.path / LOG_NAME).read_bytes() == b""


def test_fake_gateway_uid_is_honoured(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    d = gw.home() / "state"
    d.mkdir()
    handle = gw.open_dir(d, follow=False)
    assert handle is not None
    (d / LOG_NAME).write_bytes(b"")
    gw.uid_value = os.getuid() + 1
    with StateDir(gw, handle, d) as state, pytest.raises(LogError) as exc:
        append_record(state, add_record(), NOW)
    assert exc.value.reason == "unsafe_file"


def test_a_partial_last_line_is_not_merged_into(sdir: StateDir) -> None:
    path = sdir.path / LOG_NAME
    path.write_bytes(b'{"ts":"2026-01-01T00:00:00.000Z","tool":"mem')
    append_record(sdir, add_record(note_id=5), NOW)
    append_record(sdir, add_record(note_id=6), NOW)
    assert path.read_bytes().startswith(b'{"ts":"2026-01-01T00:00:00.000Z","tool":"mem\n{')
    parsed = []
    for raw in path.read_bytes().split(b"\n"):
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw)["note_id"])
        except (ValueError, KeyError):
            pass
    assert parsed == [5, 6]
    assert path.read_bytes().count(b"\n\n") == 0


def test_each_append_is_exactly_one_write_call(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write
    calls: list[int] = []

    def spy(fd: int, data: Any) -> int:
        calls.append(len(data))
        return real(fd, data)

    monkeypatch.setattr("memoctl.gateway.os.write", spy)
    append_record(sdir, add_record(), NOW)
    assert len(calls) == 1
    assert calls[0] == len(encode_record(add_record(), NOW))


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EIO, errno.EDQUOT, errno.EROFS])
def test_write_errors_are_a_log_error_and_leave_no_partial_line(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    def boom(fd: int, data: Any) -> int:
        raise OSError(err, "x")

    monkeypatch.setattr("memoctl.gateway.os.write", boom)
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert exc.value.reason == "write_failed"
    assert (sdir.path / LOG_NAME).read_bytes() == b""


def test_short_write_is_reported_and_the_next_append_starts_a_new_line(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write
    state = {"short": True}

    def short(fd: int, data: Any) -> int:
        if state["short"]:
            state["short"] = False
            return real(fd, data[: len(data) // 2])
        return real(fd, data)

    monkeypatch.setattr("memoctl.gateway.os.write", short)
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(note_id=1), NOW)
    assert exc.value.reason == "short_write"
    append_record(sdir, add_record(note_id=2), NOW)
    good = []
    for raw in (sdir.path / LOG_NAME).read_bytes().split(b"\n"):
        try:
            good.append(json.loads(raw)["note_id"])
        except (ValueError, KeyError):
            pass
    assert good == [2]


def test_state_directory_removed_mid_run_is_a_log_error(sdir: StateDir) -> None:
    append_record(sdir, add_record(), NOW)
    (sdir.path / LOG_NAME).unlink()
    sdir.path.rmdir()
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert exc.value.reason == "open_failed"


def test_log_error_message_has_no_path_or_value(sdir: StateDir) -> None:
    (sdir.path / LOG_NAME).mkdir()
    with pytest.raises(LogError) as exc:
        append_record(sdir, add_record(), NOW)
    assert str(sdir.path) not in str(exc.value) and LOG_NAME not in str(exc.value)


def test_records_never_hold_text_beyond_the_schema_fields(sdir: StateDir) -> None:
    append_record(sdir, add_record(), NOW)
    doc = json.loads(lines(sdir.path / LOG_NAME)[0])
    assert set(doc) == set(logfile.LogRecord.__dataclass_fields__) | {"ts", "tool", "version"}
    assert "body" not in doc and "labels" not in doc


# ---- rotation -----------------------------------------------------------------------------


def big(path: Path, size: int = ROTATE_BYTES) -> None:
    with path.open("wb") as f:
        f.write(b'{"old":1}\n')
        f.truncate(size)
    path.chmod(0o600)


def test_below_the_cap_does_not_rotate(sdir: StateDir) -> None:
    big(sdir.path / LOG_NAME, ROTATE_BYTES - 1)
    append_record(sdir, add_record(), NOW)
    assert not (sdir.path / ROTATED_NAME).exists()
    assert not (sdir.path / "log.lock").exists()


def test_at_the_cap_rotates_and_the_line_goes_to_a_fresh_0600_log(sdir: StateDir) -> None:
    big(sdir.path / LOG_NAME)
    append_record(sdir, add_record(note_id=3), NOW)
    assert os.stat(sdir.path / ROTATED_NAME).st_size == ROTATE_BYTES
    assert [json.loads(x)["note_id"] for x in lines(sdir.path / LOG_NAME)] == [3]
    for name in (LOG_NAME, "log.lock"):
        assert os.stat(sdir.path / name).st_mode & 0o7777 == 0o600


def test_rotation_replaces_an_older_generation(sdir: StateDir) -> None:
    (sdir.path / ROTATED_NAME).write_bytes(b"older\n")
    big(sdir.path / LOG_NAME, ROTATE_BYTES + 5)
    append_record(sdir, add_record(), NOW)
    assert os.stat(sdir.path / ROTATED_NAME).st_size == ROTATE_BYTES + 5


def test_rotation_with_a_hostile_umask_keeps_modes(sdir: StateDir) -> None:
    big(sdir.path / LOG_NAME)
    old = os.umask(0o777)
    try:
        append_record(sdir, add_record(), NOW)
    finally:
        os.umask(old)
    assert os.stat(sdir.path / LOG_NAME).st_mode & 0o7777 == 0o600
    assert os.stat(sdir.path / "log.lock").st_mode & 0o7777 == 0o600


def test_recheck_under_the_lock_skips_a_rotation_another_process_did(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    big(sdir.path / LOG_NAME)
    real_take = logfile._take_lock

    def other_rotates_first(state: StateDir) -> Any:
        lock = real_take(state)
        os.rename(state.path / LOG_NAME, state.path / ROTATED_NAME)
        (state.path / LOG_NAME).write_bytes(b'{"other":1}\n')
        (state.path / LOG_NAME).chmod(0o600)
        return lock

    monkeypatch.setattr(logfile, "_take_lock", other_rotates_first)
    append_record(sdir, add_record(note_id=8), NOW)
    assert os.stat(sdir.path / ROTATED_NAME).st_size == ROTATE_BYTES  # not replaced by the new
    got = lines(sdir.path / LOG_NAME)
    assert got[0] == b'{"other":1}\n' and json.loads(got[1])["note_id"] == 8


def test_a_failed_rename_still_appends_to_the_current_file(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    big(sdir.path / LOG_NAME)
    (sdir.path / ROTATED_NAME).mkdir()  # a directory cannot be replaced by a file
    append_record(sdir, add_record(note_id=4), NOW)
    assert os.stat(sdir.path / LOG_NAME).st_size > ROTATE_BYTES
    assert json.loads(lines(sdir.path / LOG_NAME)[-1])["note_id"] == 4


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EROFS, errno.EIO])
def test_rotation_errors_do_not_stop_the_append(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    big(sdir.path / LOG_NAME)

    def boom(*a: Any, **k: Any) -> None:
        raise OSError(err, "x")

    monkeypatch.setattr("memoctl.gateway.os.rename", boom)
    append_record(sdir, add_record(note_id=4), NOW)
    assert json.loads(lines(sdir.path / LOG_NAME)[-1])["note_id"] == 4
    assert not (sdir.path / ROTATED_NAME).exists()


def test_an_unusable_lock_file_means_append_without_rotating(
    sdir: StateDir, tmp_path: Path
) -> None:
    big(sdir.path / LOG_NAME)
    (sdir.path / "log.lock").symlink_to(tmp_path / "elsewhere")
    append_record(sdir, add_record(note_id=4), NOW)
    assert not (sdir.path / ROTATED_NAME).exists()
    assert not (tmp_path / "elsewhere").exists()
    assert json.loads(lines(sdir.path / LOG_NAME)[-1])["note_id"] == 4


HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("ready", flush=True)
    time.sleep(60)
    """
)


def test_a_lock_held_by_a_stuck_process_bounds_the_wait_then_appends(
    sdir: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    big(sdir.path / LOG_NAME)
    monkeypatch.setattr(logfile, "LOCK_WAIT_SECONDS", 0.3)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(sdir.path / "log.lock")],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
        start = time.monotonic()
        append_record(sdir, add_record(note_id=4), NOW)
        elapsed = time.monotonic() - start
    finally:
        proc.kill()  # the exact process launched above
        proc.wait(timeout=10)
    assert 0.25 <= elapsed < 5
    assert not (sdir.path / ROTATED_NAME).exists()
    assert json.loads(lines(sdir.path / LOG_NAME)[-1])["note_id"] == 4


WRITER = textwrap.dedent(
    """
    import sys
    from datetime import datetime, UTC
    from pathlib import Path
    from memoctl.gateway import RealGateway
    from memoctl.logfile import LogRecord, append_record
    from memoctl.statedir import StateDir
    d = Path(sys.argv[1]); base = int(sys.argv[2]); count = int(sys.argv[3])
    gw = RealGateway()
    h = gw.open_dir(d, follow=False)
    state = StateDir(gw, h, d)
    print("ready", flush=True)
    sys.stdin.readline()
    for i in range(count):
        rec = LogRecord("add", base + i, "auto:w", "default", "example.org/a", "t", "unknown", 1,
                        "0123456789abcdef")
        append_record(state, rec, datetime(2026, 1, 1, tzinfo=UTC))
    """
)


def _ids(path: Path) -> list[int]:
    out = []
    for raw in path.read_bytes().split(b"\n"):
        if raw.startswith(b'{"ts"'):
            out.append(json.loads(raw)["note_id"])
    return out


@pytest.mark.parametrize("writers,count", [(2, 20), (4, 25)])
def test_processes_rotating_at_once_rotate_once_and_lose_no_line(
    sdir: StateDir, writers: int, count: int
) -> None:
    big(sdir.path / LOG_NAME)
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", WRITER, str(sdir.path), str(1000 * (n + 1)), str(count)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for n in range(writers)
    ]
    try:
        for p in procs:
            assert p.stdout is not None and p.stdout.readline() == b"ready\n"
        for p in procs:
            assert p.stdin is not None
            p.stdin.write(b"go\n")
            p.stdin.flush()
        for p in procs:
            _, err = p.communicate(timeout=60)
            assert p.returncode == 0, err.decode()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()  # exactly the processes launched above
                p.wait(timeout=10)
    rotated = sdir.path / ROTATED_NAME
    assert os.stat(rotated).st_size >= ROTATE_BYTES
    expected = {1000 * (n + 1) + i for n in range(writers) for i in range(count)}
    got = _ids(sdir.path / LOG_NAME) + _ids(rotated)
    assert sorted(got) == sorted(expected)  # every line once, none twice
    for raw in (sdir.path / LOG_NAME).read_bytes().split(b"\n")[:-1]:
        if raw:  # a blank line is allowed: a writer that saw another's write half done adds one
            json.loads(raw)  # no torn or merged line
    assert os.stat(sdir.path / LOG_NAME).st_mode & 0o7777 == 0o600


# ---- the gateway pieces -------------------------------------------------------------------


def test_open_file_at_refuses_path_components_and_missing_without_create(sdir: StateDir) -> None:
    for bad in ("", ".", "..", "a/b", "a\0b"):
        with pytest.raises(ValueError):
            sdir.handle.open_file_at(bad, 0o600, create=True)
    assert sdir.handle.open_file_at("nope", 0o600, create=False) is None
    assert sdir.handle.size_of("nope") is None


def test_size_of_reports_a_non_regular_file_as_minus_one(sdir: StateDir, tmp_path: Path) -> None:
    (sdir.path / "d").mkdir()
    (sdir.path / "l").symlink_to(tmp_path)
    assert sdir.handle.size_of("d") == -1
    assert sdir.handle.size_of("l") == -1


def test_flock_is_exclusive_between_descriptors_and_released_on_close(sdir: StateDir) -> None:
    a = sdir.handle.open_file_at("log.lock", 0o600, create=True)
    b = sdir.handle.open_file_at("log.lock", 0o600, create=True)
    assert a is not None and b is not None
    assert a.lock_exclusive(0.0)
    assert not b.lock_exclusive(0.05)
    a.close()
    assert b.lock_exclusive(0.0)
    b.close()
    with pytest.raises(OSError):
        b.size()
