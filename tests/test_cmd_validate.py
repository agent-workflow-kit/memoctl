"""``validate`` end to end: through ``main`` on a fake gateway, and in a child process.

The checks themselves are pinned in ``tests/test_validation.py``; this file pins what the
command adds: registration, argv, the output forms and goldens, exit codes, the status line,
``--quiet``, the deadline (no partial output) and read-only behavior of the real entry point.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from memoctl import validation
from memoctl.cli import main
from memoctl.cli_commands import COMMANDS
from memoctl.statedir import StateAccessError
from memoctl.store import Store
from tests import private_home
from tests.child_env import child_path
from tests.fake_gateway import FakeAlarm, FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import validate_json_output
from tests.test_validation import (
    MOUNTINFO,
    insert,
    make_state,
    make_store,
    raw,
    snapshot,
    state_path,
    write_config,
)

ENVELOPE_TIMEOUT = (
    '{"error": {"code": "failed", "message": "deadline passed", "reason": "timeout", '
    '"details": null, "retryable": true}}\n'
)


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "fake")


def run(gw: FakeGateway, *argv: str) -> int:
    gw.terminal.stdout = gw.terminal.stderr = ""
    return main(["validate", *argv], gw)


def out(gw: FakeGateway) -> bytes:
    return gw.terminal.stdout.encode()


# --- registration and argv --------------------------------------------------------------------


def test_validate_is_registered_without_dry_run() -> None:
    (command,) = [c for c in COMMANDS if c.name == "validate"]
    assert command.dry_run is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run"],
        ["extra"],
        ["--bogus"],
        ["--jso"],  # flags are exact: no abbreviation
        ["--json", "--quiet", "--verbose"],
        ["x" * 200_000],
        ["--"],
        ["--json", "--"],
    ],
)
def test_bad_argv_is_a_usage_error_with_no_check_output(gw: FakeGateway, argv: list[str]) -> None:
    make_store(gw)
    assert run(gw, *argv) == 2
    assert "checks" not in gw.terminal.stdout and "ok  " not in gw.terminal.stdout
    assert "x" * 100 not in gw.terminal.stdout + gw.terminal.stderr
    assert gw.terminal.stderr == "usage: invalid argv\n"


def test_usage_error_with_json_prints_the_envelope_only(gw: FakeGateway) -> None:
    assert run(gw, "--dry-run", "--json") == 2
    doc = json.loads(gw.terminal.stdout)
    assert doc["error"]["code"] == "usage" and doc["error"]["details"] == {"field": "argv"}


def test_json_before_the_subcommand_is_refused(gw: FakeGateway) -> None:
    gw.terminal.stdout = gw.terminal.stderr = ""
    assert main(["--json", "validate"], gw) == 2
    assert "checks" not in gw.terminal.stdout


def test_flags_in_either_order_after_the_subcommand(gw: FakeGateway) -> None:
    make_store(gw)
    assert run(gw, "--json", "--quiet") == 0
    first = gw.terminal.stdout
    assert run(gw, "--quiet", "--json") == 0
    assert gw.terminal.stdout == first


def test_help_lists_no_dry_run(gw: FakeGateway) -> None:
    gw.terminal.stdout = ""
    assert main(["validate", "--help"], gw) == 0
    assert "--dry-run" not in gw.terminal.stdout and "--json" in gw.terminal.stdout


# --- goldens ----------------------------------------------------------------------------------

Setup = Callable[[FakeGateway, pytest.MonkeyPatch], None]


def _clean(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)


def _config_invalid(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    write_config(gw, "limits:\n  total: 0\n")


def _unsafe_state(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_state(gw).chmod(0o755)


def _db_unreadable(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)
    mp.setattr("memoctl.store.MIN_SQLITE", (99, 0, 0))


def _db_corrupt(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    db = make_store(gw)
    db.write_bytes(os.urandom(8192))
    db.chmod(0o600)


def _schema_newer(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA user_version = 99")
    c.close()


def _unsafe_filesystem(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    make_store(gw)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()


def _db_locked(gw: FakeGateway, mp: pytest.MonkeyPatch) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    LOCKS.append(c)


LOCKS: list[sqlite3.Connection] = []

GOLDEN_CASES: dict[str, tuple[Setup, int]] = {
    "clean": (_clean, 0),
    "config_invalid": (_config_invalid, 2),
    "unsafe_state": (_unsafe_state, 20),
    "db_unreadable": (_db_unreadable, 12),
    "db_corrupt": (_db_corrupt, 12),
    "db_locked": (_db_locked, 12),
    "schema_newer": (_schema_newer, 12),
    "unsafe_filesystem": (_unsafe_filesystem, 0),
}


@pytest.fixture(autouse=True)
def _release_locks() -> Any:
    yield
    while LOCKS:
        c = LOCKS.pop()
        try:
            c.execute("ROLLBACK")
        finally:
            c.close()


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_goldens_json_and_human(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    setup, code = GOLDEN_CASES[case]
    setup(gw, monkeypatch)
    assert run(gw, "--json") == code
    check_golden(f"validate/{case}.json", out(gw), schema="cli/validate")
    json_err = gw.terminal.stderr
    assert run(gw) == code
    check_golden(f"validate/{case}.txt", out(gw))
    assert gw.terminal.stderr == json_err  # the status line is the same in both forms
    if code in (2, 12, 20):
        check_golden(f"validate/{case}-err.txt", gw.terminal.stderr.encode())
    else:
        assert gw.terminal.stderr == ""


def test_golden_files_are_all_used() -> None:
    expected: set[str] = set()
    for case, (_, code) in GOLDEN_CASES.items():
        expected |= {f"{case}.json", f"{case}.txt"}
        if code in (2, 12, 20):
            expected.add(f"{case}-err.txt")
    root = Path(__file__).parent / "golden" / "validate"
    assert {p.name for p in root.iterdir()} == expected


def test_the_json_document_is_the_checks_document(gw: FakeGateway) -> None:
    make_store(gw)
    run(gw, "--json")
    doc = validate_json_output("cli/validate", out(gw))
    assert doc == validation.validate_store(gw).document()


# --- status line and --quiet ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "line"),
    [
        ("config_invalid", "config_invalid: the configuration is invalid\n"),
        ("unsafe_state", "refused (unsafe_state): the state directory is not safe to use\n"),
        ("db_corrupt", "failed (db_corrupt): the database is damaged or not memoctl's\n"),
        ("db_unreadable", "failed (db_unreadable): the database cannot be read\n"),
        ("db_locked", "failed (db_locked): the database is locked\n"),
        (
            "schema_newer",
            "failed (schema_newer): the schema is newer than this version supports\n",
        ),
    ],
)
@pytest.mark.parametrize("flags", [[], ["--json"], ["--quiet"], ["--json", "--quiet"]])
def test_a_failed_check_has_one_status_line_even_with_quiet(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, case: str, line: str, flags: list[str]
) -> None:
    GOLDEN_CASES[case][0](gw, monkeypatch)
    assert run(gw, *flags) == GOLDEN_CASES[case][1]
    assert gw.terminal.stderr == line
    assert gw.terminal.stdout != ""  # data is never suppressed


@pytest.mark.parametrize("flags", [[], ["--quiet"]])
def test_a_passing_run_prints_nothing_on_stderr(gw: FakeGateway, flags: list[str]) -> None:
    make_store(gw)
    assert run(gw, *flags) == 0
    assert gw.terminal.stderr == ""
    assert gw.terminal.stdout.count("\n") == 4


def test_quiet_does_not_change_stdout(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    _unsafe_filesystem(gw, monkeypatch)
    run(gw)
    plain = gw.terminal.stdout
    run(gw, "--quiet")
    assert gw.terminal.stdout == plain
    assert plain.endswith("warning  unsafe_filesystem\n")
    assert gw.terminal.stderr == ""  # a warning is on stdout; it does not fail the run


def test_a_failed_check_with_the_unsafe_filesystem_warning(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(gw).chmod(0o755)
    gw.mountinfo = MOUNTINFO.format(mnt=gw.home().resolve(), fs="nfs").encode()
    assert run(gw) == 20
    assert gw.terminal.stdout == (
        "ok  config  configuration is valid\n"
        "FAIL  state_dir  the state directory is not safe to use\n"
        "warning  unsafe_filesystem\n"
    )
    assert gw.terminal.stderr == "refused (unsafe_state): the state directory is not safe to use\n"
    assert run(gw, "--json") == 20
    assert gw.terminal.stdout == (
        '{"ok":false,"checks":[{"name":"config","ok":true,"reason":null,'
        '"message":"configuration is valid"},{"name":"state_dir","ok":false,'
        '"reason":"unsafe_state","message":"the state directory is not safe to use"}],'
        '"warnings":["unsafe_filesystem"]}\n'
    )


def test_output_never_echoes_paths_or_sqlite_text(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case in ("db_corrupt", "unsafe_state", "config_invalid", "schema_newer"):
        GOLDEN_CASES[case][0](gw, monkeypatch)
        for flags in ([], ["--json"]):
            run(gw, *flags)
            text = gw.terminal.stdout + gw.terminal.stderr
            assert str(gw.home()) not in text and str(gw.root) not in text
            for word in ("sqlite", "SQL", "malformed", "Traceback", "state.db"):
                assert word not in text
        _clear(gw)


def _clear(gw: FakeGateway) -> None:
    import shutil

    shutil.rmtree(gw.home())
    gw.home().mkdir()


# --- hostile databases through the command ----------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["foreign_app_id", "garbage_bytes", "truncated", "extra_table", "bad_row_grammar", "orphan"],
)
def test_hostile_databases_exit_12_and_change_nothing(gw: FakeGateway, case: str) -> None:
    db = make_store(gw)
    if case in ("garbage_bytes", "truncated"):
        data = db.read_bytes()
        db.write_bytes(os.urandom(8192) if case == "garbage_bytes" else data[:1500])
    else:
        c = raw(db)
        if case == "foreign_app_id":
            c.execute("PRAGMA application_id = 12345")
        elif case == "extra_table":
            c.execute("CREATE TABLE extra (a)")
        elif case == "bad_row_grammar":
            insert(c, repo="BAD REPO\n")
        else:
            c.execute("PRAGMA foreign_keys = OFF")
            c.execute("INSERT INTO note_labels VALUES (77, 'k', 'v')")
        c.close()
    db.chmod(0o600)
    before = snapshot(state_path(gw))
    assert run(gw, "--json") == 12
    doc = json.loads(gw.terminal.stdout)
    assert [c["reason"] for c in doc["checks"]][-1] == "db_corrupt"
    assert gw.terminal.stdout.count("\n") == 1
    assert snapshot(state_path(gw)) == before


# --- StateAccessError: unsafe_state at the state-dir level, db_unreadable for the database ----


def test_state_access_error_in_the_state_dir_check_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(gw)

    def boom(*a: Any, **k: Any) -> Any:
        raise StateAccessError()

    monkeypatch.setattr(validation, "open_state_dir", boom)
    assert run(gw, "--json") == 20
    doc = json.loads(gw.terminal.stdout)
    assert [(c["name"], c["reason"]) for c in doc["checks"]][-1] == ("state_dir", "unsafe_state")


def test_state_access_error_when_reopening_for_the_database_is_unsafe_state(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    real = validation.open_state_dir
    calls: list[int] = []

    def flaky(*a: Any, **k: Any) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise StateAccessError()
        return real(*a, **k)

    monkeypatch.setattr(validation, "open_state_dir", flaky)
    assert run(gw, "--json") == 20
    doc = json.loads(gw.terminal.stdout)
    assert [(c["name"], c["reason"]) for c in doc["checks"]][-1] == ("database", "unsafe_state")


def test_state_access_error_when_opening_the_store_is_db_unreadable(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)

    def boom(*a: Any, **k: Any) -> Any:
        raise StateAccessError()

    monkeypatch.setattr(validation, "open_store", boom)
    assert run(gw, "--json") == 12
    doc = json.loads(gw.terminal.stdout)
    assert [(c["name"], c["reason"]) for c in doc["checks"]][-1] == ("database", "db_unreadable")
    assert gw.terminal.stderr == "failed (db_unreadable): the database cannot be read\n"


# --- the deadline -----------------------------------------------------------------------------


def test_the_deadline_hook_is_installed_on_the_store(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    seen: list[Store] = []
    from memoctl import deadline

    real = deadline.Deadline.configure_store

    def spy(self: Any, store: Store) -> None:
        seen.append(store)
        real(self, store)

    monkeypatch.setattr(deadline.Deadline, "configure_store", spy)
    assert run(gw) == 0
    assert len(seen) == 1


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_deadline_firing_inside_a_check_leaves_no_partial_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    make_store(gw)
    real = Store.check_rows

    def slow(self: Store) -> Any:
        gw.advance_mono(31)  # the alarm fires inside the check
        return real(self)

    monkeypatch.setattr(Store, "check_rows", slow)
    with pytest.raises(FakeExit) as e:
        run(gw, *flags)
    assert e.value.code == 12
    assert gw.terminal.stdout == ("" if not flags else ENVELOPE_TIMEOUT)
    assert gw.terminal.stderr == "failed (timeout): deadline passed\n"


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_a_statement_interrupted_by_the_progress_handler_is_the_timeout_envelope(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    c = raw(make_store(gw))
    for i in range(300):
        insert(c, id=i + 1, topic=f"t{i}", body=f"b{i}", size=len(f"b{i}"))
    c.close()
    real = Store.check_rows

    def expire(self: Store) -> Any:
        gw.mono += 31  # time has run out, the timer has not run: the handler sees it
        return real(self)

    monkeypatch.setattr(Store, "check_rows", expire)
    monkeypatch.setattr(FakeAlarm, "release", lambda self: False)  # the timer never ran
    # the StoreError(timeout) path returns the exit code; the timer path would raise FakeExit
    assert run(gw, *flags) == 12
    assert "checks" not in gw.terminal.stdout and "ok  " not in gw.terminal.stdout
    assert gw.terminal.stderr == "failed (timeout): deadline passed\n"
    if flags:
        assert gw.terminal.stdout == ENVELOPE_TIMEOUT


def test_a_deadline_that_ran_out_before_the_output_exits_12_with_no_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_store(gw)
    real = validation.validate_store

    def late(gateway: Any, **kw: Any) -> Any:
        result = real(gateway, **kw)
        gw.mono += 31  # the timer has not run yet; the disarm sees the time is up
        return result

    monkeypatch.setattr("memoctl.cmd_validate.validate_store", late)
    with pytest.raises(FakeExit) as e:
        run(gw, "--json")
    assert e.value.code == 12
    assert gw.terminal.stdout == ENVELOPE_TIMEOUT


# --- the real entry point in a child process --------------------------------------------------


def _child(
    home: Path, *args: str, stdout: Any = subprocess.PIPE, prefix: str = ""
) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": child_path(home.parent, tools=["sh"] if prefix else []),
        "LC_ALL": "C",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
    }
    cmd: list[str] = private_home.argv(home, *args)
    if prefix:
        cmd = ["sh", "-c", f'exec "$0" "$@" {prefix}', *cmd]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE, env=env, cwd=home
    )
    try:
        o, e = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, o, e)


def test_child_clean_store_exit_0_and_read_only(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    insert(c)
    c.execute("INSERT INTO note_labels VALUES (1, 'k', 'v')")
    c.close()
    before = snapshot(state_path(gw))
    r = _child(gw.home(), "validate", "--json")
    assert (r.returncode, r.stderr) == (0, b"")
    doc = validate_json_output("cli/validate", r.stdout)
    assert doc["ok"] is True and [c["name"] for c in doc["checks"]] == [
        "config", "state_dir", "database", "schema",
    ]  # fmt: skip
    h = _child(gw.home(), "validate")
    assert h.returncode == 0 and h.stdout.startswith(b"ok  config  ")
    assert snapshot(state_path(gw)) == before  # byte for byte, apart from the WAL sidecars


def test_child_missing_store_is_created_nowhere(gw: FakeGateway) -> None:
    r = _child(gw.home(), "validate", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout)["checks"][1]["message"] == "not created yet"
    assert not (gw.home() / ".local").exists()


def test_child_exit_2_config_invalid(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  total: 0\n")
    r = _child(gw.home(), "validate", "--json")
    assert r.returncode == 2
    assert [c["name"] for c in validate_json_output("cli/validate", r.stdout)["checks"]] == [
        "config"
    ]
    assert r.stderr == b"config_invalid: the configuration is invalid\n"


def test_child_exit_20_unsafe_state_and_nothing_changed(gw: FakeGateway) -> None:
    make_state(gw).chmod(0o755)
    before = snapshot(state_path(gw))
    r = _child(gw.home(), "validate")
    assert r.returncode == 20
    assert r.stdout.startswith(b"ok  config  ") and b"FAIL  state_dir  " in r.stdout
    assert r.stderr == b"refused (unsafe_state): the state directory is not safe to use\n"
    assert snapshot(state_path(gw)) == before


def test_child_exit_12_corrupt_database(gw: FakeGateway) -> None:
    db = make_store(gw)
    db.write_bytes(os.urandom(8192))
    db.chmod(0o600)
    before = snapshot(state_path(gw))
    r = _child(gw.home(), "validate", "--json")
    assert r.returncode == 12
    doc = validate_json_output("cli/validate", r.stdout)
    assert doc["checks"][-1]["reason"] == "db_corrupt"
    assert snapshot(state_path(gw)) == before


def test_child_exit_12_locked_database(gw: FakeGateway) -> None:
    c = raw(make_store(gw))
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        r = _child(gw.home(), "validate", "--json")
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert r.returncode == 12
    assert validate_json_output("cli/validate", r.stdout)["checks"][-1]["reason"] == "db_locked"


def test_child_hostile_argv_is_a_usage_error(gw: FakeGateway) -> None:
    make_store(gw)
    for args in (["validate", "--dry-run"], ["validate", "x" * 100_000]):
        r = _child(gw.home(), *args)
        assert r.returncode == 2 and r.stdout == b""
    r = _child(gw.home(), "--json", "validate")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "usage"
    r = _child(gw.home(), "validate", "--bogus", "--json")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "usage"


def test_child_closed_stdout_keeps_the_exit_code(gw: FakeGateway) -> None:
    make_state(gw).chmod(0o755)
    r = _child(gw.home(), "validate", "--json", prefix=">&-")
    assert r.returncode == 20
    assert r.stderr.startswith(b"refused (unsafe_state)")


def test_child_broken_pipe_keeps_the_exit_code(gw: FakeGateway) -> None:
    make_store(gw)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        r = _child(gw.home(), "validate", "--json", stdout=write_end)
    finally:
        os.close(write_end)
    assert r.returncode == 0
