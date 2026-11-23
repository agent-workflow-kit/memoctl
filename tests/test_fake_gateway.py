"""The gateway fake: usable with no real home, state directory, terminal or crontab."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memoctl.gateway import Gateway, GatewayError
from tests.fake_gateway import EPOCH, FakeGateway


def test_fake_satisfies_the_gateway_protocol(tmp_path: Path) -> None:
    gw: Gateway = FakeGateway(tmp_path)
    assert gw.now() == EPOCH


def test_clock_is_fixed_until_the_test_moves_it(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert gw.now() == gw.now()


def test_clock_moves_forward_and_backward(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.clock.advance(timedelta(hours=2))
    assert gw.now() == EPOCH + timedelta(hours=2)
    gw.clock.advance(timedelta(hours=-3))
    assert gw.now() == EPOCH - timedelta(hours=1)
    gw.clock.set(datetime(2030, 5, 6, tzinfo=UTC))
    assert gw.now() == datetime(2030, 5, 6, tzinfo=UTC)


def test_clock_rejects_naive_times(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    with pytest.raises(ValueError):
        gw.clock.set(datetime(2030, 1, 1))  # noqa: DTZ001


def test_home_is_a_private_directory_under_the_root(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert gw.home().is_dir()
    assert gw.home().parent == tmp_path


def test_files_live_under_the_root_and_refuse_escapes(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.write_bytes(gw.home() / "n", b"\x00x")
    assert gw.read_bytes(gw.home() / "n") == b"\x00x"
    for bad in (Path("/etc/passwd"), gw.home() / ".." / ".." / "x", tmp_path.parent / "x"):
        with pytest.raises(GatewayError):
            gw.read_bytes(bad)
        with pytest.raises(GatewayError):
            gw.write_bytes(bad, b"")


def test_files_refuse_symlink_escapes(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (gw.home() / "link").symlink_to(outside)
    with pytest.raises(GatewayError):
        gw.read_bytes(gw.home() / "link")


def test_terminal_records_output_and_serves_scripted_input(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.terminal.stdin = "body\n"
    gw.terminal.width = 33
    gw.terminal.tty = {1: False, 2: False}
    assert gw.read_stdin_bounded(100) == b"body\n"
    gw.terminal.tty[0] = True
    gw.write_out("a")
    gw.write_out("b")
    gw.write_err("e")
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("ab", "e")
    assert (gw.isatty(0), gw.isatty(1), gw.isatty(7)) == (True, False, False)
    assert gw.terminal_width() == 33


def test_default_terminal_is_not_a_tty_and_has_no_input(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert gw.read_stdin_bounded(100) == b""
    assert not any(gw.isatty(fd) for fd in (0, 1, 2))


def test_random_is_deterministic_per_seed(tmp_path: Path) -> None:
    a, b = FakeGateway(tmp_path / "a", seed=1), FakeGateway(tmp_path / "b", seed=1)
    assert a.random_hex(4) == b.random_hex(4)
    assert a.random_hex(4) != FakeGateway(tmp_path / "c", seed=2).random_hex(4)
    assert len(a.random_hex(5)) == 10


def test_crontab_records_the_exact_bytes_written(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert gw.crontab_read() is None
    table = b"* * * * * echo \xff\r\n\x00no newline"
    gw.crontab_write(table)
    assert gw.crontab.writes == [table]
    assert gw.crontab_read() == table
    gw.crontab_write(b"")
    assert gw.crontab.writes == [table, b""]


def test_crontab_can_be_told_to_fail(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.crontab.fail_with = "boom"
    with pytest.raises(GatewayError, match="boom"):
        gw.crontab_write(b"x")
    assert gw.crontab.writes == []
    with pytest.raises(GatewayError, match="boom"):
        gw.crontab_read()


def test_run_is_scripted_and_recorded(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.script_run(["tool", "x"], stdout="out", returncode=4)
    result = gw.run(["tool", "x"], stdin="in", timeout=2.0)
    assert (result.stdout, result.returncode) == ("out", 4)
    assert gw.runs == [(["tool", "x"], "in", 2.0)]


def test_unscripted_run_fails_loudly_and_never_spawns(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    with pytest.raises(AssertionError, match="unscripted"):
        gw.run(["rm", "-rf", "/"])
    assert gw.runs == []


def test_fake_never_starts_a_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("fake gateway started a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    gw = FakeGateway(tmp_path)
    gw.crontab_write(b"x")
    gw.crontab_read()
    gw.script_run(["t"])
    gw.run(["t"])


def test_relative_paths_resolve_against_the_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = FakeGateway(tmp_path / "root")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    gw.write_bytes(Path("rel.txt"), b"x")
    assert (gw.root / "rel.txt").read_bytes() == b"x"
    assert not (elsewhere / "rel.txt").exists()
    assert gw.read_bytes(Path("rel.txt")) == b"x"
    with pytest.raises(GatewayError):
        gw.read_bytes(Path("../elsewhere/rel.txt"))


def test_embedded_nul_in_a_path_is_a_gateway_error(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    with pytest.raises(GatewayError, match="NUL"):
        gw.read_bytes(Path("a\0b"))
    with pytest.raises(GatewayError, match="NUL"):
        gw.write_bytes(Path("a\0b"), b"")


def test_crontab_write_rejects_non_bytes(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    with pytest.raises(TypeError):
        gw.crontab_write("text")  # type: ignore[arg-type]
    assert gw.crontab.writes == []


def test_login_name_is_scriptable_and_may_be_absent(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert gw.login_name() == "tester"
    gw.login = None
    assert gw.login_name() is None
