"""``list`` and ``show`` end to end through ``main`` on a fake gateway and a real store."""

from __future__ import annotations

import json
import os
import sqlite3
import tracemalloc
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memoctl import notes_read
from memoctl.cli import main
from memoctl.lifetime import format_timestamp
from memoctl.statedir import open_state_dir
from memoctl.store import Mode, open_store
from tests.fake_gateway import EPOCH, FakeExit, FakeGateway
from tests.golden_files import check_golden

SHA = "a" * 64
NOW = int(EPOCH.timestamp())
REPO = "example.com/a/b"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def db_path(gw: FakeGateway) -> Path:
    return gw.home() / ".local" / "state" / "memoctl" / "state.db"


def seed(
    gw: FakeGateway, notes: list[dict[str, Any]], labels: Sequence[tuple[int, str, str]] = ()
) -> sqlite3.Connection:
    state = open_state_dir(gw, create=True)
    with state:
        store = open_store(state, Mode.INIT)
        assert store is not None
        store.close()
    conn = sqlite3.connect(str(db_path(gw)), isolation_level=None)
    for n in notes:
        add_note(conn, **n)
    for note_id, key, value in labels:
        conn.execute("INSERT INTO note_labels VALUES (?,?,?)", (note_id, key, value))
    return conn


def add_note(conn: sqlite3.Connection, **over: Any) -> None:
    row: dict[str, Any] = dict(
        id=1, repo=REPO, topic="t", kind="unknown", body="hello", body_sha256=SHA, size=5,
        created=NOW - 100, expires=NOW + 1000, superseded_by=None,
    )  # fmt: skip
    row.update(over)
    if "size" not in over and isinstance(row["body"], str):
        row["size"] = len(row["body"].encode("utf-8", "surrogatepass"))
    conn.execute(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires,"
        " superseded_by) VALUES (:id,:repo,:topic,:kind,:body,:body_sha256,:size,:created,"
        ":expires,:superseded_by)",
        row,
    )


def run(gw: FakeGateway, *argv: str) -> int:
    gw.terminal.stdout = gw.terminal.stderr = ""
    return main(list(argv), gw)


def out_json(gw: FakeGateway) -> Any:
    return json.loads(gw.terminal.stdout)


def ids(gw: FakeGateway) -> list[int]:
    return [n["id"] for n in out_json(gw)["notes"]]


# --- list ------------------------------------------------------------------------------------


def test_missing_store_is_empty_and_not_created(gw: FakeGateway) -> None:
    assert run(gw, "list", "--json") == 0
    assert out_json(gw) == {"notes": [], "total": 0, "shown": 0, "hidden": 0}
    assert not (gw.home() / ".local").exists()
    assert run(gw, "list") == 0
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "no notes\n")


def test_newest_first_with_status_filter_and_hidden(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1),
            dict(id=2, superseded_by=3),
            dict(id=3),
            dict(id=4, expires=NOW),  # expires == now: expired
            dict(id=5, expires=NOW + 1),
        ],
    )
    assert run(gw, "list", "--json") == 0
    assert ids(gw) == [5, 3, 1]
    doc = out_json(gw)
    assert (doc["total"], doc["shown"], doc["hidden"]) == (3, 3, 2)
    assert gw.terminal.stderr == "2 superseded or expired notes hidden; use --all\n"
    assert run(gw, "list", "--all", "--json") == 0
    assert ids(gw) == [5, 4, 3, 2, 1]
    assert [n["status"] for n in out_json(gw)["notes"]] == [
        "live", "expired", "live", "superseded", "live",
    ]  # fmt: skip
    assert out_json(gw)["hidden"] == 0 and gw.terminal.stderr == ""


def test_superseded_wins_over_expired(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, superseded_by=2, expires=NOW - 5), dict(id=2)])
    run(gw, "list", "--all", "--json")
    assert [n["status"] for n in out_json(gw)["notes"]] == ["live", "superseded"]


def test_filters_repo_topic_kind_label(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, repo="example.com/x/y", topic="a", kind="k1"),
            dict(id=2, topic="a", kind="k1"),
            dict(id=3, topic="b", kind="k2"),
            dict(id=4, topic="a", kind="k2", superseded_by=1),
        ],
        [(2, "env", "ci"), (2, "team", "x"), (3, "env", "ci"), (4, "env", "ci")],
    )
    run(gw, "list", "--json", "--repo", REPO)
    assert ids(gw) == [3, 2]
    run(gw, "list", "--json", "--topic", "a")
    assert ids(gw) == [2, 1]
    run(gw, "list", "--json", "--kind", "k2")
    assert ids(gw) == [3]
    run(gw, "list", "--json", "--label", "env=ci")
    assert ids(gw) == [3, 2]
    run(gw, "list", "--json", "--label", "env=ci", "--label", "team=x")
    assert ids(gw) == [2]
    assert out_json(gw)["hidden"] == 0
    run(gw, "list", "--json", "--label", "env=ci", "--repo", REPO)  # 4 matches and is hidden
    assert out_json(gw)["hidden"] == 1
    run(gw, "list", "--json", "--label", "env=ci", "--label", "env=ci")  # repeat collapses
    assert ids(gw) == [3, 2]
    run(gw, "list", "--json", "--label", "env=")  # empty value is a valid, absent pair
    assert ids(gw) == []


def test_many_label_filters_do_not_grow_the_sql(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)])
    argv = [f"--label=k{i}=v" for i in range(3000)]
    assert run(gw, "list", "--json", *argv) == 0
    assert out_json(gw)["total"] == 0


def test_limit_total_shown(gw: FakeGateway) -> None:
    seed(gw, [dict(id=i) for i in range(1, 8)])
    run(gw, "list", "--json", "--limit", "3")
    doc = out_json(gw)
    assert ids(gw) == [7, 6, 5]
    assert (doc["total"], doc["shown"]) == (7, 3)
    run(gw, "list", "--limit", "3")
    assert gw.terminal.stderr == "3 of 7 shown\n"
    run(gw, "list", "--json", "--limit", "500")
    assert out_json(gw)["shown"] == 7


@pytest.mark.parametrize(
    "value", ["0", "-1", "501", "007", "1e2", "", " 5", "5\n", "99999999999999999999", "٣", "+5"]
)
def test_bad_limit_is_usage_limit(gw: FakeGateway, value: str) -> None:
    assert run(gw, "list", "--json", f"--limit={value}") == 2
    assert json.loads(gw.terminal.stdout)["error"]["details"] == {"field": "limit"}
    assert gw.terminal.stderr.startswith("usage: ")


@pytest.mark.parametrize(
    ("flag", "value", "field"),
    [
        ("--repo", "Bad", "repo"),
        ("--repo", "example.com/a\n", "repo"),
        ("--topic", "-x", "topic"),
        ("--topic", "x" * 65, "topic"),
        ("--kind", "A", "kind"),
        ("--label", "nokey", "label"),
        ("--label", "K=v", "label"),
        ("--label", "k=v w", "label"),
    ],
)
def test_bad_filter_is_usage_with_field(gw: FakeGateway, flag: str, value: str, field: str) -> None:
    assert run(gw, "list", "--json", f"{flag}={value}") == 2
    doc = json.loads(gw.terminal.stdout)
    assert doc["error"]["details"] == {"field": field}
    assert value not in gw.terminal.stdout + gw.terminal.stderr or value == ""


def test_list_refuses_dry_run_and_positional(gw: FakeGateway) -> None:
    assert run(gw, "list", "--dry-run") == 2
    assert run(gw, "list", "extra") == 2
    assert run(gw, "list", "--quiet", "--verbose") == 2


def test_quiet_drops_status_lines_but_keeps_data(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1), dict(id=2, superseded_by=1)])
    assert run(gw, "list", "--quiet") == 0
    assert gw.terminal.stdout.startswith("1  live  ")
    assert gw.terminal.stderr == ""
    run(gw, "list", "--quiet", "--json")
    assert out_json(gw)["hidden"] == 1 and gw.terminal.stderr == ""


def test_ties_and_order_are_deterministic(gw: FakeGateway) -> None:
    seed(gw, [dict(id=i, created=NOW - 1, expires=NOW + 9) for i in (3, 1, 2)])
    run(gw, "list", "--json")
    first = gw.terminal.stdout
    run(gw, "list", "--json")
    assert first == gw.terminal.stdout and ids(gw) == [3, 2, 1]


def test_labels_sorted_by_key_then_value(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)], [(1, "b", "2"), (1, "a", "9"), (1, "a", "1")])
    run(gw, "list", "--json")
    assert out_json(gw)["notes"][0]["labels"] == ["a=1", "a=9", "b=2"]


# --- hostile rows ----------------------------------------------------------------------------

ESC = "\x1b[31mred\x1b]0;title\x07‮\x00"


def hostile_rows(gw: FakeGateway) -> sqlite3.Connection:
    conn = seed(gw, [dict(id=1, body="fine")])
    add_note(conn, id=2, repo="Bad Repo\x1b[2J")  # grammar
    add_note(conn, id=3, topic="t\n2  live  forged")
    add_note(conn, id=4, size=99)  # wrong size
    add_note(conn, id=5, expires=10**12)  # timestamp past the printable range
    conn.execute(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (6, ?, 't', 'unknown', CAST(X'ffff' AS TEXT), ?, 2, 1, 2000000000)",
        (REPO, SHA),
    )  # undecodable text
    add_note(conn, id=7, body="x" * 10, superseded_by=0)  # id out of range
    add_note(conn, id=8, kind="k\x1b]0;x\x07")
    add_note(conn, id=9, body=ESC)  # body with escapes (list never prints it)
    conn.execute("INSERT INTO note_labels VALUES (9, 'ok', 'fine')")
    conn.execute("INSERT INTO note_labels VALUES (1, 'bad key', 'v')")  # makes note 1 bad
    add_note(conn, id=10)
    for i in range(9):
        conn.execute("INSERT INTO note_labels VALUES (10, ?, 'v')", (f"k{i}",))  # 9 labels
    add_note(conn, id=11, repo="example.com/ok", topic="fine", kind="ok")
    conn.execute("INSERT INTO note_labels VALUES (11, 'a', 'x\x1b[31m')")  # bad value
    add_note(conn, id=12, repo="example.com/ok", topic="fine", kind="ok")
    return conn


def test_hostile_rows_are_skipped_and_never_raw(gw: FakeGateway) -> None:
    hostile_rows(gw)
    assert run(gw, "list", "--json", "--all") == 0
    doc = out_json(gw)
    assert [n["id"] for n in doc["notes"]] == [12, 9]
    assert (doc["total"], doc["shown"], doc["hidden"]) == (2, 2, 0)
    assert "\x1b" not in gw.terminal.stdout
    assert run(gw, "list", "--all") == 0
    for ch in gw.terminal.stdout + gw.terminal.stderr:
        assert ch in "\n" or 0x20 <= ord(ch) < 0x7F
    assert len(gw.terminal.stdout.splitlines()) == 2


def test_bad_rows_do_not_reach_counts_or_hidden(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1)])
    add_note(conn, id=2, superseded_by=1, repo="Bad")
    assert run(gw, "list", "--json") == 0
    assert out_json(gw)["hidden"] == 0


def test_value_over_one_mib_fails_the_statement(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1)])
    conn.execute("UPDATE notes SET body = ?", ("x" * 1_100_000,))
    assert run(gw, "list", "--json") == 12
    err = json.loads(gw.terminal.stdout)["error"]
    assert (err["code"], err["reason"]) == ("failed", "db_corrupt")
    assert run(gw, "show", "1") == 12


def test_foreign_database_is_corrupt(gw: FakeGateway) -> None:
    path = db_path(gw)
    path.parent.mkdir(parents=True, mode=0o700)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()
    path.chmod(0o600)
    assert run(gw, "list", "--json") == 12
    assert json.loads(gw.terminal.stdout)["error"]["reason"] == "db_corrupt"
    assert gw.terminal.stderr == "failed (db_corrupt): database is corrupt\n"


def test_read_never_creates_or_writes(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    before = db_path(gw).read_bytes()
    run(gw, "list")
    run(gw, "show", "1")
    assert db_path(gw).read_bytes() == before


# --- show ------------------------------------------------------------------------------------


def test_show_json_and_supersedes_chain(gw: FakeGateway) -> None:
    seed(
        gw,
        [dict(id=1, superseded_by=2, body="old"), dict(id=2, body="new\nlines\n")],
        [(2, "b", "2"), (2, "a", "1")],
    )
    assert run(gw, "show", "2", "--json") == 0
    doc = out_json(gw)
    assert doc["supersedes"] == 1 and doc["superseded_by"] is None
    assert doc["labels"] == ["a=1", "b=2"] and doc["body"] == "new\nlines\n"
    assert doc["status"] == "live"
    run(gw, "show", "1", "--json")
    doc = out_json(gw)
    assert (doc["status"], doc["superseded_by"], doc["supersedes"]) == ("superseded", 2, None)


def test_show_expired_is_shown(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, expires=NOW - 1)])
    assert run(gw, "show", "1", "--json") == 0
    assert out_json(gw)["status"] == "expired"


def test_show_unknown_id_is_not_found(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)])
    assert run(gw, "show", "2", "--json") == 10
    err = out_json(gw)["error"]
    assert (err["code"], err["reason"], err["details"]) == ("not_found", None, {"id": 2})
    assert gw.terminal.stderr == "not_found: note not found\n"
    assert run(gw, "show", "2") == 10
    assert gw.terminal.stdout == ""


def test_show_missing_store_is_not_found(gw: FakeGateway) -> None:
    assert run(gw, "show", "7") == 10
    assert not (gw.home() / ".local").exists()


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "007", "1000000000000000000", "99999999999999999999999", "x", "1.5", "٣", "1 "],
)
def test_show_bad_id_is_usage_id(gw: FakeGateway, value: str) -> None:
    assert run(gw, "show", "--json", "--", value) == 2
    assert out_json(gw)["error"]["details"] == {"field": "id"}


def test_show_id_arity(gw: FakeGateway) -> None:
    assert run(gw, "show") == 2
    assert run(gw, "show", "1", "2") == 2
    assert run(gw, "show", "-x") == 2


def test_show_largest_id_not_found(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)])
    assert run(gw, "show", "999999999999999999") == 10


@pytest.mark.parametrize("bad", ["repo", "size", "body", "label", "sha", "kind"])
def test_show_bad_row_is_db_corrupt(gw: FakeGateway, bad: str) -> None:
    conn = seed(gw, [dict(id=1)])
    if bad == "repo":
        conn.execute("UPDATE notes SET repo = 'Bad'")
    elif bad == "size":
        conn.execute("UPDATE notes SET size = 6")
    elif bad == "body":
        conn.execute("UPDATE notes SET body = CAST(X'ff' AS TEXT)")
    elif bad == "label":
        conn.execute("INSERT INTO note_labels VALUES (1, 'K', 'v')")
    elif bad == "sha":
        conn.execute("UPDATE notes SET body_sha256 = 'A'")
    else:
        conn.execute("UPDATE notes SET kind = 'a b'")
    assert run(gw, "show", "1", "--json") == 12
    assert out_json(gw)["error"]["reason"] == "db_corrupt"
    assert run(gw, "show", "1") == 12
    assert gw.terminal.stdout == ""


def test_show_body_goes_through_the_renderer(gw: FakeGateway) -> None:
    body = "a\x1b[2Jb\n‮txt\tz\x00\x9b\n\n"
    seed(gw, [dict(id=1, body=body)])
    assert run(gw, "show", "1") == 0
    out = gw.terminal.stdout
    for ch in out:
        assert ch == "\n" or 0x20 <= ord(ch) < 0x7F or ord(ch) > 0xA0, repr(ch)
    assert "\x1b" not in out and "‮" not in out and "\x00" not in out
    assert "\\u{001B}" in out and "\\u{202E}" in out
    assert out.endswith("|\n|\n")  # body ends with LF LF: two empty lines... final piece too
    run(gw, "show", "1", "--json")
    assert out_json(gw)["body"] == body  # JSON keeps the stored text, JSON-escaped
    assert "\x1b" not in gw.terminal.stdout and gw.terminal.stdout.isascii()


def test_show_lone_surrogate_in_body_cannot_exist_but_is_safe(gw: FakeGateway) -> None:
    # sqlite stores the CESU bytes; the strict decoder refuses them: db_corrupt, no output
    conn = seed(gw, [dict(id=1)])
    conn.execute("UPDATE notes SET body = CAST(X'eda080' AS TEXT), size = 3")
    assert run(gw, "show", "1") == 12
    assert gw.terminal.stdout == ""


def test_quiet_does_not_hide_show(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)])
    assert run(gw, "show", "1", "--quiet") == 0
    assert gw.terminal.stdout.startswith("note 1\n")


# --- goldens ---------------------------------------------------------------------------------


def golden_store(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, topic="build", kind="human", body="use `make verify`\n\nthen commit\n",
                 superseded_by=3, created=NOW - 86400 * 2, expires=NOW + 86400),
            dict(id=2, repo="example.com/x/y", topic="old", body="expired note", expires=NOW - 1),
            dict(id=3, topic="build", kind="human", body="use `make verify` first\n",
                 created=NOW - 3600, expires=NOW + 86400 * 30),
            dict(id=4, topic="queue", body="the local queue must be running\twith tabs",
                 expires=NOW + 5000),
        ],
        [(3, "ticket", "T-1"), (3, "area", "ci"), (4, "area", "ci")],
    )  # fmt: skip


def test_goldens(gw: FakeGateway) -> None:
    golden_store(gw)
    cases = {
        "list/default.json": ("list", "--json"),
        "list/all.json": ("list", "--all", "--json"),
        "list/label.json": ("list", "--json", "--label", "area=ci", "--limit", "1"),
        "list/default.txt": ("list",),
        "list/all.txt": ("list", "--all"),
        "list/limited.txt": ("list", "--limit", "1"),
        "show/live.json": ("show", "3", "--json"),
        "show/superseded.json": ("show", "1", "--json"),
        "show/live.txt": ("show", "3"),
        "show/superseded.txt": ("show", "1"),
        "show/expired.txt": ("show", "2"),
    }
    for name, argv in cases.items():
        assert run(gw, *argv) == 0, name
        schema = "cli/list" if name.startswith("list/") else "cli/show"
        out = gw.terminal.stdout.encode()
        if name.endswith(".json"):
            check_golden(name, out, schema=schema)
        else:
            check_golden(name, out)
        if name.endswith(".txt"):
            check_golden(name.replace(".txt", "-err.txt"), gw.terminal.stderr.encode())


def test_goldens_errors(gw: FakeGateway) -> None:
    golden_store(gw)
    run(gw, "show", "99", "--json")
    check_golden("show/not-found.json", gw.terminal.stdout.encode(), schema="cli/error")
    run(gw, "list", "--limit", "0", "--json")
    check_golden("list/bad-limit.json", gw.terminal.stdout.encode(), schema="cli/error")


def test_hostile_golden(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1, body="line1\x1b[31m\n‮\x00\x85end")])
    conn.close()
    run(gw, "show", "1")
    check_golden("show/hostile.txt", gw.terminal.stdout.encode())
    assert b"\x1b" not in gw.terminal.stdout.encode()


# --- deadline and output ---------------------------------------------------------------------


def test_deadline_firing_during_the_scan_leaves_no_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(gw, [dict(id=i) for i in range(1, 30)])
    real = notes_read.check_note_row
    seen = 0

    def slow(values: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == 5:
            gw.advance_mono(31)  # the alarm fires inside the scan
        return real(values)

    monkeypatch.setattr(notes_read, "check_note_row", slow)
    with pytest.raises(FakeExit) as e:
        run(gw, "list", "--json")
    assert e.value.code == 12
    assert gw.terminal.stdout.count("\n") <= 1  # only the handler's envelope, no note output
    assert '"notes"' not in gw.terminal.stdout
    assert "deadline passed" in gw.terminal.stderr


def test_deadline_passed_before_output_exits_12(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(gw, [dict(id=1)])
    real = notes_read.fetch_note

    def late(store: Any, note_id: int, now: int) -> Any:
        result = real(store, note_id, now)
        gw.mono += 31  # the time ran out after the last read; the alarm has not run yet
        return result

    monkeypatch.setattr("memoctl.cmd_show.fetch_note", late)
    with pytest.raises(FakeExit) as e:
        run(gw, "show", "1", "--json")
    assert e.value.code == 12
    assert '"body"' not in gw.terminal.stdout


def test_output_failure_after_result_is_ignored(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)])
    del gw.fds[1]  # a closed stdout: ensure_standard_descriptors reopens /dev/null
    assert run(gw, "list") == 0


# --- size ------------------------------------------------------------------------------------


def test_hundred_thousand_notes_bounded_work_and_memory(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (?, ?, 't', 'unknown', ?, ?, 40, 1, 4000000000)",
        ((i, REPO if i % 2 else "example.com/z/z", "b" * 40, SHA) for i in range(1, 100_001)),
    )
    conn.execute("INSERT INTO note_labels SELECT id, 'k', 'v' FROM notes WHERE id % 10 = 0")
    conn.execute("COMMIT")
    conn.close()
    tracemalloc.start()
    try:
        assert run(gw, "list", "--json", "--limit", "500") == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    doc = out_json(gw)
    assert (doc["total"], doc["shown"]) == (100_000, 500)
    assert ids(gw)[:2] == [100_000, 99_999]
    assert peak < 8_000_000, peak
    run(gw, "list", "--json", "--repo", REPO, "--label", "k=v")
    assert out_json(gw)["total"] == 0  # odd ids carry no label: no match
    run(gw, "list", "--json", "--repo", "example.com/z/z", "--label", "k=v", "--limit", "2")
    assert out_json(gw)["total"] == 10_000


def test_timestamps_roundtrip() -> None:
    assert format_timestamp(0) == "1970-01-01T00:00:00Z"
    assert format_timestamp(253402300799) == "9999-12-31T23:59:59Z"
    assert datetime.fromtimestamp(NOW, UTC) == EPOCH


# --- state directory failures (exit 20 unsafe_state, exit 12 db_unreadable) -------------------

READ_ARGVS = [("list",), ("show", "1")]
READ_IDS = ["list", "show"]


def state_dir(gw: FakeGateway) -> Path:
    return db_path(gw).parent


@pytest.mark.parametrize("argv", READ_ARGVS, ids=READ_IDS)
def test_an_unsafe_state_directory_is_refused_20(gw: FakeGateway, argv: tuple[str, ...]) -> None:
    state_dir(gw).mkdir(parents=True)
    state_dir(gw).chmod(0o777)
    assert run(gw, *argv, "--json") == 20
    err = out_json(gw)["error"]
    assert (err["code"], err["reason"], err["details"]) == ("refused", "unsafe_state", None)
    assert gw.terminal.stderr == "refused (unsafe_state): state directory is not safe to use\n"


@pytest.mark.parametrize("argv", READ_ARGVS, ids=READ_IDS)
def test_an_unreachable_state_directory_is_exit_12(gw: FakeGateway, argv: tuple[str, ...]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    seed(gw, [dict(id=1)]).close()
    parent = state_dir(gw).parent
    parent.chmod(0o000)  # the state directory cannot be reached
    try:
        code = run(gw, *argv, "--json")
    finally:
        parent.chmod(0o700)
    assert code == 12
    err = out_json(gw)["error"]
    assert (err["code"], err["reason"]) == ("failed", "db_unreadable")


@pytest.mark.parametrize("argv", READ_ARGVS, ids=READ_IDS)
def test_json_output_is_one_compact_line_and_a_single_lf(
    gw: FakeGateway, argv: tuple[str, ...]
) -> None:
    seed(gw, [dict(id=1, body="a b")], [(1, "k", "v")])
    assert run(gw, *argv, "--json") == 0
    out = gw.terminal.stdout
    compact = json.dumps(json.loads(out), ensure_ascii=True, separators=(",", ":")) + "\n"
    assert out == compact
