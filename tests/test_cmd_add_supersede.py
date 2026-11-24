"""``memoctl add``: the dedupe check (step 10) and ``--supersedes`` (steps 11 and 13).

Private home throughout (the fake gateway's, or a temp dir for the child processes).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from memoctl import store as store_mod
from memoctl.store import NoteRow
from tests import private_home
from tests.fake_gateway import FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_add import (
    KIND_LINE,
    REPO,
    add,
    error_of,
    log_records,
    state_dir,
    stored,
    write_config,
)

__all__ = ["gw"]  # the fixture below shadows the one in test_cmd_add


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def rows(gw: FakeGateway, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def sql(gw: FakeGateway, statement: str, *params: Any) -> None:
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def by_id(gw: FakeGateway, note_id: int) -> NoteRow:
    [row] = [r for r in stored(gw) if r.id == note_id]
    return row


def warned(doc: dict[str, Any]) -> list[str]:
    """The warnings other than ``kind_not_shown`` (these tests use the default kind)."""
    return [w for w in doc["warnings"] if w != "kind_not_shown"]


def first(gw: FakeGateway, **kwargs: Any) -> int:
    code, out, _ = add(gw, **kwargs)
    assert code == 0, out
    return int(json.loads(out)["id"])


# -- dedupe without --supersedes -----------------------------------------------------------------


def test_the_same_call_twice_returns_the_stored_note_with_created_false(gw: FakeGateway) -> None:
    code, out1, _ = add(gw, "--label", "a=1", "--kind", "implement")
    assert code == 0
    gw.clock.advance(timedelta(days=2))
    code, out2, err = add(gw, "--label", "a=1", "--kind", "implement", "--expires", "1h")
    assert (code, err) == (0, "")
    doc1, doc2 = json.loads(out1), json.loads(out2)
    assert doc2["created"] is False and doc2["id"] == doc1["id"] == 1
    assert doc2["expires"] == doc1["expires"]  # the stored lifetime, --expires is not applied
    assert {k: v for k, v in doc2.items() if k != "created"} == {
        k: v for k, v in doc1.items() if k != "created"
    }
    check_golden("add/exists.json", out2.encode(), schema="cli/add")


def test_a_hit_writes_nothing_no_note_no_rate_row_no_log_record(gw: FakeGateway) -> None:
    add(gw)
    before = rows(gw, "SELECT * FROM rate")
    log_before = log_records(gw)
    add(gw)
    add(gw)
    assert len(stored(gw)) == 1
    assert rows(gw, "SELECT * FROM rate") == before
    assert log_records(gw) == log_before and len(log_before) == 1


def test_a_hit_prints_the_exists_line(gw: FakeGateway) -> None:
    add(gw)
    code, out, err = add(gw, json_mode=False)
    assert (code, out, err) == (0, "note 1 exists\n", KIND_LINE)
    check_golden("add/exists.txt", out.encode())


def test_a_hit_with_quiet_prints_nothing(gw: FakeGateway) -> None:
    add(gw)
    assert add(gw, "--quiet", json_mode=False)[:2] == (0, "")


@pytest.mark.parametrize(
    "change",
    [
        {"repo": "example.com/team/other"},
        {"topic": "other"},
        {"body": "a note body"},  # no trailing LF: exact bytes
        {"body": "A note body\n"},
        {"body": " a note body\n"},
    ],
    ids=["repo", "topic", "no-lf", "case", "leading-space"],
)
def test_a_different_key_is_a_new_note(gw: FakeGateway, change: dict[str, Any]) -> None:
    add(gw)
    code, out, _ = add(gw, **change)
    assert code == 0 and json.loads(out)["created"] is True
    assert len(stored(gw)) == 2


@pytest.mark.parametrize(
    "extra",
    [["--kind", "review"], ["--label", "a=1"]],
    ids=["kind", "label"],
)
def test_a_different_kind_or_label_set_is_a_new_note(gw: FakeGateway, extra: list[str]) -> None:
    add(gw, "--label", "b=2", "--kind", "implement")
    code, out, _ = add(gw, "--label", "b=2", "--kind", "implement", *extra)
    assert json.loads(out)["created"] is True
    code, out, _ = add(gw, "--kind", "implement")  # a subset of the labels is not the same set
    assert json.loads(out)["created"] is True
    assert len(stored(gw)) == 3


def test_label_order_and_repeats_do_not_matter(gw: FakeGateway) -> None:
    add(gw, "--label", "b=2", "--label", "a=1")
    code, out, _ = add(gw, "--label", "a=1", "--label", "b=2", "--label", "a=1")
    assert json.loads(out)["created"] is False and len(stored(gw)) == 1


def test_a_note_with_extra_labels_is_not_a_hit_for_a_smaller_set(gw: FakeGateway) -> None:
    add(gw, "--label", "a=1", "--label", "b=2")
    assert json.loads(add(gw, "--label", "a=1")[1])["created"] is True


def test_bodies_differing_by_unicode_form_are_different_notes(gw: FakeGateway) -> None:
    add(gw, body="caf\u00e9\n")  # NFC
    code, out, _ = add(gw, body="\uff43af\u00e9\n")  # a fullwidth c: equal under NFKC only
    assert json.loads(out)["created"] is True
    assert len(stored(gw)) == 2


def test_a_hit_is_the_highest_id_of_the_live_notes_with_the_key(gw: FakeGateway) -> None:
    add(gw)
    # a planted second live row with the same key (the tool never writes one)
    sql(
        gw,
        "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
        " SELECT repo, topic, kind, body, body_sha256, size, created, expires FROM notes",
    )
    code, out, _ = add(gw)
    assert json.loads(out)["id"] == 2 and json.loads(out)["created"] is False


@pytest.mark.parametrize("state", ["expired", "superseded"])
def test_an_expired_or_superseded_note_is_not_a_hit(gw: FakeGateway, state: str) -> None:
    add(gw)
    if state == "expired":
        gw.clock.advance(timedelta(days=31))
    else:
        add(gw, "--supersedes", "1", body="other")
    code, out, _ = add(gw)
    doc = json.loads(out)
    assert code == 0 and doc["created"] is True and doc["id"] != 1


def test_a_deleted_note_is_not_a_hit(gw: FakeGateway) -> None:
    add(gw)
    sql(gw, "DELETE FROM notes WHERE id = 1")
    doc = json.loads(add(gw)[1])
    assert doc["created"] is True and doc["id"] == 2


def test_a_hit_never_crosses_repos_and_leaks_no_id(gw: FakeGateway) -> None:
    add(gw, repo="example.com/secret/repo", body="shared text")
    code, out, _ = add(gw, repo="example.com/other/repo", body="shared text")
    doc = json.loads(out)
    assert doc["created"] is True and doc["id"] == 2 and doc["repo"] == "example.com/other/repo"


def test_a_hit_returns_the_note_whoever_wrote_it_with_its_own_kind_and_lifetime(
    gw: FakeGateway,
) -> None:
    add(gw, "--kind", "review", "--expires", "2h")
    code, out, _ = add(gw, "--kind", "review", "--expires", "20d")
    doc = json.loads(out)
    assert doc["created"] is False
    assert doc["expires"] == "2026-01-01T02:00:00Z"
    assert warned(doc) == []  # --expires 20d is within the cap: nothing to warn about


def test_a_hit_warnings_come_from_this_call_not_the_stored_note(gw: FakeGateway) -> None:
    add(gw, "--kind", "implement")
    code, out, _ = add(gw, "--kind", "implement", "--expires", "400d")
    assert json.loads(out)["warnings"] == ["expires_capped"]
    write_config(gw, "context:\n  author_kinds: [review]\n")
    code, out, _ = add(gw, "--kind", "implement")
    assert json.loads(out)["warnings"] == ["kind_not_shown"]


def test_a_hit_reports_supersedes_of_the_note_it_replaced(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    doc = json.loads(add(gw, body="new")[1])
    assert doc["created"] is False and doc["id"] == 2 and doc["supersedes"] == 1


def test_a_planted_corrupt_matching_row_is_skipped_not_returned(gw: FakeGateway) -> None:
    add(gw)
    sql(gw, "UPDATE notes SET size = 99")  # fails the row checks
    code, out, _ = add(gw)
    assert code == 0 and json.loads(out)["created"] is True


# -- --supersedes --------------------------------------------------------------------------------


def test_supersede_creates_the_note_and_marks_the_target(gw: FakeGateway) -> None:
    add(gw, body="old")
    code, out, err = add(gw, "--supersedes", "1", body="new")
    assert (code, err) == (0, KIND_LINE)
    doc = json.loads(out)
    assert doc["id"] == 2 and doc["created"] is True and doc["supersedes"] == 1
    check_golden("add/superseded.json", out.encode(), schema="cli/add")
    assert by_id(gw, 1).superseded_by == 2 and by_id(gw, 2).superseded_by is None
    [_, second] = log_records(gw)
    assert second["event"] == "add" and second["note_id"] == 2 and second["supersedes"] == 1
    assert len(rows(gw, "SELECT * FROM rate")) == 2  # one rate row per created note


def test_supersede_with_the_same_body_is_the_refresh(gw: FakeGateway) -> None:
    add(gw, "--expires", "1h")
    gw.clock.advance(timedelta(minutes=30))
    code, out, _ = add(gw, "--supersedes", "1", "--expires", "1h")
    doc = json.loads(out)
    assert doc["created"] is True and doc["id"] == 2 and doc["supersedes"] == 1
    assert doc["expires"] == "2026-01-01T01:30:00Z"  # fresh lifetime, longer than the target's


def test_the_replacement_never_expires_before_the_target(gw: FakeGateway) -> None:
    add(gw, "--expires", "10d")
    code, out, _ = add(gw, "--supersedes", "1", "--expires", "2h", body="new")
    doc = json.loads(out)
    assert doc["expires"] == "2026-01-11T00:00:00Z"
    assert warned(doc) == ["expires_extended"]
    assert by_id(gw, 2).expires == by_id(gw, 1).expires


def test_a_longer_own_lifetime_is_not_extended(gw: FakeGateway) -> None:
    add(gw, "--expires", "2h")
    doc = json.loads(add(gw, "--supersedes", "1", "--expires", "5d", body="new")[1])
    assert warned(doc) == []


def test_an_expired_target_may_be_superseded(gw: FakeGateway) -> None:
    add(gw)
    gw.clock.advance(timedelta(days=40))
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    doc = json.loads(out)
    assert code == 0 and doc["created"] is True and warned(doc) == []
    assert by_id(gw, 1).superseded_by == 2


def test_the_target_is_checked_for_existence_first_exit_10(gw: FakeGateway) -> None:
    add(gw)
    code, out, err = add(gw, "--supersedes", "7", body="new")
    assert code == 10
    e = error_of(out)
    assert e["code"] == "not_found" and e["details"] == {"id": 7}
    assert err == "not_found: note not found\n"
    assert len(stored(gw)) == 1 and len(log_records(gw)) == 1
    assert len(rows(gw, "SELECT * FROM rate")) == 1


def test_a_missing_target_in_an_empty_store_is_not_found_and_creates_no_note(
    gw: FakeGateway,
) -> None:
    code, out, _ = add(gw, "--supersedes", "999999999999999999")
    assert code == 10 and error_of(out)["details"] == {"id": 999999999999999999}
    assert stored(gw) == [] and log_records(gw) == []


def test_a_target_in_another_repo_is_supersede_mismatch(gw: FakeGateway) -> None:
    add(gw, repo="example.com/team/zzz", body="old")
    code, out, err = add(gw, "--supersedes", "1", body="new")
    e = error_of(out)
    assert (code, e["reason"], e["details"]) == (20, "supersede_mismatch", None)
    assert err == "refused (supersede_mismatch): " + e["message"] + "\n"
    assert "zzz" not in out + err
    assert by_id(gw, 1).superseded_by is None and len(stored(gw)) == 1


def test_an_already_superseded_target_is_refused_even_when_expired(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    code, out, _ = add(gw, "--supersedes", "1", body="third")
    e = error_of(out)
    assert (code, e["reason"], e["details"]) == (20, "already_superseded", None)
    assert len(stored(gw)) == 2
    gw.clock.advance(timedelta(days=90))
    assert add(gw, "--supersedes", "1", body="fourth")[0] == 20


def test_mismatch_is_checked_before_already_superseded(gw: FakeGateway) -> None:
    add(gw, repo="example.com/team/other", body="old")
    add(gw, "--supersedes", "1", repo="example.com/team/other", body="newer")
    code, out, _ = add(gw, "--supersedes", "1", body="x")  # other repo AND superseded
    assert (code, error_of(out)["reason"]) == (20, "supersede_mismatch")


def test_a_chain_of_supersedes_in_a_row(gw: FakeGateway) -> None:
    add(gw, body="v0")
    for n in range(1, 25):
        gw.clock.advance(timedelta(minutes=1))  # stay under the rate limit (20 per 10 minutes)
        code, out, _ = add(gw, "--supersedes", str(n), body=f"v{n}")
        assert code == 0 and json.loads(out)["id"] == n + 1
    assert [(r.id, r.superseded_by) for r in stored(gw)][:3] == [(1, 2), (2, 3), (3, 4)]
    assert stored(gw)[-1].superseded_by is None
    assert add(gw, "--supersedes", "10", body="late")[0] == 20  # the middle is superseded


def test_a_note_cannot_supersede_its_own_successor_or_itself(gw: FakeGateway) -> None:
    add(gw, body="a")
    add(gw, "--supersedes", "1", body="b")
    assert add(gw, "--supersedes", "1", body="c")[0] == 20  # the replaced note is not a target
    assert add(gw, "--supersedes", "3", body="d")[0] == 10  # the id the call would get: absent
    assert [(r.id, r.superseded_by) for r in stored(gw)] == [(1, 2), (2, None)]


def test_a_planted_dangling_superseded_by_is_already_superseded(gw: FakeGateway) -> None:
    add(gw)
    sql(gw, "UPDATE notes SET superseded_by = 777 WHERE id = 1")
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (20, "already_superseded")


def test_a_corrupt_target_row_is_db_corrupt_and_nothing_is_written(gw: FakeGateway) -> None:
    add(gw)
    sql(gw, "UPDATE notes SET expires = -5 WHERE id = 1")
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (12, "db_corrupt")
    assert len(rows(gw, "SELECT * FROM notes")) == 1


def test_the_id_ceiling_with_a_supersede_adds_nothing_and_marks_nothing(gw: FakeGateway) -> None:
    add(gw)
    sql(gw, "UPDATE sqlite_sequence SET seq = 999999999999999999 WHERE name = 'notes'")
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (12, "db_corrupt")
    assert rows(gw, "SELECT id, superseded_by FROM notes") == [(1, None)]


def test_a_failed_mark_rolls_back_the_new_note(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(gw)

    def failing(self: Any, *args: Any, **kwargs: Any) -> None:
        raise store_mod.StoreError("db_unreadable")

    monkeypatch.setattr(store_mod.Store, "mark_superseded", failing)
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (12, "db_unreadable")
    monkeypatch.undo()
    assert [(r.id, r.superseded_by) for r in stored(gw)] == [(1, None)]
    assert len(rows(gw, "SELECT * FROM rate")) == 1 and len(log_records(gw)) == 1


def test_the_target_is_checked_inside_the_write_transaction(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the dedupe and the supersede checks read the target while the write lock is held
    (``BEGIN IMMEDIATE``), so another writer cannot change it between check and mark."""
    add(gw, body="old")
    original = store_mod.Store.get_note
    seen: list[bool] = []

    def spy(self: Any, note_id: int) -> Any:
        seen.append(self.in_transaction)
        return original(self, note_id)

    monkeypatch.setattr(store_mod.Store, "get_note", spy)
    assert add(gw, "--supersedes", "1", body="new")[0] == 0
    assert len(seen) >= 2  # the dedupe lookup and the supersede check
    assert all(seen)


# -- retry of a supersede (dedupe b) -------------------------------------------------------------


def test_a_retry_of_a_supersede_returns_the_note_it_created(gw: FakeGateway) -> None:
    add(gw, body="old")
    first_out = json.loads(add(gw, "--supersedes", "1", "--label", "a=1", body="new")[1])
    gw.clock.advance(timedelta(hours=1))
    code, out, _ = add(gw, "--supersedes", "1", "--label", "a=1", "--expires", "1h", body="new")
    doc = json.loads(out)
    assert code == 0 and doc["created"] is False and doc["id"] == 2 == first_out["id"]
    assert doc["supersedes"] == 1 and doc["expires"] == first_out["expires"]
    assert "expires_extended" not in doc["warnings"]
    assert len(stored(gw)) == 2 and len(log_records(gw)) == 2


def test_a_retry_that_would_have_extended_still_reports_no_extension(gw: FakeGateway) -> None:
    add(gw, "--expires", "10d")
    add(gw, "--supersedes", "1", "--expires", "2h", body="new")  # extended
    doc = json.loads(add(gw, "--supersedes", "1", "--expires", "2h", body="new")[1])
    assert doc["created"] is False and warned(doc) == []


def test_a_retry_with_a_different_body_is_already_superseded(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    code, out, _ = add(gw, "--supersedes", "1", body="different")
    assert (code, error_of(out)["reason"]) == (20, "already_superseded")


def test_a_retry_after_the_replacement_expired_is_already_superseded(gw: FakeGateway) -> None:
    add(gw, "--expires", "1h", body="old")
    add(gw, "--supersedes", "1", "--expires", "1h", body="new")
    gw.clock.advance(timedelta(hours=2))
    code, out, _ = add(gw, "--supersedes", "1", "--expires", "1h", body="new")
    assert (code, error_of(out)["reason"]) == (20, "already_superseded")


def test_a_retry_after_the_replacement_was_superseded_is_already_superseded(
    gw: FakeGateway,
) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    add(gw, "--supersedes", "2", body="newer")
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (20, "already_superseded")


def test_a_retry_after_the_replacement_was_deleted_is_a_new_target_state(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    sql(gw, "DELETE FROM notes WHERE id = 2")
    sql(gw, "UPDATE notes SET superseded_by = NULL WHERE id = 1")  # what delete does
    doc = json.loads(add(gw, "--supersedes", "1", body="new")[1])
    assert doc["created"] is True and doc["id"] == 3


def test_a_retry_whose_target_was_deleted_is_not_found(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new")
    sql(gw, "DELETE FROM notes WHERE id = 1")
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert code == 10 and error_of(out)["details"] == {"id": 1}


def test_with_supersedes_a_live_note_of_the_same_key_elsewhere_is_not_a_hit(
    gw: FakeGateway,
) -> None:
    add(gw, body="old")
    add(gw, body="same")  # live, id 2, key of the next call
    code, out, _ = add(gw, "--supersedes", "1", body="same")
    doc = json.loads(out)
    assert doc["created"] is True and doc["id"] == 3 and doc["supersedes"] == 1


def test_a_retry_names_a_replacement_of_another_key_as_no_hit(gw: FakeGateway) -> None:
    add(gw, body="old")
    add(gw, "--supersedes", "1", body="new", topic="t1")
    code, out, _ = add(gw, "--supersedes", "1", body="new", topic="t2")
    assert (code, error_of(out)["reason"]) == (20, "already_superseded")


def test_with_supersedes_a_missing_target_is_not_found_even_when_the_body_exists(
    gw: FakeGateway,
) -> None:
    """Dedupe (a) does not apply with --supersedes: a missing target is exit 10."""
    add(gw, body="same")
    code, out, _ = add(gw, "--supersedes", "999", body="same")
    assert code == 10  # (b) only applies when the target exists: no hit, then not found


# -- the real process: two adds of the same body at once ----------------------------------------


# A child runs the real entry point with its home replaced (tests/private_home.py): the tool
# takes the home from the password database, so a test process never touches the real home.


def _env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }


def test_two_processes_adding_the_same_body_insert_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    argv = private_home.argv(
        home, "add", "--repo", REPO, "--topic", "t", "--body-file", "-", "--json"
    )
    procs = [
        subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(home),
        )  # fmt: skip
        for _ in range(2)
    ]
    outputs = []
    try:
        for proc in procs:
            out, _err = proc.communicate(b"racing body\n", timeout=60)
            assert proc.returncode == 0
            outputs.append(json.loads(out))
    finally:
        for proc in procs:  # exactly the handles started here
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert sorted(doc["created"] for doc in outputs) == [False, True]
    assert {doc["id"] for doc in outputs} == {1}
    state = home / ".local/state/memoctl"
    conn = sqlite3.connect(state / "state.db")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM rate").fetchone()[0] == 1
    conn.close()
    assert len((state / "log.jsonl").read_text().splitlines()) == 1


def test_two_processes_superseding_one_target_create_one_replacement(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    base = private_home.argv(home, "add", "--repo", REPO, "--topic", "t")
    first = subprocess.run(
        [*base, "--body-file", "-"], input=b"old\n", capture_output=True, env=_env(home),
        timeout=60,
    )  # fmt: skip
    assert first.returncode == 0
    procs = [
        subprocess.Popen(
            [*base, "--body-file", "-", "--supersedes", "1", "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(home),
        )  # fmt: skip
        for _ in range(2)
    ]
    codes = []
    try:
        for proc in procs:
            proc.communicate(b"new\n", timeout=60)
            codes.append(proc.returncode)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert codes == [0, 0]  # the loser is a dedupe hit (a retry), never a second replacement
    conn = sqlite3.connect(home / ".local/state/memoctl/state.db")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    assert conn.execute("SELECT superseded_by FROM notes WHERE id = 1").fetchone() == (2,)
    conn.close()


def test_a_planted_target_of_another_repo_is_no_hit_and_leaks_no_id(gw: FakeGateway) -> None:
    add(gw, repo="example.com/team/zzz", body="old")  # id 1
    add(gw, body="new")  # id 2, in REPO
    sql(gw, "UPDATE notes SET superseded_by = 2 WHERE id = 1")  # planted cross-repo pointer
    code, out, _ = add(gw, "--supersedes", "1", body="new")
    assert (code, error_of(out)["reason"]) == (20, "supersede_mismatch")
    assert "zzz" not in out


# -- the hit's ``supersedes`` never leaks a note of another repo ---------------------------------


def _planted_foreign_pointer(gw: FakeGateway) -> None:
    add(gw, repo="example.com/team/zzz", body="foreign")  # 1, another repo
    add(gw, body="target")  # 2
    add(gw, body="x")  # 3, the note both 1 and 2 are planted to point at
    sql(gw, "UPDATE notes SET superseded_by = 3 WHERE id IN (1, 2)")


def test_a_supersede_retry_hit_reports_the_requested_target_not_a_foreign_id(
    gw: FakeGateway,
) -> None:
    _planted_foreign_pointer(gw)
    code, out, _ = add(gw, "--supersedes", "2", body="x")
    doc = json.loads(out)
    assert code == 0 and doc["created"] is False and doc["id"] == 3
    assert doc["supersedes"] == 2
    assert "zzz" not in out


def test_a_plain_hit_reports_the_replaced_note_of_its_own_repo_only(gw: FakeGateway) -> None:
    _planted_foreign_pointer(gw)
    doc = json.loads(add(gw, body="x")[1])
    assert doc["created"] is False and doc["id"] == 3 and doc["supersedes"] == 2


def test_a_plain_hit_with_only_a_foreign_pointer_reports_no_supersedes(gw: FakeGateway) -> None:
    _planted_foreign_pointer(gw)
    sql(gw, "UPDATE notes SET superseded_by = NULL WHERE id = 2")
    doc = json.loads(add(gw, body="x")[1])
    assert doc["created"] is False and doc["supersedes"] is None


def test_a_plain_hit_with_duplicate_same_repo_pointers_reports_the_smallest_id(
    gw: FakeGateway,
) -> None:
    add(gw, body="a")  # 1
    add(gw, body="b")  # 2
    add(gw, body="x")  # 3
    sql(gw, "UPDATE notes SET superseded_by = 3 WHERE id IN (1, 2)")
    assert json.loads(add(gw, body="x")[1])["supersedes"] == 1
