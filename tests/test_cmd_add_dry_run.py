"""``memoctl add --dry-run`` and the stderr warning lines (interface.md, ``add``).

Every test runs on the fake gateway's private home and never touches the real one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memoctl import cmd_add
from memoctl import store as store_mod
from memoctl.cli import main
from tests.add_scenarios import (
    REPO,
    SCENARIOS,
    release,
    state_dir,
    step_of,
    token,
    write_config,
)
from tests.fake_gateway import FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import SchemaViolation, validate_json_output
from tests.test_cmd_add import (
    KIND_LINE,
    add,
    error_of,
    log_records,
    snapshot,
    stored,
)

__all__ = ["gw"]

CAPPED_LINE = "warning (expires_capped): the requested lifetime was cut to the maximum age\n"
EXTENDED_LINE = (
    "warning (expires_extended): "
    "the lifetime was raised to the expiry of the note this one replaces\n"
)
LOG_LINE = "warning (log_failed): the note was stored but its log record was not written\n"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def dry(gw: FakeGateway, *argv: str, **kwargs: Any) -> tuple[int, str, str]:
    return add(gw, "--dry-run", *argv, **kwargs)


# -- the output -----------------------


def test_dry_run_prints_the_documented_document_and_writes_nothing(gw: FakeGateway) -> None:
    before = snapshot(gw.home())
    code, out, err = dry(gw, "--kind", "implement", "--label", "b=2", "--label", "a=1")
    assert (code, err) == (0, "")
    validate_json_output("cli/add", out.encode())
    doc = json.loads(out)
    assert doc["id"] is None and doc["created"] is True and doc["dry_run"] is True
    assert doc["labels"] == ["a=1", "b=2"] and doc["supersedes"] is None
    assert doc["size"] == len(b"a note body\n") and doc["warnings"] == []
    assert doc["expires"] == "2026-01-31T00:00:00Z"  # now + the default 30 days
    check_golden("add/dry-run.json", out.encode(), schema="cli/add")
    assert snapshot(gw.home()) == before and not state_dir(gw).exists()


def test_dry_run_human_form_and_golden(gw: FakeGateway) -> None:
    code, out, err = dry(gw, "--kind", "implement", json_mode=False)
    assert (code, err) == (0, "")
    check_golden("add/dry-run.txt", out.encode())


def test_dry_run_matches_the_real_add_except_id_created_and_dry_run(gw: FakeGateway) -> None:
    _, dry_out, _ = dry(gw, "--label", "a=1", "--expires", "2h")
    _, real_out, _ = add(gw, "--label", "a=1", "--expires", "2h")
    shown = json.loads(dry_out)
    real = json.loads(real_out)
    for doc in (shown, real):
        doc.pop("id"), doc.pop("dry_run")
    assert shown == real


def test_dry_run_twice_prints_the_same_bytes(gw: FakeGateway) -> None:
    first = dry(gw)
    second = dry(gw)
    assert first == second


def test_dry_run_with_quiet_prints_no_result_line_but_keeps_json_and_warnings(
    gw: FakeGateway,
) -> None:
    assert dry(gw, "--quiet", json_mode=False) == (0, "", KIND_LINE)
    code, out, err = dry(gw, "--quiet")
    assert code == 0 and json.loads(out)["dry_run"] is True and err == KIND_LINE


def test_dry_run_reports_a_hit_never_and_a_supersede_target_unchecked(gw: FakeGateway) -> None:
    add(gw)  # a live note with the same key: a real add would be a dedupe hit
    code, out, _ = dry(gw, "--supersedes", "77")  # no such note: not looked at
    doc = json.loads(out)
    assert code == 0 and doc["id"] is None and doc["created"] is True and doc["supersedes"] == 77
    assert len(stored(gw)) == 1


def test_dry_run_expires_is_the_calls_own_lifetime_never_extended(gw: FakeGateway) -> None:
    add(gw, "--expires", "30d", "--kind", "implement")
    code, out, err = dry(gw, "--supersedes", "1", "--expires", "1h", "--kind", "implement")
    doc = json.loads(out)
    assert (code, err) == (0, "")
    assert doc["expires"] == "2026-01-01T01:00:00Z" and doc["warnings"] == []


def test_dry_run_runs_no_limits(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  live_per_repo: 1\n  total: 1\n  adds_per_window: 1\n")
    assert add(gw, "--kind", "implement")[0] == 0
    assert add(gw, "--kind", "implement", body="two")[0] == 20  # a real add is refused
    assert dry(gw, "--kind", "implement", body="three")[0] == 0


# -- never touches the state -----------------------


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("dry run reached the state")


def test_dry_run_calls_no_store_state_dir_or_log_function(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "step8_open_state_dir", "step9_open_store", "step10_dedupe", "step11_supersede",
        "step12_limits", "step13_insert", "step14_append_log", "open_state_dir", "open_store",
        "append_record", "evaluate_limits", "find_duplicate", "check_supersede_target",
    ):  # fmt: skip
        monkeypatch.setattr(cmd_add, name, _fail)
    monkeypatch.setattr(store_mod, "open_store", _fail)
    assert dry(gw, "--supersedes", "3", "--kind", "implement")[0] == 0
    assert dry(gw, json_mode=False)[0] == 0


def test_dry_run_reads_the_clock_and_stdin_once(gw: FakeGateway) -> None:
    assert dry(gw)[0] == 0
    assert (gw.now_calls, gw.stdin_reads) == (1, 1)


def test_dry_run_does_not_care_about_the_state_directory(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.parent.mkdir(parents=True)
    sd.mkdir()
    sd.chmod(0o777)  # unsafe: a real add is refused (unsafe_state)
    assert add(gw)[0] == 20
    before = snapshot(gw.home())
    assert dry(gw)[0] == 0
    assert snapshot(gw.home()) == before


def test_dry_run_with_a_state_dir_that_is_a_file(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.parent.mkdir(parents=True)
    sd.write_text("not a directory")
    before = snapshot(gw.home())
    code, out, _ = add(gw)
    assert (code, error_of(out)["reason"]) == (20, "unsafe_state")
    assert dry(gw)[0] == 0
    assert snapshot(gw.home()) == before


def test_dry_run_with_a_corrupt_database(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True)
    sd.chmod(0o700)
    (sd / "state.db").write_bytes(b"this is not a database" * 100)
    (sd / "state.db").chmod(0o600)
    before = snapshot(gw.home())
    code, out, _ = add(gw)
    assert (code, error_of(out)["reason"]) == (12, "db_corrupt")
    assert snapshot(gw.home()) == before
    assert dry(gw)[0] == 0
    assert snapshot(gw.home()) == before


def test_dry_run_with_a_missing_home_state_parent_creates_nothing(gw: FakeGateway) -> None:
    assert dry(gw)[0] == 0
    assert not (gw.home() / ".local").exists()


def test_dry_run_does_not_write_a_log_record(gw: FakeGateway) -> None:
    add(gw)
    before = log_records(gw)
    dry(gw, body="another")
    assert log_records(gw) == before and len(stored(gw)) == 1


# -- the steps 1 to 7 apply, 8 to 13 do not -----------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s[0])
def test_each_failing_step_alone_applies_to_dry_run_only_up_to_step_7(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, scenario: Any
) -> None:
    monkeypatch.setattr(store_mod, "WRITE_BUSY_MS", 30)
    argv = ["add", "--repo", REPO, "--topic", "build", "--body-file", "-", "--json", "--dry-run"]
    gw.terminal.stdin = "a body"
    try:
        scenario[1](gw, argv)
        before = snapshot(gw.home())
        gw.terminal.stdout = ""
        code = main(argv, gw)
        if step_of(scenario) <= 7:
            assert scenario[2](code, error_of(gw.terminal.stdout)), (scenario[0], code)
        else:  # the state and the limits are not looked at
            assert code == 0, scenario[0]
            doc = json.loads(gw.terminal.stdout)
            assert doc["id"] is None and doc["dry_run"] is True
            validate_json_output("cli/add", gw.terminal.stdout.encode())
        assert gw.now_calls <= 1
        if step_of(scenario) <= 7:
            assert gw.stdin_reads == (0 if step_of(scenario) < 5 else 1)
        sidecar = (".db-wal", ".db-shm", "-wal", "-shm")
        assert {k: v for k, v in snapshot(gw.home()).items() if not k.endswith(sidecar)} == {
            k: v for k, v in before.items() if not k.endswith(sidecar)
        }
    finally:
        release(gw)
    assert log_records(gw) == []


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (a, b)
        for i, a in enumerate(SCENARIOS)
        for b in SCENARIOS[i + 1 :]
        if step_of(a) != step_of(b) and step_of(a) <= 7
    ],
    ids=lambda s: s[0],
)
def test_of_two_failing_steps_dry_run_reports_the_earlier_one(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, first: Any, second: Any
) -> None:
    monkeypatch.setattr(store_mod, "WRITE_BUSY_MS", 30)
    argv = ["add", "--repo", REPO, "--topic", "build", "--body-file", "-", "--json", "--dry-run"]
    gw.terminal.stdin = "a body"
    try:
        for _, setup, _ in (second, first):
            setup(gw, argv)
        gw.terminal.stdout = ""
        code = main(argv, gw)
    finally:
        release(gw)
    assert first[2](code, error_of(gw.terminal.stdout)), (first[0], second[0], code)
    assert log_records(gw) == []


# -- the deadline -----------------------

DRY_STEPS = [
    "step1_parse", "step2_load_config", "step3_check_label_count", "step4_resolve_kind",
    "step5_read_body", "step6_secret_check", "step7_read_clock",
]  # fmt: skip


@pytest.mark.parametrize("step", DRY_STEPS)
def test_a_deadline_that_passes_in_a_step_exits_12_and_prints_no_dry_run_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    original = getattr(cmd_add, step)

    def slow(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        gw.advance_mono(31)
        return result

    monkeypatch.setattr(cmd_add, step, slow)
    with pytest.raises(FakeExit) as info:
        dry(gw)
    assert info.value.code == 12
    assert error_of(gw.terminal.stdout)["reason"] == "timeout"
    assert "dry_run" not in gw.terminal.stdout and not state_dir(gw).exists()


def test_the_deadline_is_disarmed_before_the_first_byte_of_dry_run_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real_write = gw.write_out

    def write_out(text: str, *, bound: float | None = None, complete: bool = False) -> None:
        seen.append(len(gw.alarms))
        real_write(text, bound=bound, complete=complete)

    monkeypatch.setattr(gw, "write_out", write_out)
    assert dry(gw)[0] == 0
    assert seen == [0]


def test_a_timer_that_ran_out_before_the_disarm_point_prints_no_document(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cmd_add.step7_read_clock

    def mark(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        gw.alarms[0].fired = True
        return result

    monkeypatch.setattr(cmd_add, "step7_read_clock", mark)
    with pytest.raises(FakeExit) as info:
        dry(gw)
    assert info.value.code == 12 and "dry_run" not in gw.terminal.stdout


# -- hostile input -----------------------


@pytest.mark.parametrize("flag", ["--dry-run=x", "--dry-run=", "--dry", "--dry-ru", "--dryrun"])
def test_dry_run_takes_no_value_and_has_no_abbreviation(gw: FakeGateway, flag: str) -> None:
    code, out, err = add(gw, flag)
    assert code == 2 and error_of(out)["details"] == {"field": "argv"}
    assert err == "usage: invalid argv\n" and gw.stdin_reads == 0
    assert not state_dir(gw).exists()


def test_dry_run_refuses_a_secret_exactly_as_a_real_add(gw: FakeGateway) -> None:
    code, out, err = dry(gw, body=f"x {token()}")
    assert code == 20
    assert error_of(out)["details"] == {"class": "github_token", "field": "body"}
    assert token() not in out + err
    assert not state_dir(gw).exists()


@pytest.mark.parametrize("body", ["a\x00b", "", "  \n", "﻿x", "a‮b", "x" * 9000])
def test_dry_run_refuses_a_bad_body_exactly_as_a_real_add(gw: FakeGateway, body: str) -> None:
    real = add(gw, body=body)
    shown = dry(gw, body=body)
    assert shown[0] == real[0] == 2 and shown[1] == real[1] and shown[2] == real[2]
    assert stored(gw) == []


def test_dry_run_with_a_huge_body_stops_at_the_limit_not_at_the_end_of_stdin(
    gw: FakeGateway,
) -> None:
    code, out, _ = dry(gw, body="x" * 5_000_000)
    assert code == 2 and error_of(out)["details"] == {"field": "body", "limit": 8192}


def test_a_closed_stdout_keeps_the_dry_run_exit_code(gw: FakeGateway) -> None:
    del gw.fds[1]
    assert dry(gw)[0] == 0


def test_dry_run_with_a_body_file_reads_that_file(gw: FakeGateway) -> None:
    (gw.root / "note.txt").write_text("from a file\n")
    code, out, _ = dry(gw, body=None, body_file="note.txt")
    assert code == 0 and json.loads(out)["size"] == len(b"from a file\n")
    assert gw.stdin_reads == 0


# -- the warning lines -----------------------


def test_expires_capped_prints_its_line_in_both_forms(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--kind", "implement", "--expires", "90d")
    assert (code, err) == (0, CAPPED_LINE)
    assert json.loads(out)["warnings"] == ["expires_capped"]
    check_golden("add/warning-expires-capped-err.txt", err.encode())
    code, out, err = add(gw, "--kind", "implement", "--expires", "90d", "--quiet", json_mode=False)
    assert (code, out, err) == (0, "", CAPPED_LINE)  # a warning is never suppressed


def test_kind_not_shown_prints_its_line(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--kind", "other")
    assert (code, err) == (0, KIND_LINE)
    assert json.loads(out)["warnings"] == ["kind_not_shown"]
    check_golden("add/warning-kind-not-shown-err.txt", err.encode())


def test_expires_extended_prints_its_line(gw: FakeGateway) -> None:
    add(gw, "--kind", "implement", "--expires", "30d")
    code, out, err = add(
        gw, "--kind", "implement", "--expires", "1h", "--supersedes", "1", body="n"
    )
    assert (code, err) == (0, EXTENDED_LINE)
    assert json.loads(out)["warnings"] == ["expires_extended"]
    check_golden("add/warning-expires-extended-err.txt", err.encode())


def test_warnings_print_in_the_order_of_the_array_then_log_failed(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(gw, "--kind", "other", "--expires", "30d")
    monkeypatch.setattr(cmd_add, "append_record", lambda *a, **k: 1 / 0)
    code, out, err = add(gw, "--kind", "other", "--expires", "90d", "--supersedes", "1", body="n")
    assert code == 0
    assert json.loads(out)["warnings"] == ["expires_capped", "kind_not_shown"]
    assert err == CAPPED_LINE + KIND_LINE + LOG_LINE
    check_golden("add/warning-all-err.txt", err.encode())


def test_log_failed_is_never_in_the_json_warnings_and_not_suppressed_by_quiet(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmd_add, "append_record", lambda *a, **k: 1 / 0)
    code, out, err = add(gw, "--kind", "implement", "--quiet")
    assert code == 0 and json.loads(out)["warnings"] == [] and err == LOG_LINE
    check_golden("add/warning-log-failed-err.txt", err.encode())


def test_a_dedupe_hit_prints_this_calls_warnings_and_never_expires_extended(
    gw: FakeGateway,
) -> None:
    add(gw, "--kind", "implement")
    code, out, err = add(gw, "--kind", "implement", "--expires", "90d")
    doc = json.loads(out)
    assert code == 0 and doc["created"] is False and doc["warnings"] == ["expires_capped"]
    assert err == CAPPED_LINE


def test_dry_run_warnings_match_between_the_two_forms(gw: FakeGateway) -> None:
    _, out, err_json = dry(gw, "--expires", "90d", "--kind", "other")
    _, human, err_human = dry(gw, "--expires", "90d", "--kind", "other", json_mode=False)
    assert json.loads(out)["warnings"] == ["expires_capped", "kind_not_shown"]
    assert err_json == err_human == CAPPED_LINE + KIND_LINE
    assert human.startswith("dry run: valid")
    check_golden("add/dry-run-warnings-err.txt", err_human.encode())
    check_golden("add/dry-run-warnings.json", out.encode(), schema="cli/add")


def test_the_schema_rejects_expires_extended_in_a_dry_run_document(gw: FakeGateway) -> None:
    _, out, _ = dry(gw)
    doc = json.loads(out)
    assert doc["dry_run"] is True
    doc["warnings"] = ["expires_extended"]
    with pytest.raises(SchemaViolation):
        validate_json_output("cli/add", (json.dumps(doc) + "\n").encode())


@pytest.mark.parametrize("kind", ["Bad Kind", "a;b", "x" * 5000, "\udcff"])
def test_warning_lines_never_echo_a_hostile_value(gw: FakeGateway, kind: str) -> None:
    gw.environ["CSESSION_KIND"] = kind
    _, out, err = dry(gw, json_mode=False)
    assert err == KIND_LINE and kind[:8] not in out
