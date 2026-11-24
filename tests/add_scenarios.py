"""Shared fixtures of the ``memoctl add`` tests: the failing-step scenarios of the order tests
(used by ``test_cmd_add`` and ``test_cmd_add_dry_run``), the config writer and the run-time
assembled token. Secret looking values are assembled at run time."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memoctl.statedir import open_state_dir
from memoctl.store import Mode, open_store
from tests.fake_gateway import FakeGateway

REPO = "example.com/team/app"


def state_dir(gw: FakeGateway) -> Path:
    return gw.home() / ".local" / "state" / "memoctl"


def write_config(gw: FakeGateway, text: str) -> None:
    directory = gw.home() / ".config" / "memoctl"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    (directory / "config.yaml").write_text(text)
    (directory / "config.yaml").chmod(0o600)


def token() -> str:
    return "gh" + "p_" + "A1" * 18  # assembled at run time


def _s1(gw: FakeGateway, argv: list[str]) -> None:
    argv[argv.index(REPO)] = "Bad_Repo"


def _s2(gw: FakeGateway, argv: list[str]) -> None:
    write_config(gw, "limits:\n  max_body_bytes: 1\n")


def _s3(gw: FakeGateway, argv: list[str]) -> None:
    for i in range(9):
        argv += ["--label", f"k{i}=v"]


def _s5(gw: FakeGateway, argv: list[str]) -> None:
    gw.terminal.stdin = "x" * 9000


def _s6(gw: FakeGateway, argv: list[str]) -> None:
    argv += ["--label", f"k={token()}"]


def _s7(gw: FakeGateway, argv: list[str]) -> None:
    gw.clock.set(datetime(1969, 1, 1, tzinfo=UTC))


def _s8(gw: FakeGateway, argv: list[str]) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True, exist_ok=True)
    sd.chmod(0o777)


def _s9(gw: FakeGateway, argv: list[str]) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sd / "state.db")
    conn.execute("PRAGMA application_id = 1835363695")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    (sd / "state.db").chmod(0o600)
    sd.chmod(0o700)


def _s9_locked(gw: FakeGateway, argv: list[str]) -> None:
    state = open_state_dir(gw, create=True)
    with state:
        store = open_store(state, Mode.INIT)
        assert store is not None
        store.close()
    blocker = sqlite3.connect(state_dir(gw) / "state.db", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    gw.blocker = blocker  # type: ignore[attr-defined]


def _s9_corrupt(gw: FakeGateway, argv: list[str]) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sd / "state.db")
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    (sd / "state.db").chmod(0o600)
    sd.chmod(0o700)


def _seed_corrupt_target(gw: FakeGateway, argv: list[str]) -> None:
    """Step 10: ``--supersedes 1`` names a row that fails its row checks (db_corrupt); the one
    failure of the dedupe step that does not depend on a step before it."""
    state = open_state_dir(gw, create=True)
    with state:
        store = open_store(state, Mode.INIT)
        assert store is not None
        with store.write():
            store.insert_note(
                repo=REPO, topic="build", kind="unknown", body="x", body_sha256="0" * 64,
                size=1, created=1, expires=2, labels=(),
            )  # fmt: skip
        store.close()
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    conn.execute("UPDATE notes SET expires = -5")
    conn.commit()
    conn.close()
    argv += ["--supersedes", "1"]


def _s11(gw: FakeGateway, argv: list[str]) -> None:
    argv += ["--supersedes", "5"]  # no such note


def _seed_target(gw: FakeGateway, repo: str, superseded_by: int | None) -> None:
    state = open_state_dir(gw, create=True)
    with state:
        store = open_store(state, Mode.INIT)
        assert store is not None
        with store.write():
            store.insert_note(
                repo=repo, topic="build", kind="unknown", body="x", body_sha256="0" * 64,
                size=1, created=1, expires=2, labels=(),
            )  # fmt: skip
        store.close()
    if superseded_by is not None:
        conn = sqlite3.connect(state_dir(gw) / "state.db")
        conn.execute("UPDATE notes SET superseded_by = ?", (superseded_by,))
        conn.commit()
        conn.close()


def _s11_mismatch(gw: FakeGateway, argv: list[str]) -> None:
    _seed_target(gw, "example.com/team/other", None)
    argv += ["--supersedes", "1"]


def _s11_superseded(gw: FakeGateway, argv: list[str]) -> None:
    _seed_target(gw, REPO, 77)
    argv += ["--supersedes", "1"]


Scenario = tuple[
    str, Callable[[FakeGateway, list[str]], None], Callable[[int, dict[str, Any]], bool]
]
SCENARIOS: list[Scenario] = [
    ("1", _s1, lambda c, e: c == 2 and e["details"] == {"field": "repo"}),
    ("2", _s2, lambda c, e: c == 2 and e["code"] == "config_invalid"),
    ("3", _s3, lambda c, e: c == 2 and e["details"] == {"field": "label", "limit": 8}),
    ("5", _s5, lambda c, e: c == 2 and e["details"] == {"field": "body", "limit": 8192}),
    ("6", _s6, lambda c, e: c == 20 and e["reason"] == "secret_detected"),
    ("7", _s7, lambda c, e: c == 1 and e["code"] == "internal"),
    ("8", _s8, lambda c, e: c == 20 and e["reason"] == "unsafe_state"),
    ("9", _s9, lambda c, e: c == 12 and e["reason"] == "schema_newer"),
    ("9l", _s9_locked, lambda c, e: c == 12 and e["reason"] == "db_locked"),
    ("9c", _s9_corrupt, lambda c, e: c == 12 and e["reason"] == "db_corrupt"),
    ("10", _seed_corrupt_target, lambda c, e: c == 12 and e["reason"] == "db_corrupt"),
    ("11", _s11, lambda c, e: c == 10 and e["details"] == {"id": 5}),
    ("11m", _s11_mismatch, lambda c, e: c == 20 and e["reason"] == "supersede_mismatch"),
    ("11s", _s11_superseded, lambda c, e: c == 20 and e["reason"] == "already_superseded"),
]


def step_of(scenario: Scenario) -> int:
    return int(scenario[0].rstrip("lcms"))  # "9l", "9c", "11m", "11s" share their step


def release(gw: FakeGateway) -> None:
    blocker = getattr(gw, "blocker", None)
    if blocker is not None:
        blocker.execute("ROLLBACK")
        blocker.close()
