"""The body reader: bounded, never follows a symlink, refuses special files, fixed reasons.

Every FIFO, symlink and pipe is made under a pytest temporary directory. A test that would
hang (a reader that blocks) is cut off by an interval timer so the suite fails instead.
"""

from __future__ import annotations

import os
import pty
import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import pytest

from memoctl.bodyread import BodyError, read_body
from memoctl.gateway import BodySourceError, RealGateway
from tests.fake_gateway import FakeGateway

LIMIT = 8192
HANG_SECONDS = 10


class _Hung(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_hang() -> Iterator[None]:
    def fire(signum: int, frame: FrameType | None) -> None:
        raise _Hung("the reader blocked")

    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, HANG_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.fixture
def stdin_fd() -> Iterator[object]:
    """Replace descriptor 0 for the test; restore it afterwards."""
    saved = os.dup(0)

    def install(fd: int) -> None:
        os.dup2(fd, 0)

    try:
        yield install
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def _reason(excinfo: pytest.ExceptionInfo[BodyError]) -> str:
    return excinfo.value.reason


# --- file source, real gateway --------------------------------------------------------------


def test_file_at_the_limit_is_accepted(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes(b"a" * LIMIT)
    assert read_body(RealGateway(), f, LIMIT) == b"a" * LIMIT


def test_file_one_byte_over_the_limit_is_refused_not_truncated(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes(b"a" * (LIMIT + 1))
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), f, LIMIT)
    assert _reason(e) == "too_long"
    assert e.value.limit == LIMIT


def test_empty_file_is_empty_bytes(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes(b"")
    assert read_body(RealGateway(), f, LIMIT) == b""


def test_bytes_are_returned_as_written_with_no_decoding(tmp_path: Path) -> None:
    f = tmp_path / "b"
    raw = b"\xff\xfe\x00 caf\xc3\xa9\r\n"
    f.write_bytes(raw)
    assert read_body(RealGateway(), f, LIMIT) == raw


def test_symlink_to_a_regular_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_bytes(b"x")
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), link, LIMIT)
    assert _reason(e) == "symlink"


def test_dangling_symlink_is_refused_as_a_symlink(tmp_path: Path) -> None:
    link = tmp_path / "l"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), link, LIMIT)
    assert _reason(e) == "symlink"


def test_symlink_to_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    link = tmp_path / "l"
    link.symlink_to(tmp_path / "d")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), link, LIMIT)
    assert _reason(e) == "symlink"


def test_symlink_to_dev_zero_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "l"
    link.symlink_to("/dev/zero")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), link, LIMIT)
    assert _reason(e) == "symlink"


def test_symlink_in_a_parent_directory_is_followed_only_in_the_last_component(
    tmp_path: Path,
) -> None:
    # The spec forbids following the last component only; a linked parent is allowed.
    real = tmp_path / "real"
    real.mkdir()
    (real / "b").write_bytes(b"ok")
    (tmp_path / "dirlink").symlink_to(real)
    assert read_body(RealGateway(), tmp_path / "dirlink" / "b", LIMIT) == b"ok"


def test_fifo_without_a_writer_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), fifo, LIMIT)
    assert _reason(e) == "not_regular"


def test_fifo_with_an_endless_writer_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    stop = threading.Event()

    def writer() -> None:
        fd = os.open(fifo, os.O_WRONLY)  # blocks until the reader opens
        try:
            while not stop.is_set():
                os.write(fd, b"x" * 4096)
        except OSError:
            pass
        finally:
            os.close(fd)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        with pytest.raises(BodyError) as e:
            read_body(RealGateway(), fifo, LIMIT)
        assert _reason(e) == "not_regular"
    finally:
        stop.set()
        # unblock a writer still waiting in open(): open the read end ourselves
        try:
            fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        t.join(5)


def test_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), tmp_path, LIMIT)
    assert _reason(e) == "not_regular"


@pytest.mark.parametrize("dev", ["/dev/zero", "/dev/null", "/dev/urandom"])
def test_character_device_is_refused(dev: str) -> None:
    if not Path(dev).exists():
        pytest.skip(f"{dev} missing")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), Path(dev), LIMIT)
    assert _reason(e) == "not_regular"


def test_missing_file_and_permission_denied_are_unreadable(tmp_path: Path) -> None:
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), tmp_path / "missing", LIMIT)
    assert _reason(e) == "unreadable"
    f = tmp_path / "b"
    f.write_bytes(b"x")
    f.chmod(0)
    if os.access(f, os.R_OK):
        pytest.skip("running with privileges that ignore file modes")
    with pytest.raises(BodyError) as e2:
        read_body(RealGateway(), f, LIMIT)
    assert _reason(e2) == "unreadable"


def test_path_with_nul_is_unreadable_and_the_empty_path_is_the_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), Path("a\0b"), LIMIT)
    assert _reason(e) == "unreadable"
    with pytest.raises(BodyError) as e2:
        read_body(RealGateway(), Path(""), LIMIT)  # Path("") is "."
    assert _reason(e2) == "not_regular"


def test_procfs_file_reporting_size_zero_is_read_up_to_the_bound() -> None:
    if not Path("/proc/self/status").exists():
        pytest.skip("no procfs")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), Path("/proc/self/status"), 16)
    assert _reason(e) == "too_long"
    assert len(read_body(RealGateway(), Path("/proc/self/status"), 65536)) > 0


def test_a_file_that_grows_is_still_bounded(tmp_path: Path) -> None:
    f = tmp_path / "g"
    f.write_bytes(b"a" * 10)
    gw = RealGateway()
    assert gw.read_file_bounded(f, 11) == b"a" * 10
    f.write_bytes(b"a" * 1_000_000)
    assert len(gw.read_file_bounded(f, 11)) == 11


def test_failure_messages_never_echo_the_path_or_content(tmp_path: Path) -> None:
    secret = tmp_path / "very-secret-name"
    secret.symlink_to(tmp_path / "x")
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), secret, LIMIT)
    assert "very-secret-name" not in str(e.value)
    assert str(tmp_path) not in str(e.value)
    assert "very-secret-name" not in repr(e.value)


def test_gateway_error_for_a_source_failure_is_a_body_source_error(tmp_path: Path) -> None:
    with pytest.raises(BodySourceError) as e:
        RealGateway().read_file_bounded(tmp_path, 10)
    assert e.value.reason == "not_regular"


# --- stdin, real gateway --------------------------------------------------------------------


def _pipe_stdin(stdin_fd: object, data: bytes) -> None:
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    stdin_fd(r)  # type: ignore[operator]
    os.close(r)


def test_stdin_pipe_at_the_limit_is_accepted(stdin_fd: object) -> None:
    _pipe_stdin(stdin_fd, b"a" * 4096)
    assert read_body(RealGateway(), "-", 4096) == b"a" * 4096


def test_stdin_pipe_one_byte_over_is_refused(stdin_fd: object) -> None:
    _pipe_stdin(stdin_fd, b"a" * 4097)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), "-", 4096)
    assert _reason(e) == "too_long"


def test_stdin_larger_than_the_pipe_buffer_is_read_in_full_up_to_the_bound(
    stdin_fd: object,
) -> None:
    r, w = os.pipe()
    stdin_fd(r)  # type: ignore[operator]
    os.close(r)
    payload = b"b" * 65536

    def feed() -> None:
        try:
            os.write(w, payload)
        finally:
            os.close(w)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    assert read_body(RealGateway(), "-", 65536) == payload
    t.join(5)


def test_endless_stdin_dev_zero_stops_at_the_bound(stdin_fd: object) -> None:
    fd = os.open("/dev/zero", os.O_RDONLY)
    try:
        stdin_fd(fd)  # type: ignore[operator]
    finally:
        os.close(fd)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), "-", LIMIT)
    assert _reason(e) == "too_long"


def test_endless_stdin_pipe_stops_at_the_bound(stdin_fd: object) -> None:
    r, w = os.pipe()
    stdin_fd(r)  # type: ignore[operator]
    os.close(r)

    def feed() -> None:
        try:
            while True:
                os.write(w, b"y" * 4096)
        except OSError:
            pass
        finally:
            os.close(w)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), "-", LIMIT)
    assert _reason(e) == "too_long"
    # the reader stopped: put /dev/null on fd 0 so the writer sees EPIPE and ends
    nul = os.open("/dev/null", os.O_RDONLY)
    try:
        os.dup2(nul, 0)
    finally:
        os.close(nul)
    t.join(5)
    assert not t.is_alive()


def test_stdin_that_is_a_terminal_is_refused_without_reading(stdin_fd: object) -> None:
    master, slave = pty.openpty()
    try:
        stdin_fd(slave)  # type: ignore[operator]
        with pytest.raises(BodyError) as e:
            read_body(RealGateway(), "-", LIMIT)
        assert _reason(e) == "terminal"
    finally:
        os.close(master)
        os.close(slave)


def test_stdin_dev_null_is_an_empty_body(stdin_fd: object) -> None:
    fd = os.open("/dev/null", os.O_RDONLY)
    try:
        stdin_fd(fd)  # type: ignore[operator]
    finally:
        os.close(fd)
    assert read_body(RealGateway(), "-", LIMIT) == b""


def test_stdin_that_is_a_directory_is_refused(stdin_fd: object, tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        stdin_fd(fd)  # type: ignore[operator]
    finally:
        os.close(fd)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), "-", LIMIT)
    assert _reason(e) == "not_regular"


def test_stdin_redirected_from_a_regular_file_is_read(stdin_fd: object, tmp_path: Path) -> None:
    f = tmp_path / "in"
    f.write_bytes(b"from file")
    fd = os.open(f, os.O_RDONLY)
    try:
        stdin_fd(fd)  # type: ignore[operator]
    finally:
        os.close(fd)
    assert read_body(RealGateway(), "-", LIMIT) == b"from file"


def test_closed_stdin_is_unreadable(stdin_fd: object) -> None:
    saved = os.dup(0)
    try:
        os.close(0)
        with pytest.raises(BodyError) as e:
            read_body(RealGateway(), "-", LIMIT)
        assert _reason(e) == "unreadable"
    finally:
        os.dup2(saved, 0)
        os.close(saved)


# --- argument checks and policy -------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_limit_is_a_programming_error(bad: int, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_body(RealGateway(), tmp_path / "x", bad)


def test_every_reason_has_a_fixed_message() -> None:
    messages = {BodyError(r, 5).message for r in BodyError.REASONS}
    assert len(messages) == len(BodyError.REASONS)
    assert all(m and "\n" not in m for m in messages)


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        BodyError("because", 5)


def test_limit_in_details_only_for_too_long() -> None:
    assert BodyError("too_long", 5).details == {"field": "body", "limit": 5}
    assert BodyError("symlink", 5).details == {"field": "body"}


def test_the_field_can_be_text_for_search(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes(b"a" * 1025)
    with pytest.raises(BodyError) as e:
        read_body(RealGateway(), f, 1024, field="text")
    assert e.value.details == {"field": "text", "limit": 1024}


# --- fake gateway honors the same contract --------------------------------------------------


def test_fake_file_contract(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    (gw.root / "ok").write_bytes(b"abc")
    (gw.root / "big").write_bytes(b"a" * 11)
    (gw.root / "link").symlink_to(gw.root / "ok")
    (gw.root / "dir").mkdir()
    assert read_body(gw, Path("ok"), 3) == b"abc"
    for name, reason in (
        ("big", "too_long"),
        ("link", "symlink"),
        ("dir", "not_regular"),
        ("missing", "unreadable"),
    ):
        with pytest.raises(BodyError) as e:
            read_body(gw, Path(name), 10)
        assert e.value.reason == reason


def test_fake_stdin_contract(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.terminal.stdin = "héllo"
    assert read_body(gw, "-", 6) == "héllo".encode()
    with pytest.raises(BodyError) as e:
        read_body(gw, "-", 5)
    assert _reason(e) == "too_long"
    gw.terminal.stdin = b"\xff\xfe"
    assert read_body(gw, "-", 5) == b"\xff\xfe"
    gw.terminal.tty = {0: True}
    with pytest.raises(BodyError) as e2:
        read_body(gw, "-", 5)
    assert _reason(e2) == "terminal"


def test_a_str_path_other_than_dash_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        read_body(FakeGateway(tmp_path), "relative.txt", 5)  # type: ignore[arg-type]
