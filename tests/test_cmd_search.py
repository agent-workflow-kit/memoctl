"""``search`` end to end through ``main`` on a fake gateway and a real store."""

from __future__ import annotations

import gc
import json
import random
import sqlite3
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from memoctl import notes_read, searchmatch
from memoctl.body import has_long_combining_run
from memoctl.searchmatch import Matcher, TermError, fold, parse_terms
from tests.fake_gateway import FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_read import NOW, REPO, SHA, add_note, db_path, ids, out_json, run, seed


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def search(gw: FakeGateway, *argv: str) -> int:
    return run(gw, "search", "--json", *argv)


def found(gw: FakeGateway, *argv: str) -> list[int]:
    assert search(gw, *argv) == 0, gw.terminal.stdout
    return ids(gw)


def usage_details(gw: FakeGateway) -> dict[str, Any]:
    err = out_json(gw)["error"]
    assert err["code"] == "usage"
    return err["details"]


# --- matching --------------------------------------------------------------------------------


def test_matches_body_or_topic_newest_first(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, topic="alpha", body="nothing"),
            dict(id=2, topic="beta", body="has Alpha inside"),
            dict(id=3, topic="gamma", body="nope"),
        ],
    )
    assert found(gw, "alpha") == [2, 1]
    doc = out_json(gw)
    assert (doc["total"], doc["shown"], doc["hidden"]) == (2, 2, 0)
    assert "body" not in doc["notes"][0]


def test_every_term_must_occur_each_in_topic_or_body(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, topic="queue", body="local runner"),
            dict(id=2, topic="queue", body="remote"),
            dict(id=3, topic="other", body="local runner queue"),
        ],
    )
    assert found(gw, "queue local") == [3, 1]
    assert found(gw, "runner local queue") == [3, 1]
    assert found(gw, "remote local") == []


def test_terms_never_match_across_topic_and_body(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, topic="ab", body="cd")])
    assert found(gw, "bc") == []


def test_case_unicode_normalization_and_casefold(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, body="Straße"),
            dict(id=2, body="ＦＵＬＬ width"),
            dict(id=3, body="café"),
            dict(id=4, body="plain"),
        ],
    )
    assert found(gw, "STRASSE") == [1]
    assert found(gw, "full") == [2]
    assert found(gw, "ｗｉｄｔｈ") == [2]
    assert found(gw, "café") == [3]
    assert found(gw, "café") == [3]


@pytest.mark.parametrize(
    ("query", "expect"),
    [(".*", []), ("a.c", [2]), ("[a-z]+", []), ("%", [2]), ("_", [2]), ("\\", [2]), ("a%", []),
     ("x_y", [2]), ("'; --", [2]), ('"', [2])],
)  # fmt: skip
def test_no_query_syntax(gw: FakeGateway, query: str, expect: list[int]) -> None:
    seed(gw, [dict(id=1, body="abc xyz"), dict(id=2, body="a.c 100% x_y \\ '; -- \"")])
    assert found(gw, "--", query) == expect


def test_like_wildcards_are_literal(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="ab"), dict(id=2, body="a%b")])
    assert found(gw, "a%b") == [2]
    assert found(gw, "a_") == []


def test_repeated_terms_collapse_before_the_count(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="abc")])
    assert found(gw, " ".join(["abc"] * 20)) == [1]
    assert found(gw, "ABC abc ａｂｃ") == [1]


def test_term_count_and_length_limits(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="abcdefgh " + "x" * 64)])
    assert found(gw, "a b c d e f g h") == [1]  # eight terms: allowed
    assert search(gw, "a b c d e f g h i") == 2
    assert usage_details(gw) == {"field": "text"}
    assert found(gw, "x" * 64) == [1]
    assert search(gw, "x" * 65) == 2
    assert usage_details(gw) == {"field": "text"}
    assert search(gw, "ß" * 33) == 2  # 66 code points after casefold


@pytest.mark.parametrize("query", ["", " ", " \t\n ", " 　"])
def test_empty_or_blank_text_is_usage_text(gw: FakeGateway, query: str) -> None:
    assert search(gw, "--", query) == 2
    assert usage_details(gw) == {"field": "text"}


@pytest.mark.parametrize(
    ("query", "cls"),
    [
        ("a\x00b", "control"),
        ("a\x1bb", "control"),
        ("a‮b", "invisible"),
        ("a​b", "invisible"),
        ("ab", "private_use"),
        ("a͸b", "unassigned"),
        ("e" + "́" * 31, "combining"),
    ],
)
def test_refused_text_classes(gw: FakeGateway, query: str, cls: str) -> None:
    assert search(gw, "--", query) == 2
    assert usage_details(gw) == {"field": "text", "class": cls, "line": 1}
    assert query not in gw.terminal.stdout + gw.terminal.stderr


def test_newline_in_text_separates_terms(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="one two")])
    assert found(gw, "one\ntwo") == [1]


def test_text_over_1024_bytes_is_refused(gw: FakeGateway) -> None:
    assert search(gw, "a" * 1025) == 2
    assert usage_details(gw) == {"field": "text", "limit": 1024}
    assert out_json(gw)["error"]["message"] == "the search text is over the size limit"


def test_lone_surrogate_and_non_utf8_argv(gw: FakeGateway) -> None:
    for bad in ("a\udcffb", "a\ud800b"):
        assert search(gw, "--", bad) == 2
        assert usage_details(gw)["class"] == "encoding"


def test_text_file_stdin_and_file(gw: FakeGateway, tmp_path: Path) -> None:
    seed(gw, [dict(id=1, body="needle here")])
    gw.terminal.stdin = "needle\n"
    assert found(gw, "--text-file", "-") == [1]
    (gw.root / "q.txt").write_text("NEEDLE")
    assert found(gw, "--text-file", "q.txt") == [1]
    gw.terminal.stdin = b"\xff\xfe"
    assert search(gw, "--text-file", "-") == 2
    assert usage_details(gw)["class"] == "encoding"
    gw.terminal.stdin = "a" * 1025
    assert search(gw, "--text-file", "-") == 2
    assert usage_details(gw) == {"field": "text", "limit": 1024}
    gw.terminal.stdin = "ab " * 341 + "a"  # 1024 bytes
    assert search(gw, "--text-file", "-") == 0
    gw.terminal.stdin = "x\x1b"
    assert search(gw, "--text-file", "-") == 2


def test_text_source_arity(gw: FakeGateway) -> None:
    gw.terminal.stdin = "x"
    assert search(gw) == 2
    assert usage_details(gw) == {"field": "argv"}
    assert search(gw, "x", "--text-file", "-") == 2
    assert usage_details(gw) == {"field": "argv"}
    assert gw.stdin_reads == 0
    assert search(gw, "a", "b") == 2


def test_dash_text_needs_double_dash(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="a -text b")])
    assert found(gw, "--", "-text") == [1]
    assert search(gw, "-text") == 2


def test_flags_and_refusals(gw: FakeGateway) -> None:
    assert run(gw, "search", "x", "--dry-run") == 2
    assert run(gw, "search", "x", "--topic", "t") == 2
    assert run(gw, "search", "x", "--quiet", "--verbose") == 2
    assert run(gw, "search", "x", "--limit", "0") == 2


# --- filters, status, counts -----------------------------------------------------------------


def test_status_hidden_repo_limit(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, body="hit"),
            dict(id=2, body="hit", superseded_by=3),
            dict(id=3, body="hit"),
            dict(id=4, body="hit", expires=NOW),
            dict(id=5, body="miss", expires=NOW),
            dict(id=6, body="hit", repo="example.com/x/y"),
            dict(id=7, body="hit"),
        ],
    )
    assert found(gw, "hit") == [7, 6, 3, 1]
    doc = out_json(gw)
    assert (doc["total"], doc["hidden"]) == (4, 2)  # only matching notes are counted hidden
    assert gw.terminal.stderr == "2 superseded or expired notes hidden; use --all\n"
    assert found(gw, "hit", "--all") == [7, 6, 4, 3, 2, 1]
    assert out_json(gw)["hidden"] == 0
    assert found(gw, "hit", "--repo", "example.com/x/y") == [6]
    assert out_json(gw)["hidden"] == 0
    assert found(gw, "hit", "--repo", REPO, "--limit", "2") == [7, 3]
    doc = out_json(gw)
    assert (doc["total"], doc["shown"], doc["hidden"]) == (3, 2, 2)
    assert run(gw, "search", "hit", "--repo", REPO, "--limit", "2") == 0
    assert gw.terminal.stderr == "2 of 3 shown\n2 superseded or expired notes hidden; use --all\n"


def test_no_match_and_missing_store_are_exit_0(gw: FakeGateway) -> None:
    assert found(gw, "x") == []
    assert not (gw.home() / ".local").exists()
    assert out_json(gw) == {"notes": [], "total": 0, "shown": 0, "hidden": 0}
    assert run(gw, "search", "x") == 0
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "no notes\n")
    seed(gw, [dict(id=1)])
    assert run(gw, "search", "zzz") == 0


def test_quiet_keeps_data(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="hit"), dict(id=2, body="hit", superseded_by=1)])
    assert run(gw, "search", "hit", "--quiet") == 0
    assert gw.terminal.stdout.startswith("1  live  ") and gw.terminal.stderr == ""
    run(gw, "search", "zzz", "--quiet")
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("", "")


def test_json_is_one_compact_line(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1, body="a b")], [(1, "k", "v")])
    assert search(gw, "a") == 0
    out = gw.terminal.stdout
    assert out == json.dumps(json.loads(out), ensure_ascii=True, separators=(",", ":")) + "\n"


# --- hostile rows and output -----------------------------------------------------------------


def test_hostile_rows_are_skipped_and_matches_never_raw(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1, body="fine hit")])
    add_note(conn, id=2, repo="Bad Repo\x1b[2J", body="hit")
    add_note(conn, id=3, topic="t\n2  live  forged", body="hit")
    add_note(conn, id=4, body="hit", size=99)
    conn.execute(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (6, ?, 't', 'unknown', CAST(X'ffff' AS TEXT), ?, 2, 1, 2000000000)",
        (REPO, SHA),
    )
    add_note(conn, id=8, kind="k\x1b]0;x\x07", body="hit")
    add_note(conn, id=9, body="\x1b[31mhit\x1b]0;title\x07‮\x00")
    conn.execute("INSERT INTO note_labels VALUES (1, 'bad key', 'v')")  # note 1 is bad
    add_note(conn, id=12, body="hit")
    assert search(gw, "hit", "--all") == 0
    assert [n["id"] for n in out_json(gw)["notes"]] == [12, 9]
    assert "\x1b" not in gw.terminal.stdout
    assert run(gw, "search", "hit", "--all") == 0
    for ch in gw.terminal.stdout + gw.terminal.stderr:
        assert ch == "\n" or 0x20 <= ord(ch) < 0x7F
    assert "title" not in gw.terminal.stdout  # no stored body text is printed at all


def test_value_over_one_mib_fails_the_statement(gw: FakeGateway) -> None:
    conn = seed(gw, [dict(id=1)])
    conn.execute("UPDATE notes SET body = ?", ("x" * 1_100_000,))
    assert search(gw, "x") == 12
    assert out_json(gw)["error"]["reason"] == "db_corrupt"


def test_foreign_long_combining_run_is_skipped_quickly(gw: FakeGateway) -> None:
    marks = "̖́" * 32768  # NFKC of this alone takes seconds
    conn = seed(gw, [dict(id=1, body="hit")])
    add_note(conn, id=2, body="hit e" + marks[:60000])
    add_note(conn, id=3, body="hit e" + "́" * 10)  # a legal run of 10 still matches
    started = time.monotonic()
    assert found(gw, "hit") == [3, 1]
    assert time.monotonic() - started < 2


def test_foreign_long_run_note_counts_in_bad_rows_not_total(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memoctl.cmd_search as cs

    conn = seed(gw, [dict(id=1, body="hit")])
    add_note(conn, id=2, body="hit e" + "\u0301" * 40)
    stores: list[Any] = []
    real = cs.open_read_store

    def spy(gateway: Any) -> Any:
        stores.append(real(gateway))
        return stores[-1]

    monkeypatch.setattr(cs, "open_read_store", spy)
    assert found(gw, "hit") == [1]
    assert stores[0].bad_rows == 1
    doc = out_json(gw)
    assert (doc["total"], doc["hidden"]) == (1, 0)


def test_matcher_unit_cases() -> None:
    assert fold("ǅ") == fold("ǆ") == "dž"
    with pytest.raises(TermError):
        parse_terms("   ")
    m = Matcher(parse_terms("é"))
    assert m.matches("t", "café") and not m.matches("t", "cafe")
    long_run = "x" + "̖́" * 20
    mm = Matcher(("x",))
    mm.matches("t", long_run)
    assert mm.skipped == 1


def test_storable_worst_case_combining_bodies_finish_well_under_the_deadline(
    gw: FakeGateway,
) -> None:
    body = ("e" + "\u0301" * 30) * 130  # about 8 KiB, runs of 30 (storable), NFKC-heavy
    assert len(body.encode()) <= 8192 and not has_long_combining_run(body)
    conn = seed(gw, [])
    conn.execute("BEGIN")
    for i in range(1, 5001):
        add_note(conn, id=i, body=body)
    conn.execute("COMMIT")
    started = time.monotonic()
    assert search(gw, "nomatch", "--limit", "1") == 0
    # The real deadline (30 s, exit 12) is the hard bound; exit 0 above proves it was not hit.
    # 25 s is well under the deadline yet loose enough for a loaded CI machine (measured ~5 s).
    assert time.monotonic() - started < 25


def test_prefilter_has_no_false_negatives_against_the_exact_check() -> None:
    marks = sorted(searchmatch._mark_chars())
    assert 800 < len(marks) < 1200
    rng = random.Random(7)
    samples = [
        "e" + "\u0301" * 31,
        "x" + "".join(rng.choice(marks) for _ in range(40)),  # mark-only chars, 1 or 2 marks each
        "é" + "\u0301" * 29,  # a char ending in marks starts the run
        "\u1e09" + "\u0301" * 28,
        "a" + "\u0301" * 30,  # exactly the limit: storable
        "ab" * 50 + "\u0301" * 7 + "c" + "\u0301" * 7,
    ]
    for _ in range(300):
        pool = [*marks[: rng.randint(1, 60)], "e", "\u00e9", "\u1e09", "z", "\u4e00"]
        samples.append("".join(rng.choice(pool) for _ in range(rng.randint(1, 80))))
    for text in samples:
        if has_long_combining_run(text):
            assert searchmatch._may_blow_up(text), text


def test_non_ascii_body_prefilter_cost_per_row_is_bounded() -> None:
    body = "漢字かな" * 2048  # 8 KB-ish of CJK, no marks
    m = Matcher(("zzz",))
    m.matches("t", body)  # builds the mark set once
    started = time.perf_counter()
    for _ in range(1000):
        assert not m.matches("t", body)
    per_row = (time.perf_counter() - started) / 1000
    assert per_row < 0.005, per_row  # loose; the fold itself is most of it


def test_adversarial_near_max_bodies_are_cheap(gw: FakeGateway) -> None:
    conn = seed(gw, [])
    body = "a" * 65535 + "b"
    conn.execute("BEGIN")
    for i in range(1, 301):
        add_note(conn, id=i, body=body)
    conn.execute("COMMIT")
    started = time.monotonic()
    assert found(gw, "a" * 62 + "bb", "--limit", "1") == []
    assert found(gw, "a" * 40 + " " + "a" * 63 + "c") == []
    assert time.monotonic() - started < 5


def test_concurrent_writer_is_one_snapshot(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(gw, [dict(id=i, body="hit") for i in range(1, 6)])
    real = notes_read.check_note_row
    seen = 0

    def writer(values: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == 2:
            other = sqlite3.connect(str(db_path(gw)), isolation_level=None)
            add_note(other, id=50, body="hit")
            other.close()
        return real(values)

    monkeypatch.setattr(notes_read, "check_note_row", writer)
    assert found(gw, "hit") == [5, 4, 3, 2, 1]
    monkeypatch.setattr(notes_read, "check_note_row", real)
    assert found(gw, "hit")[0] == 50


def test_deadline_firing_during_the_scan_leaves_no_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(gw, [dict(id=i, body="hit") for i in range(1, 30)])
    real = notes_read.check_note_row
    seen = 0

    def slow(values: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == 5:
            gw.advance_mono(31)
        return real(values)

    monkeypatch.setattr(notes_read, "check_note_row", slow)
    with pytest.raises(FakeExit) as e:
        run(gw, "search", "hit", "--json")
    assert e.value.code == 12
    assert '"notes"' not in gw.terminal.stdout
    assert "deadline passed" in gw.terminal.stderr


def test_read_never_creates_or_writes(gw: FakeGateway) -> None:
    seed(gw, [dict(id=1)]).close()
    before = db_path(gw).read_bytes()
    run(gw, "search", "hello")
    assert db_path(gw).read_bytes() == before


def _peak_search_memory(gw: FakeGateway, count: int) -> int:
    conn = seed(gw, [])
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO notes (id, repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (?, ?, 't', 'unknown', ?, ?, 40, 1, 4000000000)",
        ((i, REPO, ("hit " if i % 2 else "mis ") + "b" * 36, SHA) for i in range(1, count + 1)),
    )
    conn.execute("INSERT INTO note_labels SELECT id, 'k', 'v' FROM notes WHERE id % 10 = 0")
    conn.execute("COMMIT")
    conn.close()
    assert search(gw, "HIT", "--limit", "500") == 0  # warm-up, untraced: caches and imports
    gc.collect()
    was_enabled = gc.isenabled()
    try:
        gc.disable()  # a collection inside the traced region would move the peak
        tracemalloc.start()
        tracemalloc.reset_peak()
        assert search(gw, "HIT", "--limit", "500") == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        if was_enabled:
            gc.enable()
    doc = out_json(gw)
    assert (doc["total"], doc["shown"]) == (count // 2, 500)
    assert ids(gw)[:2] == [count - 1, count - 3]
    return peak


def test_memory_does_not_grow_with_the_store(tmp_path: Path) -> None:
    small = _peak_search_memory(FakeGateway(tmp_path / "small"), 10_000)
    large = _peak_search_memory(FakeGateway(tmp_path / "large"), 50_000)
    assert large < 1_000_000, large  # measured about 0.5 MB
    assert large < small * 1.5 + 100_000, (small, large)  # five times the notes, not the memory


# --- goldens ---------------------------------------------------------------------------------


def test_goldens(gw: FakeGateway) -> None:
    seed(
        gw,
        [
            dict(id=1, topic="build", kind="human", body="use `make verify`\n\nthen commit\n",
                 superseded_by=3, created=NOW - 86400 * 2, expires=NOW + 86400),
            dict(id=2, repo="example.com/x/y", topic="old", body="expired verify note",
                 expires=NOW - 1),
            dict(id=3, topic="build", kind="human", body="use `make verify` first\n",
                 created=NOW - 3600, expires=NOW + 86400 * 30),
            dict(id=4, topic="queue", body="the local queue must be running"),
        ],
        [(3, "ticket", "T-1"), (3, "area", "ci"), (4, "area", "ci")],
    )  # fmt: skip
    cases = {
        "search/default.json": ("search", "VERIFY", "--json"),
        "search/all.json": ("search", "verify", "--all", "--json"),
        "search/limited.json": ("search", "verify", "--all", "--limit", "1", "--json"),
        "search/none.json": ("search", "nomatch", "--json"),
        "search/default.txt": ("search", "verify"),
        "search/all.txt": ("search", "verify", "--all"),
        "search/none.txt": ("search", "nomatch"),
    }  # fmt: skip
    for name, argv in cases.items():
        assert run(gw, *argv) == 0, name
        out = gw.terminal.stdout.encode()
        if name.endswith(".json"):
            check_golden(name, out, schema="cli/search")
        else:
            check_golden(name, out)
            check_golden(name.replace(".txt", "-err.txt"), gw.terminal.stderr.encode())
    run(gw, "search", "a b c d e f g h i", "--json")
    check_golden("search/too-many-terms.json", gw.terminal.stdout.encode(), schema="cli/error")
