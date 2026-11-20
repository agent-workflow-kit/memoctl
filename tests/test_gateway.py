"""The real gateway, exercised only where it cannot reach the user's home or crontab."""

from __future__ import annotations

import io
import os
import pwd
import sys
from datetime import UTC
from pathlib import Path

import pytest

from memoctl import gateway
from memoctl.gateway import GatewayError, RealGateway
from tests.conftest import CrontabGuardError


def test_run_decodes_output_as_utf8_with_surrogateescape() -> None:
    result = gateway.run(["/bin/sh", "-c", "printf 'caf\\303\\251 \\377'"])
    assert result.stdout == "café \udcff"


def test_run_passes_stdin_without_a_shell() -> None:
    result = gateway.run(["/bin/cat"], stdin="a; echo $HOME `x`\n")
    assert result.stdout == "a; echo $HOME `x`\n"


def test_run_returns_nonzero_status_without_raising() -> None:
    assert gateway.run(["/bin/sh", "-c", "exit 3"]).returncode == 3


def test_run_stdin_round_trips_surrogates() -> None:
    assert gateway.run(["/bin/cat"], stdin="\udcff").stdout == "\udcff"


def test_now_is_timezone_aware_utc() -> None:
    now = RealGateway().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


def test_home_comes_from_the_password_database_not_the_environment() -> None:
    assert os.environ["HOME"] != pwd.getpwuid(os.getuid()).pw_dir
    assert RealGateway().home() == Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_random_hex_has_requested_length_and_varies() -> None:
    gw = RealGateway()
    first, second = gw.random_hex(8), gw.random_hex(8)
    assert len(first) == 16
    assert first != second


def test_random_hex_rejects_non_positive_length() -> None:
    with pytest.raises(ValueError):
        RealGateway().random_hex(0)


def test_files_round_trip_exact_bytes(tmp_path: Path) -> None:
    gw = RealGateway()
    data = b"\x00\xff no newline"
    gw.write_bytes(tmp_path / "f", data)
    assert gw.read_bytes(tmp_path / "f") == data


def test_stdio_goes_through_the_process_descriptors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("in\n"))
    gw = RealGateway()
    assert gw.stdin_read() == "in\n"
    gw.write_out("o")
    gw.write_err("e")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("o", "e")


def test_terminal_width_has_a_positive_fallback() -> None:
    assert RealGateway().terminal_width() > 0


def test_crontab_write_sends_exact_bytes(fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    table = b"* * * * * echo \xff\n# no trailing newline"
    RealGateway(crontab_bin=script).crontab_write(table)
    assert (state / "input.1").read_bytes() == table
    assert (state / "calls.log").read_text() == "1 - exit=0\n"


def test_crontab_read_returns_stored_table(fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    gw = RealGateway(crontab_bin=script)
    gw.crontab_write(b"0 0 * * * x\n")
    assert gw.crontab_read() == b"0 0 * * * x\n"


def test_crontab_read_is_none_when_no_table_exists(fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    assert RealGateway(crontab_bin=script).crontab_read() is None


def test_crontab_failure_raises_with_status_and_stderr(
    fake_crontab: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, state = fake_crontab
    monkeypatch.setenv("FAKE_CRONTAB_EXIT", "5")
    with pytest.raises(GatewayError, match="exit 5"):
        RealGateway(crontab_bin=script).crontab_write(b"x\n")
    assert not (state / "crontab").exists()


def test_default_crontab_is_refused_by_the_guard() -> None:
    with pytest.raises(CrontabGuardError):
        RealGateway().crontab_read()


def test_a_crontab_outside_the_test_tmp_dir_is_refused() -> None:
    with pytest.raises(CrontabGuardError):
        gateway.run(["/usr/bin/crontab", "-l"])
    with pytest.raises(CrontabGuardError):
        RealGateway(crontab_bin="crontab").crontab_read()
