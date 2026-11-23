"""Gateway file, directory, identity and environment accessors added for the config loader.

The same behaviors are checked on the real gateway (on private temp files) and on the fake.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from memoctl.gateway import Gateway, PathInfo, RealGateway
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


def test_uid_is_the_process_uid(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, _ = gw_and_dir
    assert gw.uid() == os.getuid()


def test_stat_path_kinds_owner_and_mode(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    f = d / "f"
    f.write_bytes(b"x")
    f.chmod(0o640)
    (d / "sub").mkdir()
    (d / "link").symlink_to(f)
    assert gw.stat_path(f, follow=False) == PathInfo("file", os.getuid(), 0o640)
    assert gw.stat_path(d / "sub", follow=False) is not None
    assert gw.stat_path(d / "sub", follow=False).kind == "dir"  # type: ignore[union-attr]
    assert gw.stat_path(d / "link", follow=False).kind == "symlink"  # type: ignore[union-attr]
    assert gw.stat_path(d / "link", follow=True).kind == "file"  # type: ignore[union-attr]
    assert gw.stat_path(d / "missing", follow=False) is None
    assert gw.stat_path(f / "below-a-file", follow=False) is None


def test_list_dir_returns_names(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "b.yaml").write_bytes(b"")
    (d / "a.yaml").write_bytes(b"")
    assert sorted(gw.list_dir(d)) == ["a.yaml", "b.yaml"]
    with pytest.raises(OSError):
        gw.list_dir(d / "missing")


def test_open_file_reads_a_regular_file(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "f").write_bytes(b"hello")
    got = gw.open_file(d / "f", 100)
    assert got is not None
    assert got.info.kind == "file"
    assert got.data == b"hello"
    assert got.info.uid == os.getuid()


def test_open_file_reads_at_most_limit_plus_one(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "f").write_bytes(b"x" * 1000)
    got = gw.open_file(d / "f", 10)
    assert got is not None
    assert got.data == b"x" * 11


def test_open_file_missing_is_none(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    assert gw.open_file(d / "missing", 10) is None


def test_open_file_never_follows_a_symlink(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "target").write_bytes(b"secret")
    (d / "link").symlink_to(d / "target")
    got = gw.open_file(d / "link", 100)
    assert got is not None
    assert got.info.kind == "symlink"
    assert got.data == b""


def test_open_file_does_not_read_a_directory_or_fifo(gw_and_dir: tuple[Gateway, Path]) -> None:
    gw, d = gw_and_dir
    (d / "sub").mkdir()
    os.mkfifo(d / "fifo")
    for name in ("sub", "fifo"):
        got = gw.open_file(d / name, 100)
        assert got is not None
        assert got.info.kind in ("dir", "other")
        assert got.data == b""


def test_env_reads_the_given_variable(
    gw_and_dir: tuple[Gateway, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, _ = gw_and_dir
    if isinstance(gw, FakeGateway):
        gw.environ["MEMOCTL_T"] = "v"
    else:
        monkeypatch.setenv("MEMOCTL_T", "v")
    assert gw.env("MEMOCTL_T") == "v"
    assert gw.env("MEMOCTL_UNSET_T") is None
