"""Directory handles and atomic file writes of the gateway (added for the state directory).

The same behaviors are checked on the real gateway (private temp dirs) and on the fake.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from memoctl.gateway import Gateway, RealGateway
from tests.fake_gateway import FakeGateway

Make = Callable[[Path], Gateway]


def _real(root: Path) -> Gateway:
    return RealGateway()


def _fake(root: Path) -> Gateway:
    return FakeGateway(root)


@pytest.fixture(params=[_real, _fake], ids=["real", "fake"])
def gw_and_dir(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Gateway, Path]:
    gw = request.param(tmp_path / "fakeroot")
    base = tmp_path / "fakeroot" / "home" if isinstance(gw, FakeGateway) else tmp_path / "d"
    base.mkdir(parents=True, exist_ok=True)
    return gw, base


@pytest.fixture
def umask_all() -> Iterator[None]:
    """The most hostile umask: every permission bit masked."""
    old = os.umask(0o777)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# --- open_dir ---------------------------------------------------------------------------------


def test_open_dir_describes_the_opened_descriptor(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "sub").mkdir(mode=0o750)
    (d / "sub").chmod(0o750)
    handle = gw.open_dir(d / "sub", follow=False)
    assert handle is not None
    try:
        info = handle.stat()
        assert (info.kind, info.uid, info.mode) == ("dir", os.getuid(), 0o750)
    finally:
        handle.close()


def test_open_dir_missing_is_none(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    assert gw.open_dir(d / "missing", follow=False) is None
    assert gw.open_dir(d / "missing" / "deeper", follow=True) is None


def test_open_dir_refuses_a_final_symlink_only_when_not_following(
    gw_and_dir: tuple[Gateway, Path],
) -> None:
    gw, d = gw_and_dir
    (d / "real").mkdir()
    (d / "link").symlink_to(d / "real")
    with pytest.raises(OSError):
        gw.open_dir(d / "link", follow=False)
    followed = gw.open_dir(d / "link", follow=True)
    assert followed is not None
    followed.close()


def test_open_dir_refuses_a_regular_file(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "f").write_bytes(b"x")
    for follow in (True, False):
        with pytest.raises(NotADirectoryError):
            gw.open_dir(d / "f", follow=follow)
    with pytest.raises(NotADirectoryError):
        gw.open_dir(d / "f" / "below", follow=True)


# --- DirHandle --------------------------------------------------------------------------------


def test_mkdir_creates_with_exactly_the_mode_despite_the_umask(
    gw_and_dir: tuple[Gateway, Path], umask_all: None
) -> None:
    gw, d = gw_and_dir
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        handle.mkdir("child", 0o700)
        sub = handle.open_dir("child", follow=False)
        assert sub is not None
        sub.close()
    finally:
        handle.close()
    assert _mode(d / "child") == 0o700


def test_mkdir_existing_raises_file_exists(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "child").mkdir()
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        with pytest.raises(FileExistsError):
            handle.mkdir("child", 0o700)
    finally:
        handle.close()


def test_mkdir_never_follows_a_dangling_symlink(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "link").symlink_to(d / "nowhere")
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        with pytest.raises(FileExistsError):
            handle.mkdir("link", 0o700)
    finally:
        handle.close()
    assert not (d / "nowhere").exists()


def test_set_mode_is_exact_despite_the_umask(
    gw_and_dir: tuple[Gateway, Path], umask_all: None
) -> None:
    gw, d = gw_and_dir
    (d / "sub").mkdir()
    (d / "sub").chmod(0o755)
    handle = gw.open_dir(d / "sub", follow=False)
    assert handle is not None
    try:
        handle.set_mode(0o700)
        assert handle.stat().mode == 0o700
    finally:
        handle.close()
    assert _mode(d / "sub") == 0o700


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/abs", "a\0b"])
def test_handle_refuses_names_that_are_not_one_component(
    gw_and_dir: tuple[Gateway, Path], name: str
) -> None:
    gw, d = gw_and_dir
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        for call in (
            lambda: handle.mkdir(name, 0o700),
            lambda: handle.open_dir(name, follow=False),
            lambda: handle.lstat(name),
            lambda: handle.create_file(name, 0o600),
        ):
            with pytest.raises(ValueError):
                call()
    finally:
        handle.close()


def test_handle_open_dir_follow_and_missing(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "real").mkdir()
    (d / "link").symlink_to(d / "real")
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        assert handle.open_dir("missing", follow=False) is None
        with pytest.raises(OSError):
            handle.open_dir("link", follow=False)
        via = handle.open_dir("link", follow=True)
        assert via is not None
        via.close()
    finally:
        handle.close()


def test_handle_lstat_does_not_follow(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "f").write_bytes(b"x")
    (d / "f").chmod(0o640)
    (d / "link").symlink_to(d / "f")
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        info = handle.lstat("f")
        assert info is not None
        assert (info.kind, info.mode) == ("file", 0o640)
        link = handle.lstat("link")
        assert link is not None and link.kind == "symlink"
        assert handle.lstat("missing") is None
    finally:
        handle.close()


def test_create_file_is_exclusive_and_exactly_the_mode(
    gw_and_dir: tuple[Gateway, Path], umask_all: None
) -> None:
    gw, d = gw_and_dir
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        assert handle.create_file("state.db", 0o600) is True
        assert handle.create_file("state.db", 0o600) is False
    finally:
        handle.close()
    assert _mode(d / "state.db") == 0o600
    assert (d / "state.db").read_bytes() == b""


def test_create_file_leaves_an_existing_file_and_a_symlink_alone(
    gw_and_dir: tuple[Gateway, Path],
) -> None:
    gw, d = gw_and_dir
    (d / "other").write_bytes(b"keep")
    (d / "other").chmod(0o644)
    (d / "link").symlink_to(d / "other")
    (d / "dangling").symlink_to(d / "created-through-link")
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    try:
        assert handle.create_file("link", 0o600) is False
        assert handle.create_file("dangling", 0o600) is False
        assert handle.create_file("other", 0o600) is False
    finally:
        handle.close()
    assert (d / "other").read_bytes() == b"keep"
    assert _mode(d / "other") == 0o644
    assert not (d / "created-through-link").exists()


def test_a_closed_handle_refuses_use(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    handle = gw.open_dir(d, follow=True)
    assert handle is not None
    handle.close()
    handle.close()  # closing twice is harmless
    with pytest.raises(OSError):
        handle.lstat("x")


# --- write_bytes ------------------------------------------------------------------------------


def test_write_bytes_default_mode_is_0600_despite_the_umask(
    gw_and_dir: tuple[Gateway, Path], umask_all: None
) -> None:
    gw, d = gw_and_dir
    gw.write_bytes(d / "f", b"data")
    assert (d / "f").read_bytes() == b"data"
    assert _mode(d / "f") == 0o600


def test_write_bytes_takes_an_explicit_mode(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    gw.write_bytes(d / "f", b"data", mode=0o640)
    assert _mode(d / "f") == 0o640


def test_write_bytes_replaces_and_resets_the_mode(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "f").write_bytes(b"old old old")
    (d / "f").chmod(0o666)
    gw.write_bytes(d / "f", b"new")
    assert (d / "f").read_bytes() == b"new"
    assert _mode(d / "f") == 0o600


def test_write_bytes_leaves_no_temporary_file(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    gw.write_bytes(d / "f", b"1")
    gw.write_bytes(d / "f", b"2")
    assert sorted(os.listdir(d)) == ["f"]


def test_write_bytes_replaces_a_symlink_instead_of_writing_through_it(
    gw_and_dir: tuple[Gateway, Path],
) -> None:
    gw, d = gw_and_dir
    (d / "victim").write_bytes(b"untouched")
    (d / "f").symlink_to(d / "victim")
    gw.write_bytes(d / "f", b"new")
    assert (d / "victim").read_bytes() == b"untouched"
    assert not (d / "f").is_symlink()
    assert (d / "f").read_bytes() == b"new"


def test_write_bytes_does_not_create_parents(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    with pytest.raises(FileNotFoundError):
        gw.write_bytes(d / "missing" / "f", b"x")
    assert not (d / "missing").exists()


def test_write_bytes_failure_leaves_the_old_file_and_no_temporary(
    gw_and_dir: tuple[Gateway, Path],
) -> None:
    gw, d = gw_and_dir
    (d / "target").mkdir()  # a directory cannot be replaced by a file
    with pytest.raises(OSError):
        gw.write_bytes(d / "target", b"x")
    assert sorted(os.listdir(d)) == ["target"]


def test_write_bytes_rejects_non_bytes(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    with pytest.raises(TypeError):
        gw.write_bytes(d / "f", "text")  # type: ignore[arg-type]
