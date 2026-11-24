"""Flood and performance fixtures for ``context`` (issue #28; docs/plan.md, Stage 5).

Two kinds of check, neither a wall-clock promise:

* Fairness fixtures with the outcomes the interface documents (kind share, topic cap, the two
  documented zero-survivor floods and their control).
* Cost fixtures: worst-case stores are built raw (``tests/flood_fixtures.py``), ``context`` runs
  end to end, and the assertions are on behavior: a well-formed block inside both budgets, the
  operation counts (how many bodies were sanitized and how many characters they held) and a bound
  on peak memory. Time appears only as a generous multiple of the deadline (the deadline itself is
  the budget: a run that exceeds it must be exit 12 with nothing on stdout) and, in the two
  child-process tests, as the real default 2 s deadline with a skip when this machine is too slow
  to say anything: the scan in process plus the measured bare start-up of a child (``--version``)
  must be at most half the deadline.

The heavy variants carry ``@pytest.mark.perf`` and run only on request: ``make perf``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import tracemalloc
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from memoctl import cmd_context
from memoctl.sanitize import sanitize as real_sanitize
from memoctl.selection import b8, u16
from tests import flood_fixtures as ff
from tests import private_home
from tests.fake_gateway import FakeGateway
from tests.flood_fixtures import REPO, Note
from tests.test_cmd_context import Store, assert_block, run
from tests.test_cmd_context_process import TIMEOUT_LINE, child, env_for, set_config
from tests.test_validation import write_config

DEFAULT_DEADLINE = 2.0
ROOMY_DEADLINE = 10.0  # the config maximum: the in-process runs are not about the 2 s


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "fake")


@pytest.fixture
def store(gw: FakeGateway) -> Store:
    return Store(gw)


class Counter:
    """Wraps the sanitizer ``context`` calls: how many bodies and how many stored characters."""

    def __init__(self) -> None:
        self.calls = 0
        self.chars = 0

    def __call__(self, text: str) -> str:
        self.calls += 1
        self.chars += len(text)
        return real_sanitize(text)


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> Counter:
    c = Counter()
    monkeypatch.setattr(cmd_context, "sanitize", c)
    return c


def context_json(gw: FakeGateway, *extra: str) -> tuple[int, dict[str, Any]]:
    code = run(gw, "--repo", REPO, "--json", *extra)
    return code, json.loads(gw.terminal.stdout) if gw.terminal.stdout else {}


def assert_within_budgets(
    doc: dict[str, Any], max_chars: int = 8000, max_bytes: int = 15000
) -> None:
    """The block is well formed and inside both budgets (the fence lines are not counted)."""
    text = doc["text"]
    assert_block(text)
    assert u16(text) <= max_chars and b8(text) <= max_bytes
    assert doc["shown"] == len(doc["note_ids"]) <= 100
    assert doc["omitted"] == doc["eligible"] - doc["shown"]


def insert(store: Store, notes: list[Note]) -> list[int]:
    return ff.insert(store.conn, notes)


# --- the flood fixtures with their documented outcomes ------------------------------------------


def test_the_audit_flood_fixture_shows_at_least_ten_real_notes(
    gw: FakeGateway, store: Store
) -> None:
    """20 real ``audit`` notes in 10 topics (older ids), 5000 bogus ``implement`` notes in 2500
    topics (newer ids), the defaults: the kind share keeps at least 10 real notes visible."""
    real, flood = ff.flood_notes(
        real_kind="audit", real=20, real_topics=10, flood=[("implement", 5000, 2500)]
    )
    real_ids = set(insert(store, real))
    insert(store, flood)
    code, doc = context_json(gw)
    assert code == 0 and doc["eligible"] == 5020
    shown_real = real_ids & set(doc["note_ids"])
    assert len(shown_real) >= 10
    assert doc["shown"] == 20 and len(shown_real) == 10  # the share: 10 per kind
    assert_within_budgets(doc)


def test_a_flood_under_the_real_kind_shows_no_real_notes(gw: FakeGateway, store: Store) -> None:
    """The documented residual risk: 20 newer notes of the real notes' kind take every slot."""
    real, flood = ff.flood_notes(
        real_kind="implement", real=20, real_topics=10, flood=[("implement", 20, 10)]
    )
    real_ids = set(insert(store, real))
    insert(store, flood)
    code, doc = context_json(gw)
    assert code == 0 and doc["shown"] == 20
    assert not real_ids & set(doc["note_ids"])


def test_a_flood_split_over_two_kinds_shows_no_real_notes(gw: FakeGateway, store: Store) -> None:
    """20 real ``audit`` notes, 20 newer notes split over ``implement`` and ``draft-ticket``."""
    real, flood = ff.flood_notes(
        real_kind="audit", real=20, real_topics=10,
        flood=[("implement", 10, 10), ("draft-ticket", 10, 10)],
    )  # fmt: skip
    real_ids = set(insert(store, real))
    insert(store, flood)
    code, doc = context_json(gw)
    assert code == 0 and doc["shown"] == 20
    assert not real_ids & set(doc["note_ids"])


def test_the_same_flood_in_one_kind_leaves_ten_real_notes(gw: FakeGateway, store: Store) -> None:
    """The control for the two fixtures above, so the passing fixture does not suggest more."""
    real, flood = ff.flood_notes(
        real_kind="audit", real=20, real_topics=10, flood=[("implement", 20, 10)]
    )
    real_ids = set(insert(store, real))
    insert(store, flood)
    code, doc = context_json(gw)
    assert code == 0 and doc["shown"] == 20
    assert len(real_ids & set(doc["note_ids"])) == 10


def test_a_flood_in_one_topic_is_cut_to_the_per_topic_cap_and_the_real_notes_show(
    gw: FakeGateway, store: Store
) -> None:
    """10 older real ``audit`` notes in 10 topics, 30 newer ``implement`` notes all in ONE
    topic. The cap (``max_per_topic``, 2) decides: only 2 flood notes show, all 10 real ones do.
    Without the cap the kind share (10) and the second pass would show 10 flood notes."""
    real = [Note(topic=f"real-{i}", kind="audit", body=f"real note {i}") for i in range(10)]
    flood = [Note(topic="flood", kind="implement", body=f"bogus {i}") for i in range(30)]
    real_ids = set(insert(store, real))
    flood_ids = set(insert(store, flood))
    code, doc = context_json(gw)
    assert code == 0 and doc["eligible"] == 40
    shown = set(doc["note_ids"])
    assert len(shown & flood_ids) == 2  # max_per_topic
    assert real_ids <= shown
    assert_within_budgets(doc)


def test_many_kinds_in_the_full_64_kind_allow_list_are_bounded_by_the_note_cap(
    gw: FakeGateway, store: Store
) -> None:
    kinds = [f"kind{i:02d}" for i in range(64)]
    write_config(gw, "context:\n  author_kinds: [" + ", ".join(kinds) + "]\n")
    insert(
        store,
        [Note(topic=f"t{i}", kind=kinds[i % 64], body=f"note {i}") for i in range(1280)],
    )
    code, doc = context_json(gw)
    assert code == 0 and doc["eligible"] == 1280 and doc["shown"] == 20
    assert_within_budgets(doc)
    # One slot per kind first (the share is at least one note), the newest 20 kinds.
    shown_kinds = {store.conn.execute("SELECT kind FROM notes WHERE id=?", (i,)).fetchone()[0]
                   for i in doc["note_ids"]}  # fmt: skip
    assert len(shown_kinds) == 20


def test_many_repos_are_never_read_by_a_scan_of_one(
    gw: FakeGateway, store: Store, counter: Counter
) -> None:
    """A store at the default ``limits.total`` (5000 notes) in 25 repos of 200: the scan
    sanitizes this repo's 200 live notes and the up to 20 it prints, nothing else."""
    notes = [
        Note(repo=f"example.com/r/repo{r}", topic=f"t{i % 40}", body=f"note {r} {i}")
        for r in range(25)
        for i in range(200)
    ]
    insert(store, notes)
    code = run(gw, "--repo", "example.com/r/repo7", "--json")
    doc = json.loads(gw.terminal.stdout)
    assert code == 0 and doc["eligible"] == 200
    assert counter.calls == 200 + doc["shown"]  # one pass plus the re-read of the chosen notes


def test_every_note_tying_on_created_and_expires_is_ordered_by_id(
    gw: FakeGateway, store: Store
) -> None:
    ids = insert(store, [Note(topic=f"t{i}", body=f"tie {i}") for i in range(500)])
    code, doc = context_json(gw)
    assert code == 0 and doc["note_ids"] == sorted(ids, reverse=True)[:20]


# --- the cap fixtures ---------------------------------------------------------------------------


def test_labels_topic_kind_and_repo_near_their_caps(gw: FakeGateway, store: Store) -> None:
    """Headers of maximal length (8 labels of 64 + 256 characters, a 64-character topic and
    kind, a 255-character repo) are counted and kept inside both budgets."""
    repo = "example.com/" + "a" * 243  # 255 characters
    labels = tuple((f"k{j}" + "x" * 61, "v" * 256) for j in range(8))
    kind = "k" + "x" * 63
    write_config(gw, f"context:\n  author_kinds: [{kind}]\n")
    insert(
        store,
        [
            Note(repo=repo, topic="t" + "y" * 63, kind=kind, body="body\n" * 400, labels=labels)
            for _ in range(30)
        ],
    )
    args = ["--repo", repo, "--json"]
    for k, v in labels:
        args += ["--label", f"{k}={v}"]
    code = run(gw, *args)
    doc = json.loads(gw.terminal.stdout)
    assert code == 0 and doc["eligible"] == 30 and doc["shown"] >= 1
    assert_within_budgets(doc)


@pytest.mark.parametrize("max_body", [8192, 65536])
def test_maximum_size_bodies_at_the_live_notes_cap_stay_bounded(
    gw: FakeGateway, store: Store, counter: Counter, max_body: int
) -> None:
    """200 live notes (the default ``live_per_repo``) of the largest body the limit allows."""
    insert(store, [Note(topic=f"t{i}", body=ff.body_ascii(max_body)) for i in range(200)])
    tracemalloc.start()
    try:
        code, doc = context_json(gw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert code == 0 and doc["eligible"] == 200
    assert_within_budgets(doc)
    assert counter.calls == 200 + doc["shown"]
    assert counter.chars == counter.calls * max_body  # every body is the full ASCII body
    # No body is kept for a note that is not chosen: the peak is a few bodies, not 200 of them.
    assert peak < 200 * max_body // 8 + 512 * 1024, peak  # fixed overhead: sqlite, json


# --- pathological Unicode -----------------------------------------------------------------------

_NFKC_CAP = 8192

UNICODE_BODIES: dict[str, Callable[[], str]] = {
    "combining runs of 30": lambda: ff.body_mark_runs(8192),
    "u+fdfa to the cap": lambda: ff.body_expanding(ff.EXPANDING["u+fdfa"], 8192 // 11),
    "ligatures": lambda: ff.body_expanding(ff.EXPANDING["ligature-ffi"], 8192),
    "roman numerals": lambda: ff.body_expanding(ff.EXPANDING["roman-12"], 8192),
    "parenthesized digits": lambda: ff.body_expanding(ff.EXPANDING["paren-digit"], 8192),
    "astral digits (u16 2, b8 4)": lambda: ff.body_expanding(ff.EXPANDING["astral-math"], 8192),
    "astral emoji lines": lambda: ("\U0001f600" * 40 + "\n") * 50,
    "CJK": lambda: "中" * 2700,
    "bidi and zero width": lambda: ff.BIDI_ZERO_WIDTH + "visible",
    "mixed": lambda: ff.body_mixed(__import__("random").Random(28), 8192),
}


@pytest.mark.parametrize("name", sorted(UNICODE_BODIES))
def test_pathological_unicode_bodies_print_a_well_formed_block_inside_both_budgets(
    gw: FakeGateway, store: Store, counter: Counter, name: str
) -> None:
    """The same body in 200 notes. Whatever its size under NFKC, the printed block is inside
    ``max_chars`` (UTF-16 code units) and ``max_bytes`` (UTF-8 bytes), measured on the block."""
    body = UNICODE_BODIES[name]()
    insert(store, [Note(topic=f"t{i}", body=body) for i in range(200)])
    start = time.monotonic()
    code, doc = context_json(gw)
    took = time.monotonic() - start
    assert took < 5 * ROOMY_DEADLINE, took  # a hang is a failure; the budget is the deadline
    assert code == 0, (code, gw.terminal.stderr)  # every body, bidi and zero width included
    assert doc["eligible"] == 200
    assert_within_budgets(doc)
    assert counter.calls <= 200 + 100  # one pass and the re-read of the chosen notes


def test_a_tiny_budget_with_wide_characters_counts_utf16_and_utf8_separately(
    gw: FakeGateway, store: Store
) -> None:
    """Astral characters take 2 UTF-16 units and 4 bytes: with a 1000-unit budget the char
    budget binds first for 2-unit characters, the byte budget for 3-byte ones."""
    write_config(gw, "context:\n  max_chars: 1000\n  max_bytes: 1000\n  note_max_chars: 1000\n")
    insert(
        store,
        [Note(topic=f"t{i}", body="\U0001f600" * 150 + "\n") for i in range(10)]
        + [Note(topic=f"u{i}", body="中" * 300 + "\n") for i in range(10)],
    )
    code, doc = context_json(gw)
    assert code == 0
    assert_within_budgets(doc, 1000, 1000)


# --- the worst case at the default config -------------------------------------------------------


def worst_case_store(store: Store) -> None:
    """200 notes (``live_per_repo``) of the largest default body (8192 bytes) made of 30-mark
    runs of mixed combining classes: the most expensive text a note can hold at the defaults."""
    insert(store, [Note(topic=f"t{i}", body=ff.body_mark_runs(8192)) for i in range(200)])


def test_200_worst_case_notes_print_a_block_with_one_scan_pass_plus_the_chosen_notes(
    gw: FakeGateway, store: Store, counter: Counter
) -> None:
    worst_case_store(store)
    code, doc = context_json(gw)
    assert code == 0 and doc["eligible"] == 200
    assert_within_budgets(doc)
    assert counter.calls == 200 + doc["shown"]


def child_startup(gw: FakeGateway) -> float:
    """Seconds one bare child takes (interpreter, imports, ``--version``): the part of a child
    run that the in-process scan does not include."""
    t0 = time.monotonic()
    r = subprocess.run(
        private_home.argv(gw.home(), "--version"),
        capture_output=True, env=env_for(gw.home()), cwd=gw.home(), timeout=60, check=False,
    )  # fmt: skip
    took = time.monotonic() - t0
    assert r.returncode == 0, (r.returncode, r.stderr)
    return took


def assert_child_inside_default_deadline(gw: FakeGateway, store: Store) -> None:
    """The real entry point, the real timer, the default 2 s deadline, on the store as it is.
    The same scan runs in process first; the child must exit 0 with all 200 notes eligible.
    Skipped when the in-process time plus the measured child start-up is more than half the
    deadline (1 s): on such a machine the check says nothing about the code."""
    store.conn.close()
    t0 = time.monotonic()
    code = run(gw, "--repo", REPO, "--json")
    in_process = time.monotonic() - t0
    assert code == 0
    estimate = in_process + child_startup(gw)
    if estimate > 0.5 * DEFAULT_DEADLINE:
        pytest.skip(f"machine too slow for the deadline check ({estimate:.2f} s estimated)")
    r = child(gw.home(), ["--repo", REPO, "--json"])
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert json.loads(r.stdout)["eligible"] == 200


def test_200_worst_case_notes_finish_inside_the_default_deadline(
    gw: FakeGateway, store: Store
) -> None:
    """The 8192-byte 30-mark-run body, 200 notes, in a child at the default 2 s deadline
    (roughly 0.6 to 1.1 s here)."""
    worst_case_store(store)
    assert_child_inside_default_deadline(gw, store)


@pytest.mark.parametrize("name", ["u+fdfa to the cap", "ligatures"])
def test_200_worst_case_unicode_expansion_notes_finish_inside_the_default_deadline(
    gw: FakeGateway, store: Store, name: str
) -> None:
    """The worst storable NFKC expanders at the default config, 200 notes each, one child each
    at the default deadline (the in-process matrix above never checks the deadline)."""
    body = UNICODE_BODIES[name]()
    insert(store, [Note(topic=f"t{i}", body=body) for i in range(200)])
    assert_child_inside_default_deadline(gw, store)


# --- the read-side NFKC cost: rows only a foreign writer can leave ------------------------------


def longest_mark_run(text: str) -> int:
    longest = run_len = 0
    for ch in text:
        run_len = run_len + 1 if unicodedata.combining(ch) else 0
        longest = max(longest, run_len)
    return longest


def test_one_huge_combining_run_in_a_foreign_row_is_cut_before_it_costs(
    gw: FakeGateway, store: Store, counter: Counter
) -> None:
    """A run of 30,000 marks of mixed classes (``add`` at the default config refuses over 30).
    The sanitizer drops the marks past 30, so 50 such rows scan in a time linear in their size,
    not quadratic, and the printed text has no run of more than 30 marks; a normal note (the
    newest) still shows."""
    insert(store, [Note(topic=f"t{i}", body=ff.body_single_mark_run(30_000)) for i in range(50)])
    (normal_id,) = insert(store, [Note(topic="normal", body="a normal note")])
    start = time.monotonic()
    code, doc = context_json(gw)
    took = time.monotonic() - start
    assert code == 0 and doc["eligible"] == 51
    assert_within_budgets(doc)
    assert normal_id in doc["note_ids"] and "a normal note" in doc["text"]
    assert longest_mark_run(doc["text"]) <= 30
    assert took < ROOMY_DEADLINE, took  # 30,000 marks unbounded: 46 s measured, cap removed


def test_foreign_64_kib_expansion_bodies_hit_the_deadline_with_no_partial_output(
    gw: FakeGateway, store: Store
) -> None:
    """200 rows of U+FDFA to 64 KiB (each folds to 18 characters; ``add`` at the default config
    refuses them under body rule 6) cost roughly 15 to 20 s to sanitize here. The deadline is
    the budget, and the test asserts the fail-closed exit 12 ``timeout`` with an empty stdout on
    purpose, not "finished within the deadline": rule 6 keeps such bodies out of a store written
    by ``add``, so the read side is not obliged to print them; it must never print a partial
    block. Skipped only when the machine scans them inside 1 s."""
    insert(
        store,
        [Note(topic=f"t{i}", body=ff.body_expanding(ff.EXPANDING["u+fdfa"], 65536))
         for i in range(200)],
    )  # fmt: skip
    store.conn.close()
    set_config(gw, "context:\n  deadline: 1\n")
    start = time.monotonic()
    r = child(gw.home(), ["--repo", REPO])
    took = time.monotonic() - start
    if r.returncode == 0:
        pytest.skip("scan finished inside the deadline on this machine")
    assert r.returncode == 12 and r.stdout == b"" and r.stderr == TIMEOUT_LINE
    assert took < 10, took


# --- heavy variants: opt in with ``make perf`` --------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def scratch(tmp_path: Path) -> Iterator[Path]:
    free = shutil.disk_usage(tmp_path).free
    if free < 1 << 30:
        pytest.skip("less than 1 GiB free for the heavy fixtures")
    try:
        yield tmp_path
    finally:
        shutil.rmtree(tmp_path / "heavy", ignore_errors=True)


@pytest.mark.perf
def test_perf_5000_notes_of_8_kib_in_one_repo_at_the_default_total(scratch: Path) -> None:
    """The default ``limits.total`` (5000) all in one repo, at the 8 KiB body limit (about 44 MB
    on disk; ``add`` at the default config stops at ``live_per_repo`` 200, a foreign writer does
    not). Measured
    roughly 0.4 to 1.7 s depending on the machine (tracemalloc on), inside the 2 s deadline."""
    gw = FakeGateway(scratch / "heavy")
    s = Store(gw)
    insert(s, [Note(topic=f"t{i % 50}", body=ff.body_ascii(8192)) for i in range(5000)])
    s.conn.close()
    set_config(gw, "context:\n  deadline: 10\n")
    code, doc = _measure(gw, "5000 x 8 KiB ascii", trace=True)
    assert code == 0 and doc["eligible"] == 5000
    assert_within_budgets(doc)


@pytest.mark.perf
def test_perf_200_notes_of_64_kib_mark_runs_at_the_maximum_body_limit(scratch: Path) -> None:
    """The largest body (``limits.max_body_bytes`` 65536) in 30-mark runs, 200 live notes (13 MB).
    Measured about 4 to 6 s here: over the default deadline, so the answer is exit 12 (fail
    closed) unless ``context.deadline`` is raised. The test records the time at the roomy 10 s
    deadline (exit 0 expected), then runs a child at the DEFAULT deadline and accepts exit 0 or
    12 (empty stdout on 12)."""
    gw = FakeGateway(scratch / "heavy")
    s = Store(gw)
    insert(s, [Note(topic=f"t{i}", body=ff.body_mark_runs(65536)) for i in range(200)])
    s.conn.close()
    set_config(gw, "context:\n  deadline: 10\n")
    code, doc = _measure(gw, "200 x 64 KiB mark runs", allow_timeout=True)
    if code == 0:
        assert_within_budgets(doc)
    set_config(gw, "context:\n  deadline: 2\n")
    r = child(gw.home(), ["--repo", REPO])
    assert r.returncode in (0, 12), (r.returncode, r.stderr)
    if r.returncode == 12:
        assert r.stdout == b"" and r.stderr == TIMEOUT_LINE


@pytest.mark.perf
def test_perf_the_scan_is_linear_in_the_notes_of_the_repo(scratch: Path) -> None:
    """Operation counts at 10k and 100k notes: bodies sanitized grow with the notes, not with the
    square (it grows by exactly 90,000 sanitize calls), and the 100k store at the roomy
    10 s deadline prints a complete block."""
    counts: dict[int, int] = {}
    for n in (10_000, 100_000):
        gw = FakeGateway(scratch / "heavy" / str(n))
        s = Store(gw)
        insert(s, [Note(topic=f"t{i % 50}", body="word " * 80) for i in range(n)])
        s.conn.close()
        set_config(gw, "context:\n  deadline: 10\n")
        c = Counter()
        mp = pytest.MonkeyPatch()
        mp.setattr(cmd_context, "sanitize", c)
        try:
            code, doc = _measure(gw, f"{n} x 400 B")
        finally:
            mp.undo()
        assert code == 0 and doc["eligible"] == n
        counts[n] = c.calls
    assert counts[100_000] - counts[10_000] == 90_000  # the same chosen notes both times


def _measure(
    gw: FakeGateway, label: str, *, allow_timeout: bool = False, trace: bool = False
) -> tuple[int, dict[str, Any]]:
    """Run ``context`` in this process, print what it cost on stderr (a run report, not an
    assertion), and return the exit code and the parsed document."""
    db = gw.home() / ".local" / "state" / "memoctl" / "state.db"
    if trace:
        tracemalloc.start()
    start = time.monotonic()
    try:
        code = run(gw, "--repo", REPO, "--json")
        took = time.monotonic() - start
        peak = tracemalloc.get_traced_memory()[1] if trace else 0
    finally:
        if trace:
            tracemalloc.stop()
    doc = json.loads(gw.terminal.stdout) if gw.terminal.stdout else {}
    print(
        f"[perf] {label}: exit {code}, db {db.stat().st_size / 1e6:.1f} MB, "
        f"{took:.2f} s, peak {peak / 1e6:.1f} MB (0 = not traced)",
        file=sys.stderr,
    )
    assert allow_timeout or code == 0
    return code, doc
