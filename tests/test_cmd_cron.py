"""``memoctl cron install|uninstall`` end to end through ``main`` on a fake gateway whose
crontab records every write. The real ``crontab`` is never run (tests/conftest.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoctl.cli import main
from memoctl.cron_block import block_bytes
from memoctl.gateway import GatewayError
from tests.fake_gateway import FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import validate

HOME = Path("/home/example")
BLOCK = block_bytes(str(HOME))
OTHER = b"MAILTO=me@example.com\n0 * * * * /usr/bin/true\n"
SECRET = "SECRET-TABLE-TEXT"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    gateway = FakeGateway(tmp_path / "gw")
    gateway._home = HOME
    return gateway


def run(gw: FakeGateway, *argv: str) -> int:
    gw.terminal.stdout = gw.terminal.stderr = ""
    return main(["cron", *argv], gw)


# --- install: one golden per outcome ----------------------------------------------------------


def test_install_into_nothing(gw: FakeGateway) -> None:
    assert run(gw, "install", "--json") == 0
    assert gw.crontab.writes == [BLOCK]
    check_golden("cron/installed-table.bin", gw.crontab.writes[0])
    check_golden("cron/installed.json", gw.terminal.stdout.encode(), schema="cli/cron-install")
    assert gw.terminal.stderr == ""


def test_install_appends_after_other_entries(gw: FakeGateway) -> None:
    gw.crontab.table = OTHER[:-1]  # no trailing LF
    assert run(gw, "install") == 0
    check_golden("cron/appended-table.bin", gw.crontab.writes[0])
    check_golden("cron/installed.txt", gw.terminal.stdout.encode())


def test_install_replaces_a_stale_block_in_place(gw: FakeGateway) -> None:
    gw.crontab.table = OTHER + block_bytes("/home/old") + b"# tail\n"
    assert run(gw, "install", "--json") == 0
    check_golden("cron/replaced-table.bin", gw.crontab.writes[0])
    check_golden("cron/replaced.json", gw.terminal.stdout.encode(), schema="cli/cron-install")


def test_install_unchanged_writes_nothing(gw: FakeGateway) -> None:
    gw.crontab.table = OTHER + BLOCK
    assert run(gw, "install", "--json") == 0
    assert gw.crontab.writes == []
    check_golden("cron/unchanged.json", gw.terminal.stdout.encode(), schema="cli/cron-install")


def test_install_twice_is_byte_identical(gw: FakeGateway) -> None:
    assert run(gw, "install") == 0
    first = gw.crontab.table
    assert run(gw, "install") == 0
    assert gw.crontab.table == first and len(gw.crontab.writes) == 1
    assert gw.terminal.stdout == "cron block unchanged\n"


# --- uninstall --------------------------------------------------------------------------------


def test_uninstall_removes_only_the_block(gw: FakeGateway) -> None:
    gw.crontab.table = OTHER + BLOCK + b"# tail\n"
    assert run(gw, "uninstall", "--json") == 0
    assert gw.crontab.writes == [OTHER + b"# tail\n"]
    check_golden("cron/removed-table.bin", gw.crontab.writes[0])
    check_golden("cron/removed.json", gw.terminal.stdout.encode(), schema="cli/cron-uninstall")


def test_uninstall_the_only_block_writes_an_empty_table(gw: FakeGateway) -> None:
    gw.crontab.table = BLOCK
    assert run(gw, "uninstall") == 0
    assert gw.crontab.writes == [b""]
    assert gw.terminal.stdout == "cron block removed\n"


def test_uninstall_reports_a_hand_edited_middle_line(gw: FakeGateway) -> None:
    gw.crontab.table = b"# memoctl managed\n5 5 * * * echo \xff\n# memoctl managed end\n"
    assert run(gw, "uninstall", "--json") == 0
    out = json.loads(gw.terminal.stdout)
    validate("cli/cron-uninstall", out)
    assert out["block"][1] == "5 5 * * * echo �"


def test_uninstall_long_middle_line_is_capped_for_the_schema(gw: FakeGateway) -> None:
    gw.crontab.table = b"# memoctl managed\n" + b"x" * 10000 + b"\n# memoctl managed end\n"
    assert run(gw, "uninstall", "--json") == 0
    validate("cli/cron-uninstall", json.loads(gw.terminal.stdout))
    assert gw.crontab.writes == [b""]


@pytest.mark.parametrize("table", [None, b"", OTHER], ids=["none", "empty", "other"])
def test_uninstall_absent_is_unchanged_and_writes_nothing(
    gw: FakeGateway, table: bytes | None
) -> None:
    gw.crontab.table = table
    assert run(gw, "uninstall", "--json") == 0
    assert gw.crontab.writes == []
    check_golden("cron/uninstall-unchanged.json", gw.terminal.stdout.encode(),
                 schema="cli/cron-uninstall")  # fmt: skip


# --- dry run ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "table", "name"),
    [
        ("install", None, "dry-installed"),
        ("install", OTHER + block_bytes("/home/old"), "dry-replaced"),
        ("install", BLOCK, "dry-unchanged"),
        ("uninstall", OTHER + BLOCK, "dry-removed"),
        ("uninstall", OTHER, "dry-uninstall-unchanged"),
    ],
    ids=lambda v: v if isinstance(v, str) else "table",
)
def test_dry_run_reads_and_never_writes(
    gw: FakeGateway, command: str, table: bytes | None, name: str
) -> None:
    gw.crontab.table = table
    assert run(gw, command, "--dry-run", "--json") == 0
    assert gw.crontab.writes == [] and gw.crontab.table == table
    schema = "cli/cron-install" if command == "install" else "cli/cron-uninstall"
    check_golden(f"cron/{name}.json", gw.terminal.stdout.encode(), schema=schema)
    assert run(gw, command, "--dry-run") == 0
    check_golden(f"cron/{name}.txt", gw.terminal.stdout.encode())


def test_dry_run_on_a_malformed_block_still_refuses(gw: FakeGateway) -> None:
    gw.crontab.table = b"# memoctl managed\n"
    assert run(gw, "install", "--dry-run") == 20


# --- output flags -----------------------------------------------------------------------------


def test_quiet_prints_nothing_on_success(gw: FakeGateway) -> None:
    assert run(gw, "install", "--quiet") == 0
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "")
    assert gw.crontab.writes == [BLOCK]


def test_actor_is_validated_like_every_command(gw: FakeGateway) -> None:
    assert run(gw, "install", "--actor", "bad actor!") == 2
    assert gw.crontab.writes == []


@pytest.mark.parametrize(
    "argv",
    [
        [], ["--json", "install"], ["install", "extra"], ["status"], ["install", "--bogus"],
        ["install", "--quiet", "--verbose"], ["uninstall", "--dry-run=1"], ["inst"],
    ],
)  # fmt: skip
def test_usage_errors(gw: FakeGateway, argv: list[str]) -> None:
    assert run(gw, *argv) == 2
    assert gw.crontab.writes == [] and gw.crontab.table is None


def test_help_lists_both_actions(gw: FakeGateway) -> None:
    assert run(gw, "--help") == 0
    assert "install" in gw.terminal.stdout and "uninstall" in gw.terminal.stdout
    assert gw.crontab.writes == []


# --- refusals and failures ---------------------------------------------------------------------


def test_malformed_block_exits_20_changes_nothing_and_never_echoes_the_table(
    gw: FakeGateway,
) -> None:
    gw.crontab.table = b"# memoctl managed \n" + SECRET.encode() + b"\n"
    for command in ("install", "uninstall"):
        assert run(gw, command, "--json") == 20
        error = json.loads(gw.terminal.stdout)["error"]
        assert (error["code"], error["reason"], error["details"]) == (
            "refused", "cron_block_malformed", None)  # fmt: skip
        assert SECRET not in gw.terminal.stdout + gw.terminal.stderr
        assert gw.terminal.stderr.startswith("refused (cron_block_malformed)")
    assert gw.crontab.writes == []


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_a_failing_crontab_exits_12_without_a_claim_of_success(
    gw: FakeGateway, command: str, flags: list[str]
) -> None:
    gw.crontab.fail_with = SECRET
    assert run(gw, command, "--json", *flags) == 12
    error = json.loads(gw.terminal.stdout)["error"]
    assert (error["code"], error["reason"], error["retryable"]) == (
        "failed",
        "crontab_failed",
        False,
    )
    assert "outcome" not in gw.terminal.stdout
    assert SECRET not in gw.terminal.stdout + gw.terminal.stderr
    assert gw.crontab.writes == []


def test_a_failing_write_exits_12_after_a_successful_read(gw: FakeGateway) -> None:
    def boom(data: bytes) -> None:
        raise GatewayError(f"crontab - failed: {SECRET}")

    gw.crontab.table = OTHER
    gw.crontab_write = boom  # type: ignore[method-assign]
    assert run(gw, "install") == 12
    assert gw.terminal.stdout == ""
    assert gw.terminal.stderr.startswith("failed (crontab_failed)")
    assert SECRET not in gw.terminal.stderr
    assert gw.crontab.table == OTHER


@pytest.mark.parametrize(
    "home",
    [
        "/home/a b",
        "/home/a%b",
        "/home/a\nb",
        "/home/$x",
        "/home/`x`",
        "/home/'x",
        "relative",
        "",
        "/home/é",
    ],
)
def test_an_unsafe_home_is_config_invalid_before_the_crontab_is_read(
    gw: FakeGateway, home: str
) -> None:
    gw._home = Path(home)
    gw.crontab.fail_with = "must not be read"
    assert run(gw, "install", "--json") == 2
    error = json.loads(gw.terminal.stdout)["error"]
    assert (error["code"], error["details"]) == ("config_invalid", {"field": "home"})
    assert gw.crontab.writes == []


def test_uninstall_does_not_need_a_safe_home(gw: FakeGateway) -> None:
    gw._home = Path("/home/a b")
    gw.crontab.table = BLOCK
    assert run(gw, "uninstall") == 0


def test_a_user_missing_from_the_password_database_is_config_invalid(gw: FakeGateway) -> None:
    def no_home() -> Path:
        raise GatewayError("the user is not in the password database")

    gw.home = no_home  # type: ignore[method-assign]
    assert run(gw, "install", "--json") == 2
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"field": "home"}


# --- the order: the deadline is disarmed before any output ------------------------------------


def test_the_deadline_is_disarmed_after_the_write_and_before_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_write = gw.crontab_write
    real_out = gw.write_out

    def write(data: bytes) -> None:
        events.append("write")
        real_write(data)

    def out(*args: object, **kwargs: object) -> object:
        events.append("out")
        return real_out(*args, **kwargs)  # type: ignore[arg-type]

    from memoctl import cmd_cron

    class Spy:
        def disarm(self) -> None:
            events.append("disarm")

    monkeypatch.setattr(cmd_cron.deadline, "current", lambda: Spy())
    gw.crontab_write = write  # type: ignore[method-assign]
    gw.write_out = out  # type: ignore[method-assign]
    assert run(gw, "install") == 0
    assert events == ["write", "disarm", "out"]
