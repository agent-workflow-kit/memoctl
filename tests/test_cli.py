from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from memoctl import __version__
from memoctl.cli import ensure_standard_descriptors, main
from memoctl.cli_args import Command, add_global_flags, prescan_json
from memoctl.cli_errors import UsageError
from memoctl.config import ConfigError
from memoctl.gateway import Gateway, GatewayError
from memoctl.grammar import GrammarError
from tests.fake_gateway import FakeGateway

SECRET = "sk-LOCAL-SECRET-VALUE"


def _echo(args: argparse.Namespace, gw: Gateway) -> int:
    gw.write_out(f"echo json={args.json} dry={getattr(args, 'dry_run', None)}\n")
    return 0


def _boom(args: argparse.Namespace, gw: Gateway) -> int:
    secret = SECRET  # noqa: F841 - a local a traceback with locals would print
    raise RuntimeError(f"unexpected {SECRET}")


def _grammar(args: argparse.Namespace, gw: Gateway) -> int:
    raise GrammarError("topic")


def _config(args: argparse.Namespace, gw: Gateway) -> int:
    raise ConfigError("limits.max_body_bytes")


def _gateway(args: argparse.Namespace, gw: Gateway) -> int:
    raise GatewayError(f"crontab failed: {SECRET}")


def _interrupt(args: argparse.Namespace, gw: Gateway) -> int:
    raise KeyboardInterrupt


def _exit(args: argparse.Namespace, gw: Gateway) -> int:
    raise SystemExit(0)


def _label_type(text: str) -> str:
    raise GrammarError("label")


def _configure_typed(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--label", type=_label_type)


def _configure_item(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--topic", default=None)
    parser.add_argument("id", nargs="?")


def _configure_nested(parser: argparse.ArgumentParser) -> None:
    nested = parser.add_subparsers(dest="action", metavar="<action>")
    for name in ("install", "uninstall"):
        leaf = nested.add_parser(name)
        add_global_flags(leaf, dry_run=True)


COMMANDS = (
    Command("echo", "echo", _echo, _configure_item, dry_run=True),
    Command("plain", "no dry run", _echo, _configure_item),
    Command("boom", "raises", _boom),
    Command("grammar", "raises", _grammar),
    Command("config", "raises", _config),
    Command("gateway", "raises", _gateway),
    Command("interrupt", "raises", _interrupt),
    Command("exit", "raises SystemExit", _exit),
    Command("typed", "type converter raises", _echo, _configure_typed),
    Command("nested", "nested", _echo, _configure_nested, globals_on_command=False),
)


def run(
    tmp_path: Path, argv: list[str], *, gw: FakeGateway | None = None
) -> tuple[int, FakeGateway]:
    gw = gw or FakeGateway(tmp_path)
    return main(argv, gw, COMMANDS), gw


def envelope(gw: FakeGateway) -> dict[str, Any]:
    assert gw.terminal.stdout.endswith("\n")
    assert gw.terminal.stdout.count("\n") == 1
    return json.loads(gw.terminal.stdout)["error"]  # type: ignore[no-any-return]


# --- help, version, argv source --------------------------------------------------------------


def test_version_prints_on_stdout_through_the_gateway_and_exits_zero(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["--version"])
    assert (code, gw.terminal.stdout, gw.terminal.stderr) == (0, f"memoctl {__version__}\n", "")


def test_version_ignores_json_flag_after_it_is_refused_but_json_prescan_is_exempt(
    tmp_path: Path,
) -> None:
    code, gw = run(tmp_path, ["--version", "--json"])
    # Not alone: a usage error, printed as text because --version exempts the pre-scan.
    assert code == 2
    assert gw.terminal.stdout == ""
    assert gw.terminal.stderr == "usage: invalid argv\n"


def test_help_prints_usage_on_stdout_and_exits_zero(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["--help"])
    assert code == 0
    assert gw.terminal.stdout.startswith("usage: memoctl")
    assert gw.terminal.stderr == ""


def test_subcommand_help_exits_zero_and_ignores_json(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["echo", "--json", "--help"])
    assert code == 0
    assert gw.terminal.stdout.startswith("usage: memoctl echo")


def test_argv_defaults_to_the_gateway_argv(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.argv_value = ["echo", "--json"]
    assert main(None, gw, COMMANDS) == 0
    assert gw.terminal.stdout == "echo json=True dry=False\n"


def test_default_commands_table_has_only_help_and_version(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    assert main(["--version"], gw) == 0
    assert main(["add"], gw) == 2


# --- usage errors ----------------------------------------------------------------------------


def test_no_arguments_is_a_usage_error_with_a_status_line_only(tmp_path: Path) -> None:
    code, gw = run(tmp_path, [])
    assert (code, gw.terminal.stdout, gw.terminal.stderr) == (2, "", "usage: invalid argv\n")


@pytest.mark.parametrize(
    "argv",
    [
        ["nosuch"],
        ["echo", "--nosuch"],
        ["--dry-run", "echo"],
        ["--", "echo"],
        ["--help", "echo"],
        ["--version", "echo"],
        ["-h"],
        ["echo", "-h"],
        ["ech"],  # subcommand names are exact
        ["echo", "--top", "x"],  # option abbreviations are refused
        ["echo", "--quiet", "--verbose"],
        ["plain", "--dry-run"],
        ["echo", "--json=1"],
        ["echo", "a", "b"],
        ["echo", "--topic"],
        ["echo", "\udcff\udcfe", "x"],
    ],
)
def test_bad_arguments_exit_2_with_field_argv_as_text(tmp_path: Path, argv: list[str]) -> None:
    code, gw = run(tmp_path, argv)
    assert code == 2
    assert gw.terminal.stdout == ""
    assert gw.terminal.stderr == "usage: invalid argv\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "echo"],
        ["--json"],
        ["nosuch", "--json"],
        ["echo", "--json", "--nosuch"],
        ["echo", "--json", "--quiet", "--verbose"],
        ["plain", "--json", "--dry-run"],
        ["echo", "--json", "a", "b"],
    ],
)
def test_usage_errors_with_json_print_the_envelope(tmp_path: Path, argv: list[str]) -> None:
    code, gw = run(tmp_path, argv)
    assert code == 2
    assert envelope(gw) == {
        "code": "usage",
        "message": "invalid argv",
        "reason": None,
        "details": {"field": "argv"},
        "retryable": False,
    }
    assert gw.terminal.stderr == "usage: invalid argv\n"


def test_json_after_double_dash_is_not_a_flag(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["nosuch", "--", "--json"])
    assert code == 2
    assert gw.terminal.stdout == ""


def test_prescan_semantics() -> None:
    assert prescan_json(["x", "--json"])
    assert not prescan_json(["x", "--", "--json"])
    assert not prescan_json(["--json=1"])
    assert not prescan_json(["--jso"])
    assert not prescan_json(["--help", "--json"])
    assert not prescan_json(["--json", "--version"])
    assert not prescan_json([])


def test_double_dash_ends_options_and_a_lone_dash_is_a_value(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["echo", "--json", "--", "-x"])
    assert (code, gw.terminal.stdout) == (0, "echo json=True dry=False\n")
    code, gw = run(tmp_path, ["echo", "--topic", "-"])
    assert code == 0
    code, gw = run(tmp_path, ["echo", "--topic=-x"])
    assert code == 0


def test_global_flags_parse_after_the_subcommand(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["echo", "--dry-run", "--json", "--actor", "human:x", "--quiet"])
    assert (code, gw.terminal.stdout) == (0, "echo json=True dry=True\n")


def test_nested_command_takes_global_flags_after_the_leaf_only(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["nested", "install", "--json", "--dry-run"])
    assert code == 0
    code, gw = run(tmp_path, ["nested", "--json", "install"])
    assert code == 2


def test_hostile_and_huge_argv_never_echo(tmp_path: Path) -> None:
    for token in ("\x1b[31mred", "x" * 200_000, "\udcff", "a\0b", "‮"):
        code, gw = run(tmp_path, ["echo", "--json", "--bogus", token])
        assert code == 2
        assert token not in gw.terminal.stdout + gw.terminal.stderr
        assert gw.terminal.stdout.isascii()


# --- mapped and unmapped failures ------------------------------------------------------------


def test_grammar_error_is_usage_with_the_field(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["grammar", "--json"])
    assert code == 2
    assert envelope(gw) == {
        "code": "usage",
        "message": "invalid topic",
        "reason": None,
        "details": {"field": "topic"},
        "retryable": False,
    }
    assert gw.terminal.stderr == "usage: invalid topic\n"


def test_config_error_is_config_invalid_exit_2(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["config", "--json"])
    assert code == 2
    assert envelope(gw)["code"] == "config_invalid"
    assert envelope(gw)["details"] == {"field": "limits.max_body_bytes"}
    assert gw.terminal.stderr == "config_invalid: invalid configuration\n"


def test_config_error_plain_has_no_stdout(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["config"])
    assert (code, gw.terminal.stdout) == (2, "")


def test_config_error_field_is_ascii_escaped_in_json(tmp_path: Path) -> None:
    def hostile(args: argparse.Namespace, gw: Gateway) -> int:
        raise ConfigError("/home/‮é/\x1b[0m.yaml")

    gw = FakeGateway(tmp_path)
    assert main(["h", "--json"], gw, (Command("h", "h", hostile),)) == 2
    assert gw.terminal.stdout.isascii()
    assert "\x1b" not in gw.terminal.stdout + gw.terminal.stderr
    assert "‮" not in gw.terminal.stdout + gw.terminal.stderr


def test_unregistered_exception_is_internal_without_text_or_traceback(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["boom", "--json"])
    assert code == 1
    assert envelope(gw) == {
        "code": "internal",
        "message": "internal error",
        "reason": None,
        "details": None,
        "retryable": False,
    }
    out = gw.terminal.stdout + gw.terminal.stderr
    assert SECRET not in out
    assert "Traceback" not in out
    assert gw.terminal.stderr == "internal: internal error\n"


def test_gateway_error_is_internal_and_never_echoes_its_message(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["gateway"])
    assert code == 1
    assert SECRET not in gw.terminal.stderr


def test_keyboard_interrupt_exits_130_with_no_output(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["interrupt", "--json"])
    assert (code, gw.terminal.stdout, gw.terminal.stderr) == (130, "", "")


def test_usage_error_raised_by_a_command_is_mapped(tmp_path: Path) -> None:
    def refuse(args: argparse.Namespace, gw: Gateway) -> int:
        raise UsageError("text")

    gw = FakeGateway(tmp_path)
    assert main(["r"], gw, (Command("r", "r", refuse),)) == 2
    assert gw.terminal.stderr == "usage: invalid text\n"


def test_a_usage_field_that_is_not_an_identifier_is_internal(tmp_path: Path) -> None:
    def refuse(args: argparse.Namespace, gw: Gateway) -> int:
        raise UsageError("bad field\x1b")

    gw = FakeGateway(tmp_path)
    assert main(["r"], gw, (Command("r", "r", refuse),)) == 1
    assert gw.terminal.stderr == "internal: internal error\n"


# --- descriptors -----------------------------------------------------------------------------


def test_open_descriptors_are_left_alone(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    ensure_standard_descriptors(gw)
    assert gw.fd_calls == [("fstat", 0), ("fstat", 1), ("fstat", 2)]


def test_each_closed_descriptor_gets_dev_null_in_ascending_order(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    gw.fds.clear()
    ensure_standard_descriptors(gw)
    assert gw.fds == {0: "r", 1: "w", 2: "w"}
    assert [c for c in gw.fd_calls if c[0] == "open"] == [("open", 0), ("open", 1), ("open", 2)]


def test_a_misplaced_open_is_moved_with_dup2_and_the_extra_closed(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    del gw.fds[1]
    gw.next_free_fd = 5  # the open lands on 5, not 1
    ensure_standard_descriptors(gw)
    assert gw.fds == {0: "r", 1: "w", 2: "w"}
    assert ("dup2", 5, 1) in gw.fd_calls
    assert ("close", 5) in gw.fd_calls


def test_descriptor_check_runs_before_anything_else_and_failure_is_survivable(
    tmp_path: Path,
) -> None:
    gw = FakeGateway(tmp_path)
    gw.fds.clear()
    gw.devnull_fails = True
    assert main(["--version"], gw, COMMANDS) == 0
    assert gw.fd_calls[0] == ("fstat", 0)


# --- review round 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["exit"], ["exit", "--json"]])
def test_system_exit_from_a_command_is_internal_without_text(
    tmp_path: Path, argv: list[str]
) -> None:
    code, gw = run(tmp_path, argv)
    assert code == 1
    assert gw.terminal.stderr == "internal: internal error\n"
    if "--json" in argv:
        assert envelope(gw)["code"] == "internal"
    else:
        assert gw.terminal.stdout == ""


def test_keyboard_interrupt_before_dispatch_exits_130(tmp_path: Path) -> None:
    class Interrupted(FakeGateway):
        def argv(self) -> list[str]:
            raise KeyboardInterrupt

    gw = Interrupted(tmp_path)
    assert main(None, gw, COMMANDS) == 130
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "")


def test_keyboard_interrupt_in_the_descriptor_check_exits_130(tmp_path: Path) -> None:
    class Interrupted(FakeGateway):
        def fd_is_open(self, fd: int) -> bool:
            raise KeyboardInterrupt

    gw = Interrupted(tmp_path)
    assert main(["--version"], gw, COMMANDS) == 130


def test_a_type_converter_that_raises_grammar_error_keeps_its_field(tmp_path: Path) -> None:
    code, gw = run(tmp_path, ["typed", "--json", "--label", "x"])
    assert code == 2
    assert envelope(gw)["details"] == {"field": "label"}
    assert gw.terminal.stderr == "usage: invalid label\n"


@pytest.mark.parametrize("columns", ["20", "200"])
def test_help_layout_does_not_depend_on_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, columns: str
) -> None:
    monkeypatch.setenv("COLUMNS", "20")
    _, narrow = run(tmp_path, ["--help"])
    monkeypatch.setenv("COLUMNS", columns)
    _, other = run(tmp_path, ["--help"])
    assert narrow.terminal.stdout == other.terminal.stdout
    assert max(len(line) for line in other.terminal.stdout.splitlines()) <= 80
    _, sub_narrow = run(tmp_path, ["echo", "--help"])
    monkeypatch.setenv("COLUMNS", "20")
    _, sub_other = run(tmp_path, ["echo", "--help"])
    assert sub_narrow.terminal.stdout == sub_other.terminal.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["--version"],
        ["-h"],
        ["@file"],
        ["echo", "@file"],
        ["echo", "-h"],
        ["echo", "--help"],
        ["ech"],
        ["echo", "--top", "x"],
        ["--he"],
        ["echo", "--hel"],
        ["--nosuch"],
    ],
)
def test_argparse_itself_never_prints(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    from memoctl.cli_args import ParserExit, build_parser

    try:
        build_parser(COMMANDS).parse_args(argv)
    except (ParserExit, UsageError):
        pass
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")
    run(tmp_path, argv)  # and through main, whose output goes only to the gateway
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_argparse_seam_check_names_a_missing_member() -> None:
    from memoctl.cli_args import check_argparse_seam

    class Old:
        def error(self) -> None: ...
        def exit(self) -> None: ...

    with pytest.raises(ImportError, match="_print_message"):
        check_argparse_seam(Old)
    check_argparse_seam(argparse.ArgumentParser)


def test_version_matches_the_project_version() -> None:
    import tomllib

    import memoctl

    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert memoctl.__version__ == project
