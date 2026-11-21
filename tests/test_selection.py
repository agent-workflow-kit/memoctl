"""Context selection (interface.md, Context selection): candidates, order, the kind-share walk.

The property tests compare ``select_context`` with ``reference_select`` below, written from the
interface's pseudocode with its own rendering and no import of the code under test.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import unicodedata
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from memoctl.selection import (
    Candidate,
    SelectionConfig,
    b8,
    budgets,
    head_line,
    note_text,
    omit_line,
    select_context,
    share_fraction,
    u16,
    walk,
)
from memoctl.statedir import StateDir, open_state_dir
from memoctl.store import Mode, NoteRow, StoreError, open_store
from tests.test_statedir import HomeGateway

REPO = "example.com/a/b"
NOW = 1_700_000_000
SHA = "a" * 64
KINDS = ("implement", "audit", "draft-ticket", "improve")


def sanitize(text: str) -> str:
    """A stand-in for the #24 sanitizer, written independently here."""
    out = unicodedata.normalize("NFKC", text)
    out = "".join(c for c in out if c == "\n" or c == "\t" or unicodedata.category(c)[0] != "C")
    return out.replace("\t", "    ").replace("<", "&lt;")


def cfg(**over: Any) -> SelectionConfig:
    base: dict[str, Any] = dict(
        author_kinds=KINDS, max_chars=8000, max_bytes=15000, max_notes=20, max_per_topic=2,
        max_kind_share=Fraction(1, 2), note_max_chars=2000,
    )  # fmt: skip
    base.update(over)
    return SelectionConfig(**base)


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    h = tmp_path / "h"
    h.mkdir()
    return open_state_dir(HomeGateway(h), create=True)


class Db:
    """A store file plus a raw writer connection to fill it with arbitrary rows."""

    def __init__(self, state: StateDir) -> None:
        self.state = state
        self.n = 0
        s = open_store(state, Mode.INIT)
        assert s is not None
        s.close()
        self.conn = sqlite3.connect(str(state.db_path), isolation_level=None)

    def add(self, **over: Any) -> int:
        self.n += 1
        body = over.pop("body", "hello")
        labels = over.pop("labels", ())
        row: dict[str, Any] = dict(
            repo=REPO, topic=f"t{self.n}", kind="implement", body=body, sha=SHA,
            size=len(body.encode()), created=NOW - 100, expires=NOW + 1000, superseded_by=None,
        )  # fmt: skip
        row.update(over)
        cur = self.conn.execute(
            "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires,"
            " superseded_by) VALUES (?,?,?,?,?,?,?,?,?)",
            list(row.values()),
        )
        nid = cur.lastrowid
        assert nid is not None
        for k, v in labels:
            self.conn.execute("INSERT INTO note_labels VALUES (?,?,?)", (nid, k, v))
        return nid

    def select(self, labels: Any = (), config: SelectionConfig | None = None, **kw: Any) -> Any:
        s = open_store(self.state, Mode.READ)
        assert s is not None
        with s:
            return select_context(
                s, repo=kw.get("repo", REPO), session_labels=labels, config=config or cfg(),
                now=kw.get("now", NOW), sanitize=kw.get("sanitize", sanitize),
            )  # fmt: skip


@pytest.fixture
def db(state: StateDir) -> Db:
    return Db(state)


def ids(sel: Any) -> list[int]:
    return [n.id for n in sel.notes]


# --- the reference implementation (independent) ----------------------------------------------


def _ref_u16(s: str) -> int:
    return sum(2 if ord(c) >= 0x10000 else 1 for c in s)


def _ref_text(n: dict[str, Any], cap: int) -> str:
    body = sanitize(n["body"])
    pieces = body.split("\n")
    if pieces[-1] == "":
        pieces = pieces[:-1]
    lines = ["|\n" if p == "" else "| " + p + "\n" for p in pieces]
    total = sum(_ref_u16(x) for x in lines)
    cut = False
    if total <= cap:
        kept = lines
    else:
        cut = True
        kept = []
        run = 0
        for x in lines:
            if run + _ref_u16(x) > cap:
                break
            run += _ref_u16(x)
            kept.append(x)
        if not kept:
            first = lines[0][:-1]
            piece = ""
            for ch in first:
                if _ref_u16(piece + ch) + 1 <= cap:
                    piece += ch
                else:
                    break
            kept = [piece + "\n"]
    import datetime

    day = datetime.datetime.fromtimestamp(n["created"], datetime.UTC).date().isoformat()
    labs = ", ".join(k + "=" + v for k, v in sorted(n["labels"]))
    header = f"--- note {n['id']} | topic: {n['topic']} | kind: {n['kind']} | {day}"
    if labs:
        header += " | " + labs
    header += " ---\n"
    return header + "".join(kept) + (f"[memoctl: note {n['id']} cut]\n" if cut else "")


def reference_select(
    notes: list[dict[str, Any]], repo: str, labels: set[tuple[str, str]], c: SelectionConfig
) -> tuple[list[int], int]:
    cands = [
        n for n in notes
        if n["repo"] == repo and n["superseded_by"] is None and n["expires"] > NOW
        and n["kind"] in c.author_kinds and set(n["labels"]) <= labels
        and sanitize(n["body"]).strip() != ""
    ]  # fmt: skip
    cands.sort(key=lambda n: -n["id"])
    e = len(cands)
    head = f"memoctl notes for {repo} ({e} of {e} shown)\n"
    omit = f"[memoctl: {e} more notes not shown; use memoctl list --repo {repo}]\n"
    bc = c.max_chars - _ref_u16(head) - _ref_u16(omit)
    bb = c.max_bytes - len((head + omit).encode())
    share_c = (c.max_kind_share.numerator * c.max_chars) // c.max_kind_share.denominator
    share_n = max(1, (c.max_kind_share.numerator * c.max_notes) // c.max_kind_share.denominator)
    sel: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    text = {n["id"]: _ref_text(n, c.note_max_chars) for n in cands}

    def cu(n: dict[str, Any]) -> int:
        return _ref_u16(text[n["id"]])

    def bu(n: dict[str, Any]) -> int:
        return len(text[n["id"]].encode())

    def fits(n: dict[str, Any]) -> bool:
        return sum(cu(x) for x in sel) + cu(n) <= bc and sum(bu(x) for x in sel) + bu(n) <= bb

    def cnt(key: str, v: str) -> int:
        return sum(1 for x in sel if x[key] == v)

    for n in cands:
        if len(sel) == c.max_notes:
            break
        if cnt("topic", n["topic"]) == c.max_per_topic:
            continue
        if not fits(n):
            continue
        kc = sum(cu(x) for x in sel if x["kind"] == n["kind"])
        if kc + cu(n) > share_c or cnt("kind", n["kind"]) + 1 > share_n:
            deferred.append(n)
            continue
        sel.append(n)
    for n in deferred:
        if len(sel) == c.max_notes:
            break
        if cnt("topic", n["topic"]) == c.max_per_topic:
            continue
        if fits(n):
            sel.append(n)
    return sorted((n["id"] for n in sel), reverse=True), e


# --- candidates -------------------------------------------------------------------------------


def test_live_repo_kind_filters(db: Db) -> None:
    keep = db.add()
    db.add(repo="example.com/other")  # another repo
    db.add(superseded_by=keep)  # superseded
    db.add(expires=NOW)  # expired exactly at now
    db.add(kind="unknown")  # not allowed
    last = db.add(expires=NOW + 1)  # live by one second
    sel = db.select()
    assert ids(sel) == [last, keep]
    assert sel.eligible == 2


def test_labels_are_a_subset_of_the_session_labels_not_and(db: Db) -> None:
    none = db.add()
    one = db.add(labels=[("team", "a")])
    two = db.add(labels=[("team", "a"), ("env", "x")])
    other = db.add(labels=[("team", "b")])
    assert ids(db.select([("team", "a")])) == [one, none]
    assert ids(db.select([("team", "a"), ("env", "x")])) == [two, one, none]
    assert ids(db.select([("env", "x")])) == [none]
    assert ids(db.select([("team", "b"), ("team", "b")])) == [other, none]
    assert ids(db.select([])) == [none]


def test_label_key_and_value_must_both_match(db: Db) -> None:
    a = db.add(labels=[("team", "a")])
    assert ids(db.select([("team", "b"), ("a", "team")])) == []
    assert ids(db.select([("team", "a")])) == [a]


def test_whitespace_only_sanitized_body_is_not_a_candidate(db: Db) -> None:
    db.add(body="  \n\t \n")
    db.add(body=" 　")  # NFKC makes these spaces
    ok = db.add(body="x")
    sel = db.select()
    assert ids(sel) == [ok] and sel.eligible == 1


def test_measured_on_the_sanitizer_output_not_nfkc_of_the_stored_text(db: Db) -> None:
    db.add(body="abc")
    db.add(body="def")
    seen: list[str] = []

    def short(t: str) -> str:
        seen.append(t)
        return "z"  # the sanitizer's own output is what counts

    sel = db.select(sanitize=short)
    assert all(n.text.endswith("| z\n") for n in sel.notes)
    assert seen


def test_empty_store_no_candidates_all_expired(db: Db) -> None:
    assert db.select().eligible == 0
    db.add(expires=NOW - 1)
    db.add(expires=0)
    sel = db.select()
    assert (sel.eligible, sel.shown, sel.omitted) == (0, 0, 0)


def test_order_is_newest_id_whatever_the_timestamps(db: Db) -> None:
    a = db.add(created=5000 + NOW - 10_000, expires=NOW + 9)
    b = db.add(created=0)
    c = db.add(created=NOW)
    assert ids(db.select()) == [c, b, a]


def test_kind_in_list_larger_and_empty_allow_lists(db: Db) -> None:
    n = db.add(kind="k63")
    kinds = tuple(f"k{i}" for i in range(64))
    assert ids(db.select(config=cfg(author_kinds=kinds))) == [n]
    assert db.select(config=cfg(author_kinds=())).eligible == 0
    with pytest.raises(ValueError):
        db.select(config=cfg(author_kinds=tuple(f"k{i}" for i in range(65))))


def test_injection_shaped_repo_and_labels_are_only_values(db: Db) -> None:
    db.add()
    evil = "x' OR 1=1 --"
    assert db.select(repo=evil).eligible == 0
    assert db.select([(evil, evil)]).eligible == 1  # no label on the note; nothing matched


def test_bad_now_is_refused(db: Db) -> None:
    for bad in (-1, 2**63, 1.5, True):
        with pytest.raises(ValueError):
            db.select(now=bad)


# --- row checks ---------------------------------------------------------------------------------


def test_hostile_rows_in_the_middle_are_skipped_and_counted(db: Db) -> None:
    a = db.add()
    db.add(topic="Bad Topic")  # grammar
    db.add(size=99)  # size differs
    db.conn.execute(  # an invalid label row
        "INSERT INTO note_labels VALUES (?, 'UPPER', 'v')", (db.add(),)
    )
    z = db.add()
    sel = db.select()
    assert ids(sel) == [z, a]
    assert sel.eligible == 2 and sel.bad_rows == 3


def test_value_over_1mib_fails_the_statement(db: Db) -> None:
    db.add()
    db.conn.execute(
        "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
        " VALUES (?, 't', 'implement', ?, ?, 1, 1, ?)",
        (REPO, "x" * (1_048_576 + 1), SHA, NOW + 5),
    )
    with pytest.raises(StoreError) as e:
        db.select()
    assert e.value.reason == "db_corrupt"


def test_undecodable_text_row_is_skipped(db: Db) -> None:
    a = db.add()
    nid = db.add(body="zz")
    db.conn.execute("UPDATE notes SET body = CAST(X'ff61' AS TEXT) WHERE id = ?", (nid,))
    sel = db.select()
    assert ids(sel) == [a] and sel.bad_rows == 1


# --- rendering ----------------------------------------------------------------------------------


def row(**over: Any) -> NoteRow:
    base: dict[str, Any] = dict(
        id=7, repo=REPO, topic="topic", kind="audit", body="b", body_sha256=SHA, size=1,
        created=1_700_000_000, expires=NOW + 1, superseded_by=None, labels=(),
    )  # fmt: skip
    base.update(over)
    return NoteRow(**base)


def test_header_and_lines() -> None:
    text, cut = note_text(row(labels=(("z", "1"), ("a", "2"))), "one\n\nthree\n", 2000)
    assert not cut
    assert text == (
        "--- note 7 | topic: topic | kind: audit | 2023-11-14 | a=2, z=1 ---\n| one\n|\n| three\n"
    )
    text, _ = note_text(row(), "x", 2000)
    assert text.startswith("--- note 7 | topic: topic | kind: audit | 2023-11-14 ---\n")


def test_cut_keeps_whole_lines_and_adds_the_cut_line() -> None:
    body = "aaaa\nbbbb\ncccc\n"  # each line is 7 u16
    text, cut = note_text(row(), body, 14)
    assert cut and text.endswith("| aaaa\n| bbbb\n[memoctl: note 7 cut]\n")
    text, cut = note_text(row(), body, 21)
    assert not cut and text.endswith("| cccc\n")


def test_cut_single_long_line_by_code_points() -> None:
    text, cut = note_text(row(), "a" + "\U0001f600" * 10, 8)
    # "| a" is 3, each emoji 2: 3+2+2 = 7, +1 LF = 8 <= 8
    assert cut
    assert text.endswith("| a\U0001f600\U0001f600\n[memoctl: note 7 cut]\n")
    text, _ = note_text(row(), "\U0001f600" * 10, 6)  # "| " 2 + 2 + 2 = 6, +1 > 6: one emoji
    assert text.endswith("| \U0001f600\n[memoctl: note 7 cut]\n")
    assert "\ud83d" not in text


def test_counts_are_u16_and_bytes_not_characters() -> None:
    assert (u16("a\U0001f600é"), b8("a\U0001f600é")) == (4, 7)
    assert u16("") == b8("") == 0


def test_reserved_lines_and_budgets() -> None:
    assert head_line("r/x", 3, 9) == "memoctl notes for r/x (3 of 9 shown)\n"
    assert omit_line("r/x", 6) == "[memoctl: 6 more notes not shown; use memoctl list --repo r/x]\n"
    bc, bb = budgets("r/x", 9, cfg(max_chars=1000, max_bytes=2000))
    h, o = head_line("r/x", 9, 9), omit_line("r/x", 9)
    assert bc == 1000 - len(h) - len(o) and bb == 2000 - len(h) - len(o)
    repo = "a.b/" + "x" * 251
    bc, _ = budgets(repo, 100000, cfg(max_chars=1000))
    assert bc >= 381


def test_share_fraction_is_exact_decimal() -> None:
    assert share_fraction(0.3) == Fraction(3, 10)
    assert share_fraction(1) == 1
    assert share_fraction(0.7) * 1000 == 700
    with pytest.raises(ValueError):
        share_fraction(True)


# --- the walk -----------------------------------------------------------------------------------


def cands(spec: list[tuple[int, str, str, int]]) -> list[Candidate]:
    return [Candidate(i, t, k, c, c) for i, t, k, c in spec]


def test_walk_topic_limit_and_pass_two_fill() -> None:
    cs = cands([(9, "a", "x", 10), (8, "a", "x", 10), (7, "a", "x", 10), (6, "b", "x", 10)])
    c = cfg(max_kind_share=Fraction(1), max_per_topic=2)
    got = walk(cs, 1000, 1000, c)
    assert [n.id for n in got] == [9, 8, 6]


def test_walk_kind_share_defers_then_pass_two_refills() -> None:
    # share_n = 1: the second x note is deferred, the y note (older) is taken in pass 1
    cs = cands([(9, "a", "x", 10), (8, "b", "x", 10), (7, "c", "y", 10)])
    c = cfg(max_notes=2, max_kind_share=Fraction(1, 2))
    assert [n.id for n in walk(cs, 1000, 1000, c)] == [9, 7]
    c = cfg(max_notes=3, max_kind_share=Fraction(1, 3))  # share_n = 1: 8 deferred, taken in pass 2
    assert [n.id for n in walk(cs, 1000, 1000, c)] == [9, 8, 7]


def test_walk_share_n_is_at_least_one_and_char_share_applies() -> None:
    cs = cands([(9, "a", "x", 60), (8, "b", "x", 30), (7, "c", "y", 5)])
    c = cfg(max_chars=100, max_notes=1, max_kind_share=Fraction(1, 10))  # share_c 10, share_n 1
    assert [n.id for n in walk(cs, 1000, 1000, c)] == [
        7
    ]  # 9, 8 deferred (over share_c); 7 fills the slot
    c = cfg(max_chars=100, max_notes=3, max_kind_share=Fraction(1, 2))  # share_c = 50
    assert [n.id for n in walk(cs, 1000, 1000, c)] == [9, 8, 7]
    # 9 deferred (60 > 50), 8 taken, 7 taken, pass 2 adds 9
    c = cfg(max_chars=100, max_notes=2, max_kind_share=Fraction(1, 2))
    assert [n.id for n in walk(cs, 1000, 1000, c)] == [8, 7]


def test_walk_budget_exact_equal_and_smaller_than_one_note() -> None:
    cs = cands([(2, "a", "x", 10), (1, "b", "x", 10)])
    c = cfg(max_kind_share=Fraction(1))
    assert [n.id for n in walk(cs, 20, 20, c)] == [2, 1]
    assert [n.id for n in walk(cs, 19, 20, c)] == [2]
    assert [n.id for n in walk(cs, 20, 19, c)] == [2]
    assert walk(cs, 9, 100, c) == []
    assert walk(cs, 0, 0, c) == []
    assert walk(cs, -5, -5, c) == []


def test_walk_note_bigger_than_budget_is_skipped_later_notes_still_fit() -> None:
    cs = cands([(3, "a", "x", 500), (2, "b", "x", 5), (1, "c", "x", 5)])
    assert [n.id for n in walk(cs, 100, 100, cfg(max_kind_share=Fraction(1)))] == [2, 1]


def test_walk_shares_that_are_not_a_partition() -> None:
    cs = cands([(i, f"t{i}", f"k{i % 3}", 10) for i in range(30, 0, -1)])
    for share in (Fraction(1, 10), Fraction(1), Fraction(1, 3), Fraction(99, 100), Fraction(0)):
        got = walk(cs, 10**6, 10**6, cfg(max_notes=5, max_kind_share=share))
        assert len(got) == 5 and [n.id for n in got] == sorted((n.id for n in got), reverse=True)


def test_walk_huge_numbers_do_not_overflow() -> None:
    big = 10**30
    cs = [Candidate(big, "a", "x", big, big), Candidate(big - 1, "b", "y", 1, 1)]
    got = walk(cs, big * 2, big * 2, cfg(max_chars=big, max_kind_share=Fraction(1, 2)))
    assert [n.id for n in got] == [big, big - 1]


def test_walk_does_not_depend_on_dict_or_set_order() -> None:
    cs = cands([(i, f"t{i % 7}", f"k{i % 5}", 7 + i % 11) for i in range(200, 0, -1)])
    c = cfg(max_notes=30, max_chars=300)
    first = walk(cs, 5000, 5000, c)
    for _ in range(3):
        assert walk(list(cs), 5000, 5000, c) == first


# --- the whole selection against the reference ----------------------------------------------------


def random_fixture(rng: random.Random) -> tuple[list[dict[str, Any]], SelectionConfig, set[Any]]:
    n = rng.randint(0, 60)
    topics = [f"t{i}" for i in range(rng.randint(1, 8))]
    kinds = list(KINDS) + ["unknown"]
    labelpool = [("team", "a"), ("team", "b"), ("env", "x")]
    notes = []
    for i in range(1, n + 1):
        body = rng.choice(
            [
                "short",
                "line one\nline two\n",
                "é" * rng.randint(1, 300),
                "\U0001f600" * rng.randint(1, 200),
                "x" * rng.randint(1, 900) + "\n" + "y" * rng.randint(1, 900),
                "   \n",
                "<b>&lt;</b>\ttab",
                "\n".join(f"line{j}" for j in range(rng.randint(1, 80))),
            ]
        )
        notes.append(
            dict(
                id=i, repo=rng.choice([REPO, REPO, "example.com/zz"]), topic=rng.choice(topics),
                kind=rng.choice(kinds), body=body,
                created=rng.choice([0, NOW - 5, 1_000_000_000, 253_402_300_799]),
                expires=rng.choice([NOW - 1, NOW, NOW + 1, NOW + 10**6]),
                superseded_by=rng.choice([None, None, None, 1]),
                labels=rng.sample(labelpool, rng.randint(0, 3)),
            )
        )  # fmt: skip
    c = cfg(
        max_chars=rng.choice([1000, 1500, 3000, 8000]),
        max_bytes=rng.choice([1000, 2500, 15000]),
        max_notes=rng.choice([1, 3, 10, 20]),
        max_per_topic=rng.choice([1, 2, 5]),
        max_kind_share=Fraction(rng.choice([1, 2, 3, 5, 10]), 10),
        note_max_chars=rng.choice([100, 300, 2000]),
    )
    labels = set(rng.sample(labelpool, rng.randint(0, 3)))
    return notes, c, labels


@pytest.mark.parametrize("seed", range(40))
def test_selection_equals_the_reference_on_random_fixtures(state: StateDir, seed: int) -> None:
    rng = random.Random(seed)
    notes, c, labels = random_fixture(rng)
    d = Db(state)
    for n in notes:
        d.add(
            repo=n["repo"], topic=n["topic"], kind=n["kind"], body=n["body"], created=n["created"],
            expires=n["expires"], superseded_by=n["superseded_by"], labels=n["labels"],
        )  # fmt: skip
    got = d.select(labels, c)
    want, eligible = reference_select(notes, REPO, labels, c)
    assert ids(got) == want
    assert got.eligible == eligible
    assert got.omitted == eligible - len(want)
    # the printed block fits both budgets, reserved lines included
    if got.shown:
        block = got.head + "".join(n.text for n in got.notes) + got.omit
        assert u16(block) <= c.max_chars and b8(block) <= c.max_bytes
        for n in got.notes:
            assert (n.u16, n.b8) == (u16(n.text), b8(n.text))


def test_reserved_lines_never_exceeded_at_the_minimum_budget(db: Db) -> None:
    repo = "a.b/" + "x" * 251
    for i in range(300):
        db.add(repo=repo, topic=f"t{i}", body="word " * 20)
    c = cfg(max_chars=1000, max_bytes=1000, max_notes=100, max_kind_share=Fraction(1))
    sel = db.select(repo=repo, config=c)
    block = sel.head + "".join(n.text for n in sel.notes) + sel.omit
    assert sel.shown > 0 and sel.omitted > 0
    assert u16(block) <= 1000 and b8(block) <= 1000


def test_header_alone_over_budget_note_is_never_shown(db: Db) -> None:
    labels = [(f"k{i}", "v" * 256) for i in range(8)]
    db.add(labels=labels)
    sel = db.select(labels, config=cfg(max_chars=1000, max_bytes=1000))
    assert (sel.eligible, sel.shown) == (1, 0)


def test_unicode_bytes_vs_characters_budget(db: Db) -> None:
    # 3-byte characters: fits the character budget but not the byte budget
    a = db.add(body="€" * 300, topic="a")
    b = db.add(body="€" * 300, topic="b")
    c = cfg(max_chars=3000, max_bytes=1600, max_kind_share=Fraction(1), note_max_chars=2000)
    sel = db.select(config=c)
    assert ids(sel) == [b] and a < b and sel.omitted == 1


def test_identical_timestamps_tie_on_id(db: Db) -> None:
    made = [db.add(created=NOW - 1, expires=NOW + 5, topic=f"t{i}") for i in range(10)]
    assert ids(db.select()) == sorted(made, reverse=True)


def test_deterministic_across_runs(db: Db) -> None:
    for i in range(50):
        db.add(topic=f"t{i % 9}", kind=KINDS[i % 4], body="b" * (i + 1))
    runs = [ids(db.select(config=cfg(max_chars=1500))) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_large_store_streams_only_what_it_must(db: Db) -> None:
    db.conn.execute("BEGIN")
    for i in range(20000):
        db.conn.execute(
            "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
            " VALUES (?, ?, ?, 'body', ?, 4, 1, ?)",
            (REPO if i % 2 else "example.com/other", f"t{i % 500}", KINDS[i % 4], SHA, NOW + 10),
        )
    db.conn.execute("COMMIT")
    sel = db.select()
    assert sel.eligible == 10000 and sel.shown == 20
    assert max(ids(sel)) == 20000 and min(ids(sel)) > 19900


def test_one_snapshot_a_concurrent_writer_is_not_seen_midway(db: Db) -> None:
    for i in range(5):
        db.add(topic=f"t{i}")
    s = open_store(db.state, Mode.READ)
    assert s is not None
    seen: dict[str, Any] = {}
    with s, s.read():
        first = select_context(
            s, repo=REPO, session_labels=[], config=cfg(), now=NOW, sanitize=sanitize
        )

        def writer() -> None:
            w = sqlite3.connect(str(db.state.db_path), isolation_level=None)
            w.execute(
                "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires)"
                " VALUES (?, 'new', 'implement', 'n', ?, 1, 1, ?)",
                (REPO, SHA, NOW + 10),
            )
            w.close()

        t = threading.Thread(target=writer)
        t.start()
        t.join()
        second = select_context(
            s, repo=REPO, session_labels=[], config=cfg(), now=NOW, sanitize=sanitize
        )
        seen["a"], seen["b"] = first, second
    assert seen["a"] == seen["b"]  # inside the caller's transaction the second read is the first
    assert db.select().eligible == 6  # a new selection sees the writer


def test_selection_joins_an_open_transaction_and_opens_its_own_otherwise(db: Db) -> None:
    db.add()
    s = open_store(db.state, Mode.READ)
    assert s is not None
    with s:
        assert not s.in_transaction
        select_context(s, repo=REPO, session_labels=[], config=cfg(), now=NOW, sanitize=sanitize)
        assert not s.in_transaction  # its own transaction is closed again
        with s.read():
            select_context(
                s, repo=REPO, session_labels=[], config=cfg(), now=NOW, sanitize=sanitize
            )
            assert s.in_transaction


def test_selection_writes_nothing(db: Db) -> None:
    db.add()
    before = db.state.db_path.read_bytes()
    db.select()
    assert db.state.db_path.read_bytes() == before  # a read never changes the main file
    s = open_store(db.state, Mode.READ)
    assert s is not None
    with s:
        with pytest.raises(StoreError):
            s.execute("INSERT INTO rate (repo, at) VALUES ('x', 1)")
