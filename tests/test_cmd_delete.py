"""``memoctl delete`` end to end through ``main`` on a fake gateway and a real store.

Chains are written as the store holds them: ``superseded_by`` of the REPLACED note names the
replacement (A replaced B: B.superseded_by = A).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from memoctl.cli import main
from memoctl.render import DELETE_LOG_FAILED, WARNING_MESSAGES
from tests.fake_gateway import EPOCH, FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_read import NOW, REPO, add_note, db_path, seed

SHA = "a" * 64


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def run(gw: FakeGateway, *argv: str) -> int:
    gw.terminal.stdout = gw.terminal.stderr = ""
    return main(list(argv), gw)


def state(gw: FakeGateway) -> dict[str, Any]:
    """Everything a delete may change, as comparable values."""
    conn = sqlite3.connect(db_path(gw))
    try:
        snap: dict[str, Any] = {
            "notes": conn.execute("SELECT * FROM notes ORDER BY id").fetchall(),
            "labels": conn.execute("SELECT * FROM note_labels ORDER BY 1,2,3").fetchall(),
            "rate": conn.execute("SELECT * FROM rate ORDER BY 1,2").fetchall(),
        }
    finally:
        conn.close()
    log = db_path(gw).parent / "log.jsonl"
    snap["log"] = log.read_bytes() if log.is_file() else b""
    return snap


def note_ids(gw: FakeGateway) -> list[int]:
    return [row[0] for row in state(gw)["notes"]]


def pointers(gw: FakeGateway) -> dict[int, int | None]:
    return {row[0]: row[9] for row in state(gw)["notes"]}


def log_records(gw: FakeGateway) -> list[dict[str, Any]]:
    raw = state(gw)["log"].decode()
    return [json.loads(line) for line in raw.splitlines()]


def chain(gw: FakeGateway) -> None:
    """A=3 replaced B=2, B replaced C=1."""
    seed(gw, [dict(id=1, superseded_by=2), dict(id=2, superseded_by=3), dict(id=3)]).close()


# --- the happy path ---------------------------------------------------------------------------


def test_delete_one_with_yes_json(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2)], labels=[(1, "a", "b"), (2, "a", "b")]).execute(
        "INSERT INTO rate VALUES ('x', 5)"
    ).connection.commit()
    assert run(gw, "delete", "1", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[1],"reinstated":[]}\n'
    snap = state(gw)
    assert [r[0] for r in snap["notes"]] == [2]
    assert snap["labels"] == [(2, "a", "b")]
    assert snap["rate"] == [("x", 5)]  # never touched
    [rec] = log_records(gw)
    assert (rec["event"], rec["note_id"], rec["repo"], rec["topic"], rec["kind"]) == (
        "delete", 1, REPO, "t", "unknown",
    )  # fmt: skip
    assert rec["body_sha256"] == SHA[:16] and rec["size"] == 5
    assert rec["reinstated"] is None and rec["supersedes"] is None
    assert rec["actor"] == "auto:unknown" and rec["actor_source"] == "default"
    assert "hello" not in json.dumps(rec)


def test_actor_flag_is_recorded(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    assert run(gw, "delete", "1", "--yes", "--actor", "human:me") == 0
    [rec] = log_records(gw)
    assert (rec["actor"], rec["actor_source"]) == ("human:me", "flag")


def test_bad_actor_is_usage(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    assert run(gw, "delete", "1", "--yes", "--actor", "bad actor") == 2
    assert note_ids(gw) == [1]


def test_human_output_and_quiet(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2), dict(id=3)]).close()
    assert run(gw, "delete", "3", "1", "--yes") == 0
    assert gw.terminal.stdout == "deleted note 1\ndeleted note 3\n"
    assert run(gw, "delete", "2", "--yes", "--quiet") == 0
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "")
    assert note_ids(gw) == []


def test_duplicate_ids_collapse(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2)]).close()
    assert run(gw, "delete", "2", "2", "2", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[2],"reinstated":[]}\n'
    assert len(log_records(gw)) == 1


def test_delete_frees_slots_but_not_rate(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1)])
    conn.execute("INSERT INTO rate VALUES (?, ?)", (REPO, NOW))
    conn.close()
    run(gw, "delete", "1", "--yes")
    assert state(gw)["rate"] == [(REPO, NOW)]


# --- the prompt -------------------------------------------------------------------------------


def test_prompt_texts(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, topic="topic-x"), dict(id=2), dict(id=10)]).close()
    gw.terminal.tty_answer = "y"
    assert run(gw, "delete", "1") == 0
    assert run(gw, "delete", "10", "2") == 0
    assert gw.terminal.tty_prompts == [
        f"Delete note 1 ({REPO}, topic-x)? [y/N] ",
        "Delete 2 notes (2, 10)? [y/N] ",
    ]


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "yEs"])
def test_accepted_answers(gw: FakeGateway, answer: str) -> None:
    seed(gw, [dict(id=1)]).close()
    gw.terminal.tty_answer = answer
    assert run(gw, "delete", "1") == 0
    assert note_ids(gw) == []


@pytest.mark.parametrize(
    "answer",
    [None, "", "n", "N", "no", " y", "y ", "yy", "yes!", "ye", "y\x00", "yes\x00", "ｙ", "1",
     "y" * 5000, "\x1b[A", "Y\r", "sí", "yes\ty"],
)  # fmt: skip
def test_declined_answers_delete_nothing(gw: FakeGateway, answer: str | None) -> None:
    seed(gw, [dict(id=1), dict(id=2, superseded_by=1)]).close()
    before = state(gw)
    gw.terminal.tty_answer = answer
    assert run(gw, "delete", "1", "2", "--json") == 13
    assert state(gw) == before
    err = json.loads(gw.terminal.stdout)["error"]
    assert (err["code"], err["reason"], err["details"]) == ("needs_human", None, None)
    assert gw.terminal.stderr.startswith("needs_human: ")


def test_yes_never_asks(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    gw.terminal.tty_answer = None
    assert run(gw, "delete", "1", "--yes") == 0
    assert gw.terminal.tty_prompts == []


def test_prompt_is_not_on_stdout_or_stderr(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    gw.terminal.tty_answer = "n"
    run(gw, "delete", "1")
    assert "Delete" not in gw.terminal.stdout + gw.terminal.stderr


def test_deadline_is_paused_while_asking_and_armed_after(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    seen: list[int] = []

    def think() -> None:
        seen.append(len(gw.alarms))
        gw.advance_mono(3600)  # longer than any deadline

    gw.terminal.tty_answer = "y"
    gw.terminal.tty_hook = think
    assert run(gw, "delete", "1") == 0
    assert seen == [0] and gw.alarms == []
    assert note_ids(gw) == []


def test_deadline_is_armed_with_yes_and_disarmed_before_output(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    armed_at_output: list[int] = []
    original = gw.write_out

    def spy(text: str, **kw: Any) -> None:
        armed_at_output.append(len(gw.alarms))
        original(text, **kw)

    gw.write_out = spy  # type: ignore[method-assign]
    assert run(gw, "delete", "1", "--yes") == 0
    assert armed_at_output == [0]


# --- order: the earlier refusal wins and nothing is deleted -----------------------------------

BAD_IDS = [
    ["0"], ["01"], ["-1"], ["1.0"], ["+1"], [" 1"], ["1 "], ["x"], ["١"], ["１"],
    ["1" * 19], ["9" * 40], ["1" * 5000], [""], ["1", "x"],
]  # fmt: skip


@pytest.mark.parametrize("ids", BAD_IDS)
def test_bad_ids_are_usage_and_delete_nothing(gw: FakeGateway, ids: list[str]) -> None:
    seed(gw, [dict(id=1)]).close()
    before = state(gw)
    gw.terminal.tty_answer = "y"
    code = run(gw, "delete", "--yes", "--json", "--", *ids)
    assert code == 2
    assert state(gw) == before and gw.terminal.tty_prompts == []
    err = json.loads(gw.terminal.stdout)["error"]
    assert err["code"] == "usage" and err["details"]["field"] in ("id", "argv")
    assert str(ids[0]) not in gw.terminal.stderr or ids[0] in ("", "0", "1", "x")


def test_no_ids_is_usage_id(gw: FakeGateway) -> None:
    assert run(gw, "delete", "--yes", "--json") == 2
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"field": "id"}


def test_id_count_cap(gw: FakeGateway) -> None:
    seed(gw, [dict(id=i) for i in range(1, 502)]).close()
    ids = [str(i) for i in range(1, 502)]
    assert run(gw, "delete", "--yes", "--json", *ids) == 2
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"field": "id"}
    assert len(note_ids(gw)) == 501
    assert run(gw, "delete", "--yes", "--json", *ids[:500]) == 0
    assert note_ids(gw) == [501]
    assert len(log_records(gw)) == 500


def test_cap_counts_distinct_ids(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    assert run(gw, "delete", "--yes", *["1"] * 600) == 0


def test_unknown_id_names_the_smallest_and_deletes_nothing(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=5)]).close()
    before = state(gw)
    gw.terminal.tty_answer = "y"
    assert run(gw, "delete", "9", "5", "7", "1", "--json") == 10
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"id": 7}
    assert state(gw) == before
    assert gw.terminal.tty_prompts == []  # not found comes before the prompt


def test_missing_store_is_not_found(gw: FakeGateway) -> None:
    assert run(gw, "delete", "4", "3", "--yes", "--json") == 10
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"id": 3}
    assert not (gw.home() / ".local").exists()


def test_not_found_beats_needs_human_beats_nothing(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    gw.terminal.tty_answer = None
    assert run(gw, "delete", "1", "2") == 10
    assert run(gw, "delete", "1") == 13
    assert note_ids(gw) == [1]


def test_unsafe_state_is_refused(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    (db_path(gw).parent).chmod(0o755)
    assert run(gw, "delete", "1", "--yes", "--json") == 20
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "unsafe_state"
    assert note_ids(gw) == [1]


def test_newer_schema_exits_12(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    conn = sqlite3.connect(db_path(gw))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    assert run(gw, "delete", "1", "--yes", "--json") == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "schema_newer"


def test_notes_gone_while_asking_exit_10_and_nothing_deleted(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2), dict(id=3)]).close()

    def other_process_deletes() -> None:
        conn = sqlite3.connect(db_path(gw))
        conn.execute("DELETE FROM notes WHERE id = 2")
        conn.commit()
        conn.close()

    gw.terminal.tty_answer = "y"
    gw.terminal.tty_hook = other_process_deletes
    assert run(gw, "delete", "1", "2", "3", "--json") == 10
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"id": 2}
    assert note_ids(gw) == [1, 3]
    assert not (db_path(gw).parent / "log.jsonl").exists()


# --- chains -----------------------------------------------------------------------------------


def test_delete_the_tip_reinstates_the_one_below(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "3", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[3],"reinstated":[2]}\n'
    assert pointers(gw) == {1: 2, 2: None}
    [rec] = log_records(gw)
    assert rec["note_id"] == 3 and rec["reinstated"] == 2


def test_delete_the_middle_repoints_the_one_below(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "2", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[2],"reinstated":[]}\n'
    assert pointers(gw) == {1: 3, 3: None}
    assert log_records(gw)[0]["reinstated"] is None


def test_delete_the_tip_and_the_middle_reinstates_the_first(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "3", "2", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[2,3],"reinstated":[1]}\n'
    assert pointers(gw) == {1: None}
    recs = {r["note_id"]: r["reinstated"] for r in log_records(gw)}
    assert recs == {2: 1, 3: None}  # the record of the note the reinstated one pointed to


def test_delete_the_bottom_leaves_the_rest(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "1", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[1],"reinstated":[]}\n'
    assert pointers(gw) == {2: 3, 3: None}


def test_expired_reinstated_is_marked(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, superseded_by=2, expires=NOW), dict(id=2)]).close()
    assert run(gw, "delete", "2", "--yes") == 0
    assert gw.terminal.stdout == "deleted note 2; reinstated note 1 (expired)\n"
    assert pointers(gw) == {1: None}


def test_live_reinstated_human_line(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "3", "--yes") == 0
    assert gw.terminal.stdout == "deleted note 3; reinstated note 2\n"


def test_long_chain_delete_every_other(gw: FakeGateway) -> None:
    n = 24  # note i replaced note i-1
    seed(gw, [dict(id=i, superseded_by=i + 1 if i < n else None) for i in range(1, n + 1)]).close()
    evens = [str(i) for i in range(2, n + 1, 2)]
    assert run(gw, "delete", "--yes", "--json", *evens) == 0
    # every remaining odd note i points to the next remaining one, the top (23) is reinstated...
    assert pointers(gw) == {i: (i + 2 if i < n - 1 else None) for i in range(1, n, 2)}
    assert json.loads(gw.terminal.stdout)["reinstated"] == [23]


def test_long_chain_delete_all_but_bottom_reinstates_bottom(gw: FakeGateway) -> None:
    n = 24
    seed(gw, [dict(id=i, superseded_by=i + 1 if i < n else None) for i in range(1, n + 1)]).close()
    assert run(gw, "delete", "--yes", "--json", *[str(i) for i in range(2, n + 1)]) == 0
    assert json.loads(gw.terminal.stdout) == {"deleted": list(range(2, n + 1)), "reinstated": [1]}
    assert pointers(gw) == {1: None}


def test_deleting_a_whole_chain_reinstates_nothing(gw: FakeGateway) -> None:
    chain(gw)
    assert run(gw, "delete", "1", "2", "3", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[1,2,3],"reinstated":[]}\n'
    assert note_ids(gw) == []


def test_other_notes_are_untouched(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2, superseded_by=3), dict(id=3), dict(id=4)]).close()
    before = state(gw)["notes"]
    run(gw, "delete", "1", "--yes")
    assert state(gw)["notes"] == [r for r in before if r[0] != 1]


# --- planted rows -----------------------------------------------------------------------------


def test_pointer_to_a_missing_note_is_left_hidden(gw: FakeGateway) -> None:
    # 1 -> 2 -> 99 (99 does not exist): deleting 2 must not show 1 (it was replaced by
    # something that is gone, as add treats a dangling pointer: already_superseded).
    seed(gw, [dict(id=1, superseded_by=2), dict(id=2, superseded_by=99)]).close()
    assert run(gw, "delete", "2", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[2],"reinstated":[]}\n'
    assert pointers(gw) == {1: 2}


@pytest.mark.parametrize(
    ("rows", "after_first"),
    [
        ([dict(id=1, superseded_by=2), dict(id=2, superseded_by=1)], {2: 1}),  # two-note cycle
        ([dict(id=1, superseded_by=1)], {}),  # self pointer
        (
            [dict(id=1, superseded_by=3), dict(id=2, superseded_by=1), dict(id=3, superseded_by=2)],
            {2: 1, 3: 2},
        ),
        ([dict(id=1, superseded_by=2), dict(id=2, superseded_by=2)], {2: 2}),
    ],
)
def test_planted_cycles_terminate_and_never_reinstate(
    gw: FakeGateway, rows: list[dict[str, Any]], after_first: dict[int, int]
) -> None:
    seed(gw, rows).close()
    ids = [str(r["id"]) for r in rows]
    assert run(gw, "delete", "--yes", "--json", ids[0]) == 0
    assert json.loads(gw.terminal.stdout)["reinstated"] == []
    assert pointers(gw) == after_first  # nothing cleared, nothing invented
    if ids[1:]:
        assert run(gw, "delete", "--yes", "--json", *ids[1:]) == 0
        assert json.loads(gw.terminal.stdout)["reinstated"] == []
    assert pointers(gw) == {}


@pytest.mark.parametrize("bad", [0, -3, 10**18])
def test_an_invalid_pointer_leaves_the_note_hidden(gw: FakeGateway, bad: int) -> None:
    # 1 -> 2 -> invalid: the chain above 2 cannot be followed, so 1 stays hidden (never
    # reinstated, never repointed), exactly as for a pointer to a missing note.
    seed(gw, [dict(id=1, superseded_by=2), dict(id=2, superseded_by=bad)]).close()
    assert run(gw, "delete", "2", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[2],"reinstated":[]}\n'
    assert pointers(gw) == {1: 2}


def test_pointer_from_a_higher_id_is_left_alone(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2, superseded_by=1)]).close()  # 2 replaced by the older 1
    assert run(gw, "delete", "1", "--yes", "--json") == 0
    assert json.loads(gw.terminal.stdout)["reinstated"] == []
    assert pointers(gw) == {2: 1}


def test_a_row_failing_its_checks_is_deleted_with_null_fields(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1)])
    add_note(conn, id=2, repo="bad repo!", topic="t", kind="k", body_sha256="zz", size=5)
    conn.close()
    assert run(gw, "delete", "2", "--yes", "--json") == 0
    [rec] = log_records(gw)
    assert (rec["repo"], rec["body_sha256"]) == (None, None)
    assert (rec["topic"], rec["kind"], rec["size"]) == ("t", "k", 5)
    assert note_ids(gw) == [1]


@pytest.mark.parametrize("field", ["repo", "topic", "kind", "body_sha256"])
def test_a_field_over_one_mib_is_a_failed_check_and_the_row_is_deletable(
    gw: FakeGateway, field: str
) -> None:
    conn = seed(gw, [])
    add_note(conn, id=1, **{field: "a" * ((1 << 20) + 10)})
    conn.close()
    gw.terminal.tty_answer = "n"
    assert run(gw, "delete", "1") == 13  # the prompt could be built: no db_corrupt
    assert run(gw, "delete", "1", "--yes", "--json") == 0
    [rec] = log_records(gw)
    assert rec[field if field != "body_sha256" else "body_sha256"] is None
    assert note_ids(gw) == []


@pytest.mark.parametrize(
    ("body", "size", "logged"),
    [("hello", 99, None), ("hello", 5, 5), ("\u00e9", 2, 2), ("\u00e9", 1, None)],
)
def test_log_size_is_null_when_it_disagrees_with_the_body(
    gw: FakeGateway, body: str, size: int, logged: int | None
) -> None:
    conn = seed(gw, [])
    add_note(conn, id=1, body=body, size=size)
    conn.close()
    assert run(gw, "delete", "1", "--yes") == 0
    [rec] = log_records(gw)
    assert rec["size"] == logged


def test_bad_row_prompt_never_prints_its_text(gw: FakeGateway) -> None:
    add_note(seed(gw, []), id=1, repo="bad\x1b[31m repo", topic="t\x00")
    gw.terminal.tty_answer = "n"
    assert run(gw, "delete", "1") == 13
    assert gw.terminal.tty_prompts == ["Delete note 1 (?, ?)? [y/N] "]


def test_huge_body_row_is_deleted_without_reading_it(gw: FakeGateway) -> None:
    add_note(seed(gw, []), id=1, body="x" * 60000)
    assert run(gw, "delete", "1", "--yes") == 0
    assert note_ids(gw) == []


# --- the log ----------------------------------------------------------------------------------


def test_log_failure_is_a_warning_and_exit_0(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    (db_path(gw).parent / "log.jsonl").mkdir()  # not a file of ours: the append fails
    assert run(gw, "delete", "1", "--yes", "--json") == 0
    assert gw.terminal.stdout == '{"deleted":[1],"reinstated":[]}\n'
    assert gw.terminal.stderr.startswith("warning (log_failed): ")
    assert note_ids(gw) == []
    assert run(gw, "delete", "1", "--yes") == 10  # a retry: already deleted


@pytest.mark.parametrize(
    ("command", "line"),
    [
        ("add", "warning (log_failed): " + WARNING_MESSAGES["log_failed"]),
        ("delete", DELETE_LOG_FAILED.rstrip("\n")),
    ],
)
def test_log_failed_wordings_are_in_the_interface_table(command: str, line: str) -> None:
    text = (Path(__file__).parent.parent / "docs" / "interface.md").read_text()
    assert f"| `log_failed` | `{command}` | `{line}` |" in text


def test_quiet_does_not_hide_the_warning(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    (db_path(gw).parent / "log.jsonl").mkdir()
    assert run(gw, "delete", "1", "--yes", "--quiet") == 0
    assert gw.terminal.stdout == "" and "log_failed" in gw.terminal.stderr


# --- goldens ----------------------------------------------------------------------------------


def test_goldens(gw: FakeGateway) -> None:
    chain(gw)
    seed_more = sqlite3.connect(db_path(gw))
    add_note(seed_more, id=4, superseded_by=5, expires=NOW)
    add_note(seed_more, id=5)
    seed_more.commit()
    seed_more.close()
    run(gw, "delete", "3", "5", "--yes", "--json")
    check_golden("delete/reinstated.json", gw.terminal.stdout.encode(), schema="cli/delete")
    chain_gw = FakeGateway(gw.root.parent / "gw2")
    chain(chain_gw)
    run(chain_gw, "delete", "3", "--yes")
    check_golden("delete/reinstated.txt", chain_gw.terminal.stdout.encode())
    gw3 = FakeGateway(gw.root.parent / "gw3")
    seed(gw3, [dict(id=1, superseded_by=2, expires=NOW), dict(id=2)]).close()
    run(gw3, "delete", "2", "--yes")
    check_golden("delete/expired.txt", gw3.terminal.stdout.encode())
    run(gw3, "delete", "7", "--yes", "--json")
    check_golden("delete/not-found.json", gw3.terminal.stdout.encode(), schema="cli/error")
    gw3.terminal.tty_answer = None
    add = sqlite3.connect(db_path(gw3))
    add_note(add, id=10)
    add.commit()
    add.close()
    run(gw3, "delete", "10", "--json")
    check_golden("delete/needs-human.json", gw3.terminal.stdout.encode(), schema="cli/error")
    run(gw3, "delete", "10")
    check_golden("delete/needs-human-err.txt", gw3.terminal.stderr.encode())


def test_epoch_is_what_the_expiry_compares_with() -> None:
    assert NOW == int(EPOCH.timestamp())


# --- the deadline and the transaction ----------------------------------------------------------


def test_a_failure_inside_the_transaction_rolls_everything_back(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memoctl import cmd_delete
    from memoctl.store import StoreError

    chain(gw)
    before = state(gw)
    real = cmd_delete.apply_delete

    def apply_then_time_out(store: Any, plan: Any) -> None:
        real(store, plan)  # deletes and repoints are done, the commit is not
        raise StoreError("timeout")

    monkeypatch.setattr(cmd_delete, "apply_delete", apply_then_time_out)
    assert run(gw, "delete", "3", "2", "--yes", "--json") == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "timeout"
    assert state(gw) == before


def test_a_deadline_that_runs_out_during_the_commit_exits_12_with_no_log_record(
    gw: FakeGateway,
) -> None:
    from tests.fake_gateway import FakeExit

    seed(gw, [dict(id=1)]).close()
    orig = gw.alarm_arm

    def arm(seconds: float, on_alarm: Any) -> Any:
        alarm = orig(seconds, on_alarm)
        gw.deadline_alarm = alarm  # type: ignore[attr-defined]
        return alarm

    gw.alarm_arm = arm  # type: ignore[method-assign]
    import memoctl.store as store_mod

    real_write = store_mod.Store.write

    from contextlib import contextmanager

    @contextmanager
    def write(self: Any) -> Any:
        with real_write(self):
            yield
        gw.deadline_alarm.fired = True  # type: ignore[attr-defined]  # ran out while it committed
        gw.mono += 100

    store_mod.Store.write = write  # type: ignore[method-assign]
    try:
        with pytest.raises(FakeExit) as info:
            run(gw, "delete", "1", "--yes", "--json")
    finally:
        store_mod.Store.write = real_write  # type: ignore[method-assign]
    assert info.value.code == 12
    assert note_ids(gw) == []  # stored: the new state, never a mix
    assert not (db_path(gw).parent / "log.jsonl").exists()
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "timeout"
    assert run(gw, "delete", "1", "--yes") == 10  # the documented retry result
