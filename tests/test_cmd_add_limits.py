"""``memoctl add`` step 12: the write limits and the rate table (interface.md, Limits).

Every test runs on the fake gateway's private home (or a temp home for the child processes) and
never touches the real one. Limits are set through the config file, as in production.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from memoctl.cli_errors import failure_for
from memoctl.config import Limits
from memoctl.notes import (
    LimitCounts,
    LimitRefusal,
    check_limits,
    evaluate_limits,
    rate_retry_after,
)
from memoctl.statedir import open_state_dir
from memoctl.store import Mode, StoreError, open_store
from tests import private_home
from tests.fake_gateway import EPOCH, FakeGateway
from tests.golden_files import check_golden
from tests.test_cmd_add import (
    REPO,
    add,
    error_of,
    log_records,
    snapshot,
    state_dir,
    stored,
    write_config,
)

__all__ = ["gw"]

NOW = int(EPOCH.timestamp())
OTHER = "example.com/team/other"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def limits(gw: FakeGateway, **keys: object) -> None:
    write_config(gw, "limits:\n" + "".join(f"  {k}: {v}\n" for k, v in keys.items()))


def db(gw: FakeGateway, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    try:
        found = conn.execute(statement, params).fetchall()
        conn.commit()
        return found
    finally:
        conn.close()


def rate_rows(gw: FakeGateway) -> list[tuple[str, int]]:
    return db(gw, "SELECT repo, at FROM rate ORDER BY rowid")


def init_store(gw: FakeGateway) -> None:
    """Create the empty database without writing a note."""
    state = open_state_dir(gw, create=True)
    assert state is not None
    with state:
        store = open_store(state, Mode.INIT)
        assert store is not None
        store.close()


def plant_rate(gw: FakeGateway, repo: str, *ats: int) -> None:
    init_store(gw)
    for at in ats:
        db(gw, "INSERT INTO rate (repo, at) VALUES (?, ?)", repo, at)


def plant_notes(
    gw: FakeGateway, repo: str, count: int, *, expires: int = NOW + 86400, superseded: bool = False
) -> None:
    """``count`` valid note rows (no rate rows), live unless told otherwise."""
    init_store(gw)
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    conn.executemany(
        "INSERT INTO notes (repo, topic, kind, body, body_sha256, size, created, expires,"
        " superseded_by) VALUES (?, 'planted', 'unknown', ?, ?, ?, ?, ?, ?)",
        [
            (repo, f"p{i}", f"{i:064x}", len(f"p{i}"), NOW, expires, 999 if superseded else None)
            for i in range(count)
        ],
    )
    conn.commit()
    conn.close()


def refused(gw: FakeGateway, *argv: str, **kwargs: Any) -> dict[str, Any]:
    code, out, err = add(gw, *argv, **kwargs)
    assert code == 20, out
    error = error_of(out)
    assert err.startswith(f"refused ({error['reason']}): ")
    return error


def ok(gw: FakeGateway, *argv: str, **kwargs: Any) -> dict[str, Any]:
    code, out, _ = add(gw, *argv, **kwargs)
    assert code == 0, out
    return json.loads(out)  # type: ignore[no-any-return]


def assert_untouched_after(gw: FakeGateway, action: Any) -> None:
    """The refusal wrote no note, label, rate row or log record."""
    before = (stored(gw), rate_rows(gw), log_records(gw))
    action()
    assert (stored(gw), rate_rows(gw), log_records(gw)) == before


# -- 1. repo_full ----------------------------------------------


def test_repo_full_refuses_at_exactly_the_limit_and_never_below(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=3)
    for i in range(3):  # limit - 1 live notes admit one more, up to the limit
        ok(gw, body=f"n{i}")
    error = refused(gw, body="n3")
    assert (error["reason"], error["details"], error["retryable"]) == (
        "repo_full", {"limit": 3}, False,
    )  # fmt: skip
    assert len(stored(gw)) == 3 and len(rate_rows(gw)) == 3 and len(log_records(gw)) == 3


def test_repo_full_golden(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1)
    ok(gw)
    code, out, err = add(gw, body="second")
    assert code == 20
    check_golden("add/repo-full.json", out.encode(), schema="cli/error")
    check_golden("add/repo-full-err.txt", err.encode())


def test_other_repos_do_not_count_toward_repo_full(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1)
    ok(gw)
    assert ok(gw, repo=OTHER, body="b")["created"] is True


def test_expired_and_superseded_notes_do_not_count_as_live(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=2, total=100)
    plant_notes(gw, REPO, 5, expires=NOW)  # expires <= now: expired, not live
    plant_notes(gw, REPO, 5, superseded=True)
    plant_notes(gw, REPO, 1, expires=NOW + 1)  # live: the boundary just inside
    assert ok(gw, body="x")["created"] is True  # 1 live + 1 = 2, not over 2
    assert refused(gw, body="y")["reason"] == "repo_full"  # 2 live + 1 > 2


def test_a_note_expiring_exactly_now_is_not_live_and_one_second_later_is(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=100)
    plant_notes(gw, REPO, 1, expires=NOW)
    assert ok(gw, body="x")["created"] is True
    gw.clock.advance(timedelta(seconds=1))
    assert refused(gw, body="y")["reason"] == "repo_full"


def test_the_repo_override_of_live_per_repo_wins_over_the_global_figure(gw: FakeGateway) -> None:
    write_config(
        gw,
        f"limits:\n  live_per_repo: 5\nrepos:\n  {REPO}:\n    limits:\n      live_per_repo: 1\n",
    )
    ok(gw)
    assert refused(gw, body="b")["details"] == {"limit": 1}
    assert ok(gw, repo=OTHER, body="b")["created"] is True
    assert ok(gw, repo=OTHER, body="c")["created"] is True  # the global 5 applies there


# -- 2. store_full ----------------------------------------------


def test_store_full_counts_every_note_of_every_status_and_repo(gw: FakeGateway) -> None:
    limits(gw, total=4, live_per_repo=4, adds_per_window=100, adds_per_window_total=1000)
    plant_notes(gw, OTHER, 1, expires=NOW)  # expired
    plant_notes(gw, OTHER, 1, superseded=True)
    plant_notes(gw, "example.com/team/third", 1)
    ok(gw, body="a")  # 4 = total: the fourth row is admitted
    error = refused(gw, body="b")
    assert (error["reason"], error["details"]) == ("store_full", {"limit": 4})
    assert len(stored(gw)) == 4


def test_store_full_golden(gw: FakeGateway) -> None:
    limits(gw, total=1, live_per_repo=1)
    ok(gw)
    code, out, _ = add(gw, repo=OTHER, body="b")
    assert code == 20
    check_golden("add/store-full.json", out.encode(), schema="cli/error")


def test_repo_full_is_reported_before_store_full(gw: FakeGateway) -> None:
    limits(gw, total=1, live_per_repo=1)
    ok(gw)
    assert refused(gw, body="b")["reason"] == "repo_full"  # both fail: the first wins


# -- 3. rate_limited ----------------------------------------------


def test_rate_limited_per_repo_at_exactly_the_limit(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=2, adds_per_window_total=10)
    ok(gw, body="a")
    ok(gw, body="b")
    error = refused(gw, body="c")
    assert error["reason"] == "rate_limited" and error["retryable"] is True
    assert error["details"] == {"retry_after_s": 600, "limit": 2, "scope": "repo"}
    assert len(rate_rows(gw)) == 2


def test_rate_limited_golden(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=10)
    ok(gw)
    gw.clock.advance(timedelta(seconds=100))
    code, out, err = add(gw, body="b")
    assert code == 20
    check_golden("add/rate-limited.json", out.encode(), schema="cli/error")
    check_golden("add/rate-limited-err.txt", err.encode())


def test_rate_limited_store_scope_and_its_details(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=3, adds_per_window_total=3)
    for i, repo in enumerate((REPO, OTHER, "example.com/team/third")):
        gw.clock.advance(timedelta(seconds=10))
        ok(gw, repo=repo, body=f"n{i}")
    gw.clock.advance(timedelta(seconds=10))
    error = refused(gw, repo="example.com/team/fourth", body="x")
    # the oldest counted row is 30 s old: it leaves the window in 600 - 30 s
    assert error["details"] == {"retry_after_s": 570, "limit": 3, "scope": "store"}


def test_repo_rate_is_reported_before_store_rate(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=1)
    ok(gw)
    assert refused(gw, body="b")["details"]["scope"] == "repo"


def test_store_full_is_reported_before_rate_limited(gw: FakeGateway) -> None:
    limits(gw, total=1, live_per_repo=1, adds_per_window=1, adds_per_window_total=1)
    ok(gw)
    assert refused(gw, repo=OTHER, body="b")["reason"] == "store_full"


@pytest.mark.parametrize("scope", ["repo", "store"])
@pytest.mark.parametrize(
    ("offset", "counted"),
    [(-601, False), (-600, False), (-599, True), (-1, True), (0, True), (1, False)],
)
def test_the_window_is_open_below_and_closed_above(
    gw: FakeGateway, scope: str, offset: int, counted: bool
) -> None:
    """``now - W < at <= now``: a row exactly W old has left the window; a row at ``now`` is in;
    a row one second ahead of ``now`` is not counted yet. Same for both scopes."""
    if scope == "repo":
        limits(gw, adds_per_window=1, adds_per_window_total=100)
        plant_rate(gw, REPO, NOW + offset)
    else:
        limits(gw, adds_per_window=1, adds_per_window_total=1)
        plant_rate(gw, OTHER, NOW + offset)  # another repo: only the store scope sees it
    if counted:
        error = refused(gw)
        assert error["reason"] == "rate_limited" and error["details"]["scope"] == scope
    else:
        assert ok(gw)["created"] is True


@pytest.mark.parametrize(
    ("offset", "counted"), [(-300, False), (-299, True), (0, True), (1, False)]
)
def test_the_window_boundary_follows_a_non_default_window(
    gw: FakeGateway, offset: int, counted: bool
) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=100, window="5m")
    plant_rate(gw, REPO, NOW + offset)
    if counted:
        assert refused(gw)["reason"] == "rate_limited"
    else:
        assert ok(gw)["created"] is True


@pytest.mark.parametrize(
    ("ats", "expected"),
    [
        ([-10, -5], 590),  # k = L = 2: the oldest, 10 s old
        ([-30, -10, -5], 590),  # k = 3 > L = 2: a_(k-L+1) = a_2 = -10: two rows must leave
        ([-599, -599], 1),  # both about to leave: at least 1
        ([0, 0], 600),  # both brand new: the whole window
    ],
)
def test_retry_after_is_when_the_row_that_frees_a_slot_leaves(
    gw: FakeGateway, ats: list[int], expected: int
) -> None:
    limits(gw, adds_per_window=2, adds_per_window_total=100)
    plant_rate(gw, REPO, *(NOW + a for a in ats))
    error = refused(gw)
    assert error["details"] == {"retry_after_s": expected, "limit": 2, "scope": "repo"}


def test_store_scope_retry_after_with_more_counted_rows_than_the_limit(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=2, adds_per_window_total=2)
    plant_rate(gw, OTHER, NOW - 30, NOW - 10, NOW - 5)
    error = refused(gw)
    # k = 3 > L = 2: a_(k-L+1) = a_2 = -10, so 600 - 10
    assert error["details"] == {"retry_after_s": 590, "limit": 2, "scope": "store"}


def test_the_repo_override_of_adds_per_window_wins_over_the_global_figure(gw: FakeGateway) -> None:
    write_config(
        gw,
        "limits:\n  adds_per_window: 5\n  adds_per_window_total: 100\n"
        f"repos:\n  {REPO}:\n    limits:\n      adds_per_window: 1\n",
    )
    ok(gw)
    assert refused(gw, body="b")["details"] == {
        "retry_after_s": 600,
        "limit": 1,
        "scope": "repo",
    }
    for i in range(5):  # the global 5 applies to another repo
        assert ok(gw, repo=OTHER, body=f"o{i}")["created"] is True
    assert refused(gw, repo=OTHER, body="o5")["details"]["limit"] == 5


def test_retry_after_is_honest_a_retry_at_that_time_succeeds(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=2, adds_per_window_total=100, window="5m")
    ok(gw, body="a")
    gw.clock.advance(timedelta(seconds=40))
    ok(gw, body="b")
    gw.clock.advance(timedelta(seconds=7))
    wait = refused(gw, body="c")["details"]["retry_after_s"]
    assert wait == 300 - 47
    gw.clock.advance(timedelta(seconds=wait - 1))
    assert refused(gw, body="c")["details"]["retry_after_s"] == 1
    gw.clock.advance(timedelta(seconds=1))
    assert ok(gw, body="c")["created"] is True


def test_rows_of_other_repos_do_not_count_for_the_repo_rate_but_do_for_the_store(
    gw: FakeGateway,
) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=3)
    plant_rate(gw, OTHER, NOW, NOW - 1)
    assert ok(gw)["created"] is True  # repo: 0 + 1 <= 1; store: 2 + 1 <= 3
    assert refused(gw, body="b")["details"]["scope"] == "repo"
    assert refused(gw, repo="example.com/team/third", body="c")["details"]["scope"] == "store"


def test_a_clock_that_moves_backward_does_not_count_rows_from_the_future(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=1)
    ok(gw)
    gw.clock.advance(timedelta(hours=-1))
    assert ok(gw, body="b")["created"] is True  # the first row is dated ahead of now
    assert refused(gw, body="c")["details"]["retry_after_s"] == 600


def test_a_clock_that_jumps_forward_frees_the_window(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1)
    ok(gw)
    gw.clock.advance(timedelta(days=400))
    assert ok(gw, body="b")["created"] is True


def test_planted_rate_rows_are_counted_by_their_instant_only(gw: FakeGateway) -> None:
    """Negative, zero, huge, duplicate and 64-bit extreme instants never raise or miscount."""
    limits(gw, adds_per_window=3, adds_per_window_total=1000)
    plant_rate(gw, REPO, -1, 0, -(2**63), 2**63 - 1, 2**62, NOW + 10**12, NOW, NOW, NOW)
    # three duplicates at now are counted, the others are outside the window
    error = refused(gw)
    assert error["details"] == {"retry_after_s": 600, "limit": 3, "scope": "repo"}


def test_a_repo_with_a_hostile_name_in_the_rate_table_is_just_another_repo(
    gw: FakeGateway,
) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=2)
    plant_rate(gw, "x'; DROP TABLE rate;--", NOW)
    plant_rate(gw, "", NOW)
    assert refused(gw)["details"]["scope"] == "store"  # 2 rows + 1 > 2; the repo has none
    assert len(rate_rows(gw)) == 2


def test_the_rate_row_counts_creation_time_not_commit_time(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1)
    ok(gw)
    assert rate_rows(gw) == [(REPO, NOW)]


def test_many_repos_each_have_their_own_repo_budget(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=40, live_per_repo=1, total=100)
    for i in range(30):
        assert ok(gw, repo=f"example.com/team/r{i}")["created"] is True
    assert refused(gw, repo="example.com/team/r7", body="again")["reason"] == "repo_full"
    assert len(rate_rows(gw)) == 30


# -- 4. dedupe and --supersedes ----------------------------------------------


def test_a_dedupe_hit_consumes_no_budget_and_skips_every_limit(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=1, adds_per_window=1, adds_per_window_total=1)
    first = ok(gw)
    assert_untouched_after(gw, lambda: None)
    again = ok(gw)  # everything is full, but this is a retry of a stored note
    assert (again["created"], again["id"]) == (False, first["id"])
    assert len(rate_rows(gw)) == 1 and len(log_records(gw)) == 1


def test_a_supersede_that_replaces_a_live_note_is_net_zero_for_repo_full(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=100)
    ok(gw, body="old")
    doc = ok(gw, "--supersedes", "1", body="new")
    assert doc["created"] is True and doc["supersedes"] == 1
    assert refused(gw, body="third")["reason"] == "repo_full"  # still one live note


def test_a_supersede_of_an_expired_target_does_not_subtract(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=100)
    plant_notes(gw, REPO, 1, expires=NOW)  # the target, expired
    plant_notes(gw, REPO, 1, expires=NOW + 100)  # the one live note
    error = refused(gw, "--supersedes", "1", body="new")
    assert (error["reason"], error["details"]) == ("repo_full", {"limit": 1})


def test_a_supersede_of_an_expired_target_is_admitted_with_room(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=100)
    plant_notes(gw, REPO, 1, expires=NOW)
    assert ok(gw, "--supersedes", "1", body="new")["created"] is True


def test_a_supersede_still_adds_a_row_so_store_full_applies(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1, total=1)
    ok(gw, body="old")
    assert refused(gw, "--supersedes", "1", body="new")["reason"] == "store_full"


def test_a_supersede_still_spends_the_rate_budget(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=5)
    ok(gw, body="old")
    assert refused(gw, "--supersedes", "1", body="new")["reason"] == "rate_limited"


def test_a_refused_supersede_leaves_the_target_live(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=1, adds_per_window_total=5)
    ok(gw, body="old")
    refused(gw, "--supersedes", "1", body="new")
    assert [r.superseded_by for r in stored(gw)] == [None]


# -- 5. order against the earlier steps ----------------------------------------------

# Each fault is (name, arguments, setup) and is a refusal of an earlier step, with the limits
# already exceeded (see the fixture below). The earlier step wins.


def _full(gw: FakeGateway) -> None:
    """Every limit exhausted by one stored note (ids: 1), the clock at its creation."""
    limits(gw, live_per_repo=1, total=1, adds_per_window=1, adds_per_window_total=1)
    ok(gw, body="stored")


def _secret_body() -> str:
    return "ghp_" + "a1B2c3D4e5" * 4  # a GitHub-shaped token, assembled at run time


EARLIER = {
    "usage_label": (["--label", "Bad Key=v"], "stored", 2),
    "body_too_long": ([], "x" * 70000, 2),
    "secret": ([], _secret_body(), 20),
    "supersede_not_found": (["--supersedes", "77"], "new", 10),
    "supersede_mismatch": (["--supersedes", "1", "--repo", OTHER], "new", 20),
    "dedupe_hit": ([], "stored", 0),
}


@pytest.mark.parametrize("name", sorted(EARLIER))
def test_every_earlier_step_wins_over_a_full_store(gw: FakeGateway, name: str) -> None:
    argv, body, expected = EARLIER[name]
    _full(gw)
    before = (stored(gw), rate_rows(gw), log_records(gw))
    code, out, _ = add(gw, *argv, body=body, repo=None if "--repo" in argv else REPO)
    assert code == expected, out
    if expected not in (0,):
        reason = error_of(out)["reason"]
        assert reason not in ("repo_full", "store_full", "rate_limited")
    assert (stored(gw), rate_rows(gw), log_records(gw)) == before


def test_already_superseded_wins_over_a_full_store(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=5, total=5, adds_per_window=5, adds_per_window_total=5)
    ok(gw, body="a")
    ok(gw, "--supersedes", "1", body="b")
    limits(gw, live_per_repo=1, total=1, adds_per_window=1, adds_per_window_total=1)
    assert refused(gw, "--supersedes", "1", body="c")["reason"] == "already_superseded"


def test_the_step_12_failure_beats_every_later_step_by_writing_nothing(gw: FakeGateway) -> None:
    _full(gw)
    assert_untouched_after(gw, lambda: refused(gw, body="other"))


# Pairs of limit refusals: the state is one live note, one superseded note and one rate row of
# the repo at ``now``; each fault is the configuration that makes one limit fail on it.
LIMIT_FAULTS = {
    "repo_full": dict(live_per_repo=1),
    "store_full": dict(live_per_repo=2, total=2),
    "rate_repo": dict(adds_per_window=1),
    "rate_store": dict(adds_per_window=1, adds_per_window_total=1),
}
ORDER = ["repo_full", "store_full", "rate_repo", "rate_store"]
EXPECTED = {
    "repo_full": ("repo_full", None),
    "store_full": ("store_full", None),
    "rate_repo": ("rate_limited", "repo"),
    "rate_store": ("rate_limited", "repo"),  # adds_per_window=1 fails first: the repo scope
}


@pytest.mark.parametrize(("a", "b"), list(itertools.combinations(ORDER, 2)), ids=lambda p: p)
def test_of_two_limit_faults_the_earlier_in_the_documented_order_wins(
    gw: FakeGateway, a: str, b: str
) -> None:
    plant_notes(gw, REPO, 1)
    plant_notes(gw, REPO, 1, superseded=True)
    plant_rate(gw, REPO, NOW)
    merged: dict[str, int] = {}
    for fault in (a, b):
        for key, value in LIMIT_FAULTS[fault].items():
            merged[key] = min(merged.get(key, value), value)
    limits(gw, **merged)
    reason, scope = EXPECTED[a]
    error = refused(gw, body="second")
    assert error["reason"] == reason
    if scope is not None:
        assert error["details"]["scope"] == scope
    assert len(stored(gw)) == 2 and len(rate_rows(gw)) == 1


# -- 6. what a refusal leaves behind ----------------------------------------------


def test_a_refusal_prints_nothing_on_stdout_in_human_mode_and_one_status_line(
    gw: FakeGateway,
) -> None:
    limits(gw, live_per_repo=1)
    ok(gw)
    code, out, err = add(gw, body="b", json_mode=False)
    assert (code, out) == (20, "")
    assert err == "refused (repo_full): the repo has reached its live note limit\n"


def test_a_refusal_changes_no_file_in_the_state_directory(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1)
    ok(gw)
    # the WAL and shm sidecars are not part of the contract: compare the log and the config
    before = {k: v for k, v in snapshot(gw.home()).items() if not k.endswith(("-wal", "-shm"))}
    refused(gw, body="b")
    after = {k: v for k, v in snapshot(gw.home()).items() if not k.endswith(("-wal", "-shm"))}
    assert {k: v for k, v in after.items() if "state.db" not in k} == {
        k: v for k, v in before.items() if "state.db" not in k
    }
    assert len(rate_rows(gw)) == 1 and len(log_records(gw)) == 1


def test_a_refused_add_rolls_back_and_a_later_add_works_once_room_exists(
    gw: FakeGateway,
) -> None:
    """A refusal inside the write transaction rolls it back; the next add works once the first
    note is no longer live."""
    limits(gw, live_per_repo=1)
    ok(gw)
    refused(gw, body="b")
    db(gw, "UPDATE notes SET superseded_by = 1000")  # the note is no longer live
    assert ok(gw, body="b")["created"] is True


def test_the_limit_check_reads_the_clock_value_of_step_7_only(gw: FakeGateway) -> None:
    limits(gw, adds_per_window=5)
    ok(gw)
    assert gw.now_calls == 1


def test_failure_mapping_is_registered_for_the_limit_errors() -> None:
    from memoctl.cli_errors import RateLimitedError, RepoFullError, StoreFullError

    assert failure_for(RepoFullError(3)).details == {"limit": 3}
    assert failure_for(StoreFullError(4)).reason == "store_full"
    failure = failure_for(RateLimitedError(retry_after_s=5, limit=2, scope="store"))
    assert failure.details == {"retry_after_s": 5, "limit": 2, "scope": "store"}
    assert failure.retryable is True


# -- 7. the pure rules (what #19 calls) ----------------------------------------------


def counts(**kw: int) -> LimitCounts:
    base = dict(live_in_repo=0, notes_in_store=0, rate_in_repo=0, rate_in_store=0)
    return LimitCounts(**{**base, **kw})


def check(c: LimitCounts, *, target_live: bool = False, **kw: int) -> LimitRefusal | None:
    figures = Limits(**kw) if kw else Limits()
    return check_limits(c, figures, target_live=target_live)


@pytest.mark.parametrize(
    ("live", "target_live", "refused_"),
    [
        (198, False, False),
        (199, False, False),  # 199 + 1 = 200, not over
        (200, False, True),
        (201, False, True),
        (200, True, False),  # the target frees its own slot
        (201, True, True),
        (0, True, False),  # a planted target count that exceeds the live count: no underflow
    ],
)
def test_check_limits_repo_full_comparison(live: int, target_live: bool, refused_: bool) -> None:
    got = check(counts(live_in_repo=live), target_live=target_live)
    assert (got is not None and got.reason == "repo_full") is refused_
    if got is not None:
        assert got.limit == 200


@pytest.mark.parametrize(("n", "refused_"), [(4998, False), (4999, False), (5000, True)])
def test_check_limits_store_full_comparison(n: int, refused_: bool) -> None:
    got = check(counts(notes_in_store=n))
    assert (got is not None and got.reason == "store_full") is refused_


@pytest.mark.parametrize(("n", "refused_"), [(18, False), (19, False), (20, True), (2**70, True)])
def test_check_limits_rate_comparisons_never_overflow(n: int, refused_: bool) -> None:
    got = check(counts(rate_in_repo=n))
    assert (got is not None and got.scope == "repo") is refused_
    got = check(counts(rate_in_store=n * 3))
    assert (got is not None and got.scope == "store") is refused_


def test_check_limits_order_and_none() -> None:
    everything = counts(live_in_repo=10**6, notes_in_store=10**6, rate_in_repo=10**6)
    got = check(everything)
    assert got is not None and got.reason == "repo_full"
    assert check(counts()) is None


@pytest.mark.parametrize(
    ("pivot", "window", "now", "expected"),
    [(NOW - 10, 600, NOW, 590), (NOW - 599, 600, NOW, 1), (NOW, 600, NOW, 600),
     (NOW - 600, 600, NOW, 1), (NOW - 10**9, 600, NOW, 1)],
)  # fmt: skip
def test_rate_retry_after_is_integer_seconds_at_least_one(
    pivot: int, window: int, now: int, expected: int
) -> None:
    got = rate_retry_after(pivot, window, now)
    assert got == expected and type(got) is int


def test_evaluate_limits_reads_a_store_without_writing(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=1)
    ok(gw)
    state = open_state_dir(gw, create=False)
    assert state is not None
    with state:
        store = open_store(state, Mode.READ)
        assert store is not None
        with store.read():
            found = evaluate_limits(store, Limits(live_per_repo=1), REPO, NOW, target_live=False)
            assert found is not None and found.reason == "repo_full"
            assert evaluate_limits(store, Limits(), REPO, NOW, target_live=False) is None
        store.close()


# -- 8. size: bounded queries ----------------------------------------------


def test_the_limit_queries_stay_correct_with_100k_live_notes(gw: FakeGateway) -> None:
    limits(gw, live_per_repo=100000, total=1000000, adds_per_window=10000,
           adds_per_window_total=100000)  # fmt: skip
    plant_notes(gw, REPO, 99_999)
    plant_notes(gw, OTHER, 1000)
    assert ok(gw, body="one more")["created"] is True
    # 100000 live now: exactly at the limit
    assert refused(gw, body="two more")["reason"] == "repo_full"


def test_the_limit_queries_use_indexes(gw: FakeGateway) -> None:
    """EXPLAIN QUERY PLAN on the statements the Store methods really execute (traced)."""
    init_store(gw)
    state = open_state_dir(gw, create=False)
    assert state is not None
    seen: list[str] = []
    with state:
        store = open_store(state, Mode.READ)
        assert store is not None
        store._conn.set_trace_callback(seen.append)  # noqa: SLF001
        with store.read():
            for repo in (REPO, None):
                store.count_rate(repo, 9, 8)
                try:
                    store.rate_instant(repo, 9, 8, 0)
                except StoreError:
                    pass  # no rows: only the statement matters
            store.count_live(REPO, 5, 9)
            store.count_notes(9)
        store._conn.set_trace_callback(None)  # noqa: SLF001
        statements = [sql for sql in seen if sql.lstrip().upper().startswith("SELECT")]
        assert len(statements) >= 6, statements
        for sql in statements:
            plan = store._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()  # noqa: SLF001
            details = [row[3] for row in plan]
            if "FROM notes LIMIT" in sql:  # count_notes: a LIMIT-bounded scan by design
                continue
            assert any("SEARCH" in d for d in details), (sql, details)
            # scanning the count(*) subquery's own (already bounded) result is not a table scan
            scans = [d for d in details if d.startswith("SCAN") and "(subquery" not in d]
            assert not scans, (sql, details)
        store.close()


# -- 9. real processes racing for the last slot ----------------------------------------------


def test_two_processes_racing_for_the_last_slot_admit_exactly_one(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "memoctl").mkdir(parents=True, mode=0o700)
    config = home / ".config" / "memoctl" / "config.yaml"
    config.write_text("limits:\n  live_per_repo: 1\n  total: 10\n")
    config.chmod(0o600)
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(home)}
    argv = [
        *private_home.argv(home, "add", "--repo", REPO, "--topic", "race", "--body-file", "-"),
        "--json",
    ]
    procs = [
        subprocess.Popen(
            argv + (["--label", f"w={i}"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
        )
        for i in range(2)
    ]
    results = []
    try:
        for i, proc in enumerate(procs):
            out, _ = proc.communicate(f"body {i}\n".encode(), timeout=120)
            results.append((proc.returncode, out))
    finally:
        for proc in procs:
            if proc.poll() is None:  # only the handles this test started
                proc.kill()
                proc.wait()
    codes = sorted(code for code, _ in results)
    assert codes == [0, 20]
    refusal = json.loads(next(out for code, out in results if code == 20))["error"]
    assert refusal["reason"] == "repo_full"
    conn = sqlite3.connect(home / ".local" / "state" / "memoctl" / "state.db")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM rate").fetchone()[0] == 1
    conn.close()
