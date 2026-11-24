"""A seeded random walk of ``add``, ``add --supersedes``, ``delete`` and clock advances against a
plain in-memory model (issue #30's acceptance criterion; plan.md, stage 6).

The model is written from the interface text alone and shares no code with ``plan_delete``: a
note is ``(repo, expires, superseded_by)``; deleting a set D repoints each surviving note P whose
pointer names a member of D to the first note above it that is not in D (following
``superseded_by``), or clears it (reinstated) when the chain ends inside D. After every step the
real store must agree on the pointer table, the derived statuses, the ids ``delete`` printed
and the log records it appended.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_gateway import FakeGateway
from tests.test_cmd_add import add
from tests.test_cmd_delete import log_records, run

REPOS = ("example.com/a/b", "example.com/a/c")
LIFETIMES = (60, 120, 300, 900)  # seconds; add --expires accepts these
MAX_ADDS = 18  # stays under the default rate limit of 20 adds per window and repo
STEPS = 45

Model = dict[int, dict[str, Any]]  # id -> {"repo", "expires", "by"}


def model_delete(model: Model, doomed: set[int]) -> tuple[list[int], dict[int, int]]:
    """Apply a delete to ``model``; returns the reinstated ids and, per reinstated note, the
    deleted id its pointer named."""
    reinstated: dict[int, int] = {}
    for note_id in sorted(model):
        if note_id in doomed:
            continue
        pointer = model[note_id]["by"]
        if pointer is None or pointer not in doomed:
            continue
        above = pointer
        while above is not None and above in doomed:
            above = model[above]["by"]
        if above is None:
            reinstated[note_id] = pointer
        model[note_id]["by"] = above
    for note_id in doomed:
        del model[note_id]
    for note_id in reinstated:
        model[note_id]["by"] = None
    return sorted(reinstated), reinstated


def status(note: dict[str, Any], now: int) -> str:
    if note["by"] is not None:
        return "superseded"
    return "expired" if note["expires"] <= now else "live"


def table(gw: FakeGateway) -> list[tuple[int, int | None, str]]:
    code = run(gw, "list", "--all", "--json", "--limit", "500")
    assert code == 0, gw.terminal.stderr
    notes = json.loads(gw.terminal.stdout)["notes"]
    return sorted((n["id"], n["superseded_by"], n["status"]) for n in notes)


def expected(model: Model, now: int) -> list[tuple[int, int | None, str]]:
    return [(i, n["by"], status(n, now)) for i, n in sorted(model.items())]


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_walk_matches_the_model(tmp_path: Path, seed: int) -> None:
    gw = FakeGateway(tmp_path / "gw")
    rng = random.Random(seed)
    model: Model = {}
    next_id = 1
    adds = 0
    for step in range(STEPS):
        now = int(gw.clock.now().timestamp())
        action = rng.choice(["add", "add", "supersede", "delete", "delete", "advance"])
        if not model and action in ("supersede", "delete"):
            action = "add"
        if adds >= MAX_ADDS and action in ("add", "supersede"):
            action = "delete" if model else "advance"
        if action in ("add", "supersede"):
            repo = rng.choice(REPOS)
            life = rng.choice(LIFETIMES)
            argv = ["--expires", f"{life}s"]
            target = None
            if action == "supersede":
                free = [i for i, n in model.items() if n["by"] is None]
                if free:
                    target = rng.choice(free)
                    repo = model[target]["repo"]
                    argv += ["--supersedes", str(target)]
            code, out, err = add(gw, *argv, body=f"body {step}\n", repo=repo, topic="t")
            assert code == 0, (seed, step, out, err)
            adds += 1
            expires = now + life
            if target is not None:
                expires = max(expires, model[target]["expires"])
                model[target]["by"] = next_id
            assert json.loads(out)["id"] == next_id
            model[next_id] = {"repo": repo, "expires": expires, "by": None}
            next_id += 1
        elif action == "advance":
            gw.clock.advance(timedelta(seconds=rng.randrange(0, 400)))
        else:
            count = min(len(model), rng.choice([1, 1, 2, 3]))
            ids = rng.sample(sorted(model), count)
            before = len(log_records(gw))
            reinstated, via = model_delete(model, set(ids))
            argv = ["delete", "--yes", "--json", *(str(i) for i in rng.sample(ids, count))]
            assert run(gw, *argv) == 0, (seed, step, gw.terminal.stderr)
            printed = json.loads(gw.terminal.stdout)
            assert printed == {"deleted": sorted(ids), "reinstated": reinstated}
            fresh = log_records(gw)[before:]
            assert [r["note_id"] for r in fresh] == sorted(ids)
            assert all(r["event"] == "delete" for r in fresh)
            by_deleted = {d: p for p, d in via.items()}
            assert [r["reinstated"] for r in fresh] == [by_deleted.get(i) for i in sorted(ids)]
        now = int(gw.clock.now().timestamp())
        assert table(gw) == expected(model, now), (seed, step, action)
