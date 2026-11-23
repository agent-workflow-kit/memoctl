from __future__ import annotations

from pathlib import Path

import pytest

from memoctl.cli import main
from tests.fake_gateway import FakeGateway


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "memoctl 0.0.0"


def test_no_arguments_returns_usage_exit_code() -> None:
    assert main([]) == 2


def test_usage_goes_to_standard_error_through_the_gateway(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert main([], gw) == 2
    assert gw.terminal.stderr.startswith("usage: memoctl")
    assert gw.terminal.stdout == ""
