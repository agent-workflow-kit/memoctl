"""``--verbose`` phase lines (interface.md, Output): one ``verbose (<phase>): <n> ms`` line per
phase on stderr, after the result, for every command; never a change to stdout or the exit code."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from memoctl import phases
from memoctl.cli import main
from memoctl.cron_block import block_bytes
from tests.fake_gateway import FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_context import REPO as CONTEXT_REPO
from tests.test_cmd_context import Store as ContextStore
from tests.test_cmd_cron import HOME as CRON_HOME
from tests.test_cmd_cron import _absolute_paths_outside_the_root_exist_as_directories
from tests.test_cmd_read import REPO, seed

LINE = re.compile(r"verbose \(([a-z][a-z_]*)\): (0|[1-9][0-9]*) ms\n")
SECRET = "BODY-MARKER-9f3a"
_COUNTER = itertools.count()


def names(stderr: str) -> list[str]:
    """The phase names of the trailing run of verbose lines."""
    found = [m.group(1) for m in LINE.finditer(stderr)]
    assert "".join(m.group(0) for m in LINE.finditer(stderr)) == stderr[stderr.find("verbose (") :]
    return found


# -- scenarios: each seeds a fresh gateway and returns (argv, stdin) ----------------------------


def _notes(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body=SECRET), dict(id=2, expires=1)]).close()


def s_list(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["list", "--all"], None


def s_list_empty(gw: FakeGateway) -> tuple[list[str], str | None]:
    return ["list"], None


def s_show(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["show", "1"], None


def s_search(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["search", "BODY-MARKER"], None


def s_context(gw: FakeGateway) -> tuple[list[str], str | None]:
    ContextStore(gw).add(body=SECRET)
    return ["context", "--repo", CONTEXT_REPO], None


def s_context_no_repo(gw: FakeGateway) -> tuple[list[str], str | None]:
    return ["context"], None


def s_validate(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["validate"], None


def s_add(gw: FakeGateway) -> tuple[list[str], str | None]:
    return ["add", "--repo", REPO, "--topic", "t", "--body-file", "-"], SECRET


def s_add_dry(gw: FakeGateway) -> tuple[list[str], str | None]:
    return [*s_add(gw)[0], "--dry-run"], SECRET


def s_delete_yes(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["delete", "1", "--yes"], None


def s_delete_prompt(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    gw.terminal.tty_answer = "y"
    return ["delete", "1"], None


def s_prune(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["prune"], None


def s_prune_dry(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["prune", "--dry-run"], None


def s_prune_empty(gw: FakeGateway) -> tuple[list[str], str | None]:
    return ["prune"], None


def s_cron_install(gw: FakeGateway) -> tuple[list[str], str | None]:
    gw._home = CRON_HOME
    _absolute_paths_outside_the_root_exist_as_directories(gw)
    return ["cron", "install"], None


def s_cron_unchanged(gw: FakeGateway) -> tuple[list[str], str | None]:
    s_cron_install(gw)
    gw.crontab.table = block_bytes(str(CRON_HOME))
    return ["cron", "install"], None


def s_cron_dry(gw: FakeGateway) -> tuple[list[str], str | None]:
    return [*s_cron_install(gw)[0], "--dry-run"], None


Scenario = Callable[[FakeGateway], tuple[list[str], str | None]]
CASES: dict[str, tuple[Scenario, list[str]]] = {
    "list": (s_list, ["open_store", "scan", "output"]),
    "list-empty": (s_list_empty, ["open_store", "scan", "output"]),
    "show": (s_show, ["open_store", "read", "output"]),
    "search": (s_search, ["parse", "open_store", "scan", "output"]),
    "context": (s_context, ["config", "open_store", "select", "render", "output"]),
    "context-no-repo": (s_context_no_repo, ["render", "output"]),
    "validate": (s_validate, ["validate", "output"]),
    "add": (
        s_add,
        [
            "parse",
            "config",
            "body",
            "secret_check",
            "clock",
            "open_store",
            "transaction",
            "log",
            "output",
        ],
    ),  # fmt: skip
    "add-dry": (
        s_add_dry,
        ["parse", "config", "body", "secret_check", "clock", "output"],
    ),
    "delete": (s_delete_yes, ["parse", "lookup", "transaction", "log", "output"]),
    "delete-prompt": (
        s_delete_prompt,
        ["parse", "lookup", "prompt", "transaction", "log", "output"],
    ),
    "prune": (s_prune, ["parse", "open_store", "transaction", "log", "output"]),
    "prune-dry": (s_prune_dry, ["parse", "select", "output"]),
    "prune-empty": (s_prune_empty, ["parse", "output"]),
    "cron-install": (
        s_cron_install,
        ["home", "read_crontab", "plan", "write_crontab", "output"],
    ),
    "cron-unchanged": (s_cron_unchanged, ["home", "read_crontab", "plan", "output"]),
    "cron-dry": (s_cron_dry, ["home", "read_crontab", "plan", "output"]),
}


def go(tmp_path: Path, case: str, *extra: str) -> tuple[int, str, str]:
    gw = FakeGateway(tmp_path / f"gw-{next(_COUNTER)}")
    argv, stdin = CASES[case][0](gw)
    if stdin is not None:
        gw.terminal.stdin = stdin
    gw.terminal.stdout = gw.terminal.stderr = ""
    code = main([*argv, *extra], gw)
    return code, gw.terminal.stdout, gw.terminal.stderr


@pytest.mark.parametrize("case", CASES)
def test_phase_lines_follow_the_result_and_change_nothing_else(tmp_path: Path, case: str) -> None:
    plain = go(tmp_path, case)
    for flags in (("--verbose",), ("--verbose", "--json")):
        base = plain if flags == ("--verbose",) else go(tmp_path, case, "--json")
        code, out, err = go(tmp_path, case, *flags)
        assert (code, out) == (base[0], base[1])  # exit code and stdout bytes
        assert err.startswith(base[2])  # nothing before or inside the existing stderr
        assert names(err) == CASES[case][1]
        assert err[len(base[2]) :].startswith("verbose (")
        assert SECRET not in err
        assert str(tmp_path) not in err


@pytest.mark.parametrize("case", ["list", "context", "add", "delete", "prune", "cron-install"])
def test_stderr_golden(tmp_path: Path, case: str) -> None:
    _, _, err = go(tmp_path, case, "--verbose")
    check_golden(f"verbose/{case}.txt", err[err.find("verbose (") :].encode())


# -- unit: the recorder ------------------------------------------------------------------------


def test_elapsed_milliseconds_come_from_the_monotonic_clock(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with phases.Phases(gw, enabled=True) as rec:
        gw.advance_mono(0.0123)
        phases.mark("one")
        phases.mark("two")
        gw.advance_mono(2)
        phases.mark("three")
        rec.flush()
    assert gw.terminal.stderr == (
        "verbose (one): 12 ms\nverbose (two): 0 ms\nverbose (three): 2000 ms\n"
    )


def test_disabled_records_and_prints_nothing(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with phases.Phases(gw, enabled=False) as rec:
        phases.mark("one")
        phases.mark("Not Valid")  # not even checked: the path is inert
        rec.flush()
    assert gw.terminal.stderr == ""


@pytest.mark.parametrize("bad", ["", "a b", "A", "a\x1b[2J", "a\n", "1a", "a-b", "é", "x: 1"])
def test_a_phase_name_is_a_fixed_identifier(tmp_path: Path, bad: str) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with phases.Phases(gw, enabled=True):
        with pytest.raises(ValueError):
            phases.mark(bad)


def test_a_backwards_clock_never_prints_a_negative_number(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with phases.Phases(gw, enabled=True) as rec:
        gw.mono -= 5
        phases.mark("one")
        rec.flush()
    assert gw.terminal.stderr == "verbose (one): 0 ms\n"


def test_no_recorder_installed_after_the_run(tmp_path: Path) -> None:
    go(tmp_path, "list", "--verbose")
    assert isinstance(phases.current(), type(phases._INERT))


def test_a_failing_stderr_write_changes_nothing(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")

    def boom(text: str, *, bound: float | None = None) -> None:
        raise OSError

    gw.write_err = boom  # type: ignore[method-assign]
    with phases.Phases(gw, enabled=True) as rec:
        phases.mark("one")
        rec.flush()  # must not raise


# -- failures, hostile positions ---------------------------------------------------------------


def test_a_failure_prints_its_error_line_alone(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["show", "7", "--verbose"], gw) == 10
    assert gw.terminal.stderr == "not_found: note not found\n"
    assert gw.terminal.stdout == ""


def test_a_failure_with_json_prints_the_envelope_and_no_phase_lines(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["show", "7", "--verbose", "--json"], gw) == 10
    assert json.loads(gw.terminal.stdout)["error"]["code"] == "not_found"
    assert "verbose" not in gw.terminal.stderr


def test_a_refused_add_prints_no_phase_lines(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdin = "password = hunter2hunter2hunter2\n-----BEGIN PRIVATE KEY-----\n"
    gw.terminal.stdout = gw.terminal.stderr = ""
    code = main(["add", "--repo", REPO, "--topic", "t", "--body-file", "-", "--verbose"], gw)
    assert code != 0
    assert "verbose" not in gw.terminal.stderr


def test_repeated_flag_prints_one_set_of_lines(tmp_path: Path) -> None:
    _, _, once = go(tmp_path, "list", "--verbose")
    _, _, twice = go(tmp_path, "list", "--verbose", "--verbose")
    assert names(twice) == names(once)


def test_after_double_dash_it_is_a_positional_not_the_flag(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["search", "--", "--verbose"], gw) == 0
    assert "verbose (" not in gw.terminal.stderr


def test_before_the_subcommand_is_refused(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    assert main(["--verbose", "list"], gw) == 2


def test_with_fence_the_block_is_unchanged_and_stderr_has_the_lines(tmp_path: Path) -> None:
    def fenced(*extra: str) -> tuple[int, str, str]:
        gw = FakeGateway(tmp_path / f"f{next(_COUNTER)}")
        ContextStore(gw).add(body=SECRET)
        gw.terminal.stdout = gw.terminal.stderr = ""
        code = main(["context", "--repo", CONTEXT_REPO, "--fence", *extra], gw)
        return code, gw.terminal.stdout, gw.terminal.stderr

    plain, verbose = fenced(), fenced("--verbose")
    assert plain[0] == verbose[0] == 0
    # The nonce differs per call; everything else is the same.
    strip = lambda text: re.sub(r"[0-9a-f]{16}", "N", text)  # noqa: E731
    assert strip(plain[1]) == strip(verbose[1])
    assert names(verbose[2]) == CASES["context"][1]


def test_context_exit_10_still_prints_the_lines_with_empty_stdout(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["context", "--repo", "example.com/none/none", "--verbose"], gw) == 10
    assert gw.terminal.stdout == ""
    assert "verbose (render)" in gw.terminal.stderr


def test_quiet_with_verbose_is_still_usage(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["list", "--verbose", "--quiet"], gw) == 2
    assert "verbose (" not in gw.terminal.stderr


# -- the real entry point ----------------------------------------------------------------------


def test_real_process_keeps_stdout_and_puts_real_millisecond_lines_on_stderr(
    tmp_path: Path,
) -> None:
    from tests.test_cmd_delete_process import add, memoctl  # a private home, a real child

    home = tmp_path / "home"
    home.mkdir()
    add(home, SECRET)
    plain = memoctl(home, "list", "--json")
    loud = memoctl(home, "list", "--json", "--verbose")
    assert (loud.returncode, loud.stdout) == (plain.returncode, plain.stdout) == (0, plain.stdout)
    assert names(loud.stderr.decode()) == ["open_store", "scan", "output"]
    assert SECRET.encode() not in loud.stderr
    context = memoctl(home, "context", "--repo", REPO, "--verbose")
    assert names(context.stderr.decode())[-2:] == ["render", "output"]
