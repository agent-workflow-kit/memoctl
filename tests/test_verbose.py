"""``--verbose`` phase lines (interface.md, Output): one ``verbose (<phase>): <n> ms`` line per
phase on stderr, after the result, for every command; never a change to stdout or the exit code."""

from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from memoctl import cmd_add, deadline, phases
from memoctl.cli import main
from memoctl.cron_block import block_bytes
from memoctl.logfile import LogError
from memoctl.statedir import open_state_dir
from tests.fake_gateway import FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_context import REPO as CONTEXT_REPO
from tests.test_cmd_context import Store as ContextStore
from tests.test_cmd_cron import BLOCK as CRON_BLOCK
from tests.test_cmd_cron import HOME as CRON_HOME
from tests.test_cmd_cron import _absolute_paths_outside_the_root_exist_as_directories
from tests.test_cmd_read import REPO, db_path, seed
from tests.test_cmd_validate import GOLDEN_CASES as VALIDATE_CASES

LINE = re.compile(r"verbose \(([a-z][a-z_]*)\): (0|[1-9][0-9]*) ms\n")
SECRET = "BODY-MARKER-9f3a"
TOPIC = "topic-marker-7c1e"
LABEL = "ticket=zq-9183"
MARKER_REPO = "example.com/zq-owner/zq-repo-5521"
MARKERS = (SECRET, TOPIC, "zq-9183", "zq-owner", "zq-repo-5521")
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
    ContextStore(gw).add(body=SECRET, repo=MARKER_REPO, topic=TOPIC, labels=[("ticket", "zq-9183")])
    return ["context", "--repo", MARKER_REPO, "--label", LABEL], None


def s_context_no_repo(gw: FakeGateway) -> tuple[list[str], str | None]:
    return ["context"], None


def s_validate(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    return ["validate"], None


def s_add(gw: FakeGateway) -> tuple[list[str], str | None]:
    argv = ["add", "--repo", MARKER_REPO, "--topic", TOPIC, "--label", LABEL, "--body-file", "-"]
    return argv, SECRET


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


_HOLDERS: list[sqlite3.Connection] = []


def s_prune_busy(gw: FakeGateway) -> tuple[list[str], str | None]:
    _notes(gw)
    holder = sqlite3.connect(db_path(gw), isolation_level=None)
    holder.execute(
        "BEGIN IMMEDIATE"
    )  # the write lock is taken: prune is refused at its transaction
    _HOLDERS.append(holder)
    return ["prune"], None


def s_prune_no_db(gw: FakeGateway) -> tuple[list[str], str | None]:
    open_state_dir(gw, create=True).close()  # a state directory, no state.db
    return ["prune"], None


def s_cron_uninstall(gw: FakeGateway) -> tuple[list[str], str | None]:
    s_cron_install(gw)
    gw.crontab.table = CRON_BLOCK
    return ["cron", "uninstall"], None


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
    "prune-empty": (s_prune_empty, ["parse", "output"]),  # no state directory at all
    "prune-no-db": (s_prune_no_db, ["parse", "open_store", "log", "output"]),
    "prune-busy": (s_prune_busy, ["parse", "open_store", "output"]),
    "cron-install": (
        s_cron_install,
        ["home", "read_crontab", "plan", "write_crontab", "output"],
    ),
    "cron-uninstall": (
        s_cron_uninstall,
        ["home", "read_crontab", "plan", "write_crontab", "output"],
    ),
    "cron-unchanged": (s_cron_unchanged, ["home", "read_crontab", "plan", "output"]),
    "cron-dry": (s_cron_dry, ["home", "read_crontab", "plan", "output"]),
}


@dataclass
class Run:
    code: int
    out: str
    err: str
    events: list[tuple[str, str]] = field(default_factory=list)  # ("out"|"err", text), in order
    armed_at_flush: list[bool] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _prune_scenarios(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("memoctl.store.WRITE_BUSY_MS", 50)  # prune-busy waits 50 ms, not 5 s
    yield
    while _HOLDERS:
        holder = _HOLDERS.pop()
        holder.execute("ROLLBACK")
        holder.close()


def instrument(gw: FakeGateway) -> Run:
    """Record every stdout and stderr write of the gateway, in order."""
    run = Run(0, "", "")
    write_out, write_err = gw.write_out, gw.write_err

    def out(text: str, *, bound: float | None = None, complete: bool = False) -> None:
        run.events.append(("out", text))
        write_out(text, bound=bound, complete=complete)

    def err(text: str, *, bound: float | None = None) -> None:
        run.events.append(("err", text))
        write_err(text, bound=bound)

    gw.write_out, gw.write_err = out, err  # type: ignore[method-assign]
    return run


def execute(tmp_path: Path, case: str, *extra: str) -> Run:
    gw = FakeGateway(tmp_path / f"gw-{next(_COUNTER)}")
    argv, stdin = CASES[case][0](gw)
    if stdin is not None:
        gw.terminal.stdin = stdin
    gw.terminal.stdout = gw.terminal.stderr = ""
    run = instrument(gw)
    real_flush = phases.Phases.flush

    def spy(self: phases.Phases) -> None:
        run.armed_at_flush.append(deadline.current().armed)
        real_flush(self)

    phases.Phases.flush = spy  # type: ignore[method-assign]
    try:
        run.code = main([*argv, *extra], gw)
    finally:
        phases.Phases.flush = real_flush  # type: ignore[method-assign]
    run.out, run.err = gw.terminal.stdout, gw.terminal.stderr
    return run


def go(tmp_path: Path, case: str, *extra: str) -> tuple[int, str, str]:
    run = execute(tmp_path, case, *extra)
    return run.code, run.out, run.err


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
        assert not [m for m in MARKERS if m in err]  # no value of the call reaches a line
        assert str(tmp_path) not in err


@pytest.mark.parametrize("case", CASES)
def test_one_stderr_write_carries_the_lines_after_every_stdout_write(
    tmp_path: Path, case: str
) -> None:
    run = execute(tmp_path, case, "--verbose")
    carrying = [
        i for i, (kind, text) in enumerate(run.events) if kind == "err" and "verbose (" in text
    ]
    assert len(carrying) == 1
    last = carrying[0]
    assert names(run.events[last][1]) == CASES[case][1]  # all the lines, and only they
    assert run.events[last][1].startswith("verbose (")
    assert all(kind == "err" for kind, _ in run.events[last + 1 :]) and last == len(run.events) - 1
    assert not [i for i, (kind, _) in enumerate(run.events) if kind == "out" and i > last]


@pytest.mark.parametrize("case", CASES)
def test_the_deadline_is_disarmed_when_the_lines_are_flushed(tmp_path: Path, case: str) -> None:
    assert execute(tmp_path, case, "--verbose").armed_at_flush == [False]


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
    assert phases._CURRENT is None
    assert isinstance(phases.current(), type(phases._INERT))


def test_a_clock_that_raises_leaves_no_recorder_installed(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")

    def broken() -> float:
        raise OSError

    gw.monotonic = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        phases.Phases(gw, enabled=True).__enter__()
    assert phases._CURRENT is None


def test_leaving_restores_the_previously_installed_recorder(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    with phases.Phases(gw, enabled=True) as outer:
        with phases.Phases(gw, enabled=True) as inner:
            assert phases._CURRENT is inner
        assert phases._CURRENT is outer
    assert phases._CURRENT is None


def test_at_most_64_lines_are_kept_the_first_ones(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path / "gw")
    given = [f"p{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(100)]
    with phases.Phases(gw, enabled=True) as rec:
        for name in given:
            phases.mark(name)
        rec.flush()
    assert gw.terminal.stderr.count("\n") == 64
    assert [m.group(1) for m in LINE.finditer(gw.terminal.stderr)] == given[:64]


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


# -- results that are not successes, warnings, the deadline, hostile writes ----------------------


def _fresh(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / f"gw-{next(_COUNTER)}")


def test_a_failed_validate_check_is_a_result_and_prints_its_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, code = VALIDATE_CASES["db_corrupt"]
    assert code != 0
    gw = _fresh(tmp_path)
    setup(gw, monkeypatch)
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["validate", "--verbose"], gw) == code
    status = "failed (db_corrupt): the database is damaged or not memoctl's\n"
    assert gw.terminal.stderr.startswith(status)
    assert gw.terminal.stderr[len(status) :].startswith("verbose (")
    assert names(gw.terminal.stderr) == ["validate", "output"]
    assert gw.terminal.stdout != ""


def test_context_without_a_store_prints_the_phases_that_ran(tmp_path: Path) -> None:
    gw = _fresh(tmp_path)
    (gw.root / "home").mkdir(exist_ok=True)
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["context", "--repo", CONTEXT_REPO, "--verbose"], gw) == 10
    assert gw.terminal.stdout == ""
    assert names(gw.terminal.stderr) == ["config", "open_store", "render", "output"]


def test_context_with_a_repo_over_255_characters_prints_render_and_output(tmp_path: Path) -> None:
    gw = _fresh(tmp_path)
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["context", "--repo", "example.com/" + "a" * 250, "--verbose"], gw) == 10
    assert gw.terminal.stdout == ""
    assert names(gw.terminal.stderr) == ["render", "output"]


def test_context_exit_10_lists_exactly_its_phases(tmp_path: Path) -> None:
    gw = _fresh(tmp_path)
    ContextStore(gw).add(body=SECRET)
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["context", "--repo", "example.com/none/none", "--verbose"], gw) == 10
    assert gw.terminal.stdout == ""
    assert names(gw.terminal.stderr) == ["config", "open_store", "select", "render", "output"]


def test_a_log_warning_comes_before_the_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise LogError("write_failed")

    monkeypatch.setattr(cmd_add, "append_record", broken)
    plain = go(tmp_path, "add")
    code, out, err = go(tmp_path, "add", "--verbose")
    assert (code, out) == (plain[0], plain[1])
    warning = "warning (log_failed): the note was stored but its log record was not written\n"
    assert warning in err and err.startswith(plain[2])
    assert err.index(warning) + len(warning) == err.index("verbose (")
    assert names(err) == CASES["add"][1]


def test_a_deadline_that_fires_prints_only_its_prebuilt_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mark = phases.Phases.mark
    gw = _fresh(tmp_path)
    argv, _ = CASES["list"][0](gw)

    def slow(self: phases.Phases, phase: str) -> None:
        real_mark(self, phase)
        if phase == "open_store":
            gw.advance_mono(31)  # the 30 s alarm fires inside the command

    monkeypatch.setattr(phases.Phases, "mark", slow)
    gw.terminal.stdout = gw.terminal.stderr = ""
    with pytest.raises(FakeExit) as info:
        main([*argv, "--verbose"], gw)
    assert info.value.code == 12
    assert gw.terminal.stderr == deadline.STATUS_LINE
    assert "verbose" not in gw.terminal.stderr
    assert phases._CURRENT is None


def test_a_delete_declined_at_the_prompt_prints_no_phase_lines(tmp_path: Path) -> None:
    gw = _fresh(tmp_path)
    CASES["delete-prompt"][0](gw)
    gw.terminal.tty_answer = "n"
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["delete", "1", "--verbose"], gw) == 13
    assert "verbose" not in gw.terminal.stderr


def test_a_delete_prints_nothing_on_stderr_before_its_prompt(tmp_path: Path) -> None:
    gw = _fresh(tmp_path)
    argv, _ = CASES["delete-prompt"][0](gw)
    seen: list[str] = []
    gw.terminal.tty_hook = lambda: seen.append(gw.terminal.stderr)
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main([*argv, "--verbose"], gw) == 0
    assert seen == [""]
    assert names(gw.terminal.stderr) == CASES["delete-prompt"][1]


@pytest.mark.parametrize("case", ["list", "add", "context", "prune"])
def test_a_failing_stderr_write_changes_neither_exit_code_nor_stdout(
    tmp_path: Path, case: str
) -> None:
    code, out, err = go(tmp_path, case)
    gw = FakeGateway(tmp_path / f"gw-{next(_COUNTER)}")
    argv, stdin = CASES[case][0](gw)
    if stdin is not None:
        gw.terminal.stdin = stdin
    gw.terminal.stdout = gw.terminal.stderr = ""
    write_err = gw.write_err

    def failing(text: str, *, bound: float | None = None) -> None:
        if "verbose (" in text:
            raise OSError("stalled")
        write_err(text, bound=bound)

    gw.write_err = failing  # type: ignore[method-assign]
    assert main([*argv, "--verbose"], gw) == code
    assert (gw.terminal.stdout, gw.terminal.stderr) == (out, err)


# -- a real process whose stderr is a full pipe that is never read -------------------------------


def _full_pipe() -> tuple[int, int]:
    """A pipe filled to capacity (the write end blocking again afterwards); the caller keeps the
    read end open and never reads it."""
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)
    for size in (4096, 1):
        try:
            while True:
                os.write(write_end, b"x" * size)
        except BlockingIOError:
            pass
    os.set_blocking(write_end, True)
    return read_end, write_end


def test_a_stalled_stderr_costs_the_write_wait_and_changes_no_exit_code(tmp_path: Path) -> None:
    from tests import private_home
    from tests.test_cmd_delete_process import add, env, memoctl  # a private home, a real child

    home = tmp_path / "home"
    home.mkdir()
    add(home, SECRET)
    read_end, write_end = _full_pipe()
    try:
        started = time.monotonic()
        children = [
            subprocess.Popen(
                private_home.argv(home, *argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=write_end,
                env=env(home),
            )  # fmt: skip
            for argv in (
                ["list", "--verbose"],
                ["context", "--repo", "example.com/none/none", "--verbose"],
            )
        ]
        try:
            outs = [child.communicate(timeout=30)[0] for child in children]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()  # a child this test started, by its own handle
                    child.wait()
        elapsed = time.monotonic() - started
    finally:
        os.close(read_end)
        os.close(write_end)
    assert [child.returncode for child in children] == [0, 10]
    assert outs[0].startswith(b"1  live") and outs[1] == b""
    assert elapsed < 10
    assert memoctl(home, "list").returncode == 0


@pytest.mark.parametrize("case", ["add", "delete", "delete-prompt", "prune"])
def test_the_transaction_mark_is_taken_after_the_disarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    real_mark = phases.Phases.mark
    armed: list[bool] = []

    def spy(self: phases.Phases, phase: str) -> None:
        if phase == "transaction":
            armed.append(deadline.current().armed)
        real_mark(self, phase)

    monkeypatch.setattr(phases.Phases, "mark", spy)
    execute(tmp_path, case, "--verbose")
    assert armed == [False]
