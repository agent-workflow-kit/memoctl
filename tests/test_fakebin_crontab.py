"""The fake ``crontab`` script records its input and exit status."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _run(script: str, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([script, *args], input=stdin, capture_output=True, check=False)


def test_records_exact_stdin_bytes_and_exit_status(fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    table = b"\x00\xff\r\n* * * * * x"
    assert _run(script, "-", stdin=table).returncode == 0
    assert (state / "input.1").read_bytes() == table
    assert (state / "crontab").read_bytes() == table
    assert (state / "calls.log").read_text() == "1 - exit=0\n"


def test_list_shows_stored_table_or_fails_when_none(fake_crontab: tuple[str, Path]) -> None:
    script, _ = fake_crontab
    missing = _run(script, "-l")
    assert missing.returncode == 1
    assert b"no crontab" in missing.stderr
    _run(script, "-", stdin=b"t\n")
    assert _run(script, "-l").stdout == b"t\n"


def test_forced_failure_records_but_does_not_store(
    fake_crontab: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, state = fake_crontab
    monkeypatch.setenv("FAKE_CRONTAB_EXIT", "7")
    assert _run(script, "-", stdin=b"x").returncode == 7
    assert (state / "input.1").read_bytes() == b"x"
    assert not (state / "crontab").exists()
    assert (state / "calls.log").read_text() == "1 - exit=7\n"


def test_refuses_to_run_without_a_state_directory(
    fake_crontab: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = fake_crontab
    monkeypatch.delenv("FAKE_CRONTAB_DIR")
    result = _run(script, "-", stdin=b"x")
    assert result.returncode == 99


def test_unsupported_arguments_fail(fake_crontab: tuple[str, Path]) -> None:
    script, _ = fake_crontab
    assert _run(script, "-r").returncode == 64
    assert os.access(script, os.X_OK)
