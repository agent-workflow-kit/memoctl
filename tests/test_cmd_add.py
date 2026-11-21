"""``memoctl add``: the 14 ordered steps, end to end through ``cli.main`` and a fake gateway.

Every test runs on a private home (the fake gateway's) and never touches the real one. Secret
looking fixtures are assembled from parts at run time.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memoctl import cmd_add
from memoctl import store as store_mod
from memoctl.cli import main
from memoctl.gateway import RealGateway
from memoctl.logfile import LogError
from memoctl.statedir import open_state_dir
from memoctl.store import Mode, NoteRow, open_store
from tests.fake_gateway import EPOCH, FakeExit, FakeGateway
from tests.golden_files import check_golden
from tests.schema_check import validate_json_output

DAY = 86400
REPO = "example.com/team/app"


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "gw")


def state_dir(gw: FakeGateway) -> Path:
    return gw.home() / ".local" / "state" / "memoctl"


def write_config(gw: FakeGateway, text: str) -> None:
    directory = gw.home() / ".config" / "memoctl"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    (directory / "config.yaml").write_text(text)
    (directory / "config.yaml").chmod(0o600)


def add(
    gw: FakeGateway,
    *argv: str,
    body: str | bytes | None = "a note body\n",
    json_mode: bool = True,
    repo: str | None = REPO,
    topic: str | None = "build",
    body_file: str | None = "-",
) -> tuple[int, str, str]:
    """Run ``memoctl add`` with the body on stdin; the exit code, stdout and stderr."""
    args = ["add"]
    if repo is not None:
        args += ["--repo", repo]
    if topic is not None:
        args += ["--topic", topic]
    if body_file is not None:
        args += ["--body-file", body_file]
    args += list(argv)
    if json_mode:
        args.append("--json")
    if body is not None:
        gw.terminal.stdin = body
    gw.terminal.stdout = ""
    gw.terminal.stderr = ""
    code = main(args, gw)
    return code, gw.terminal.stdout, gw.terminal.stderr


def stored(gw: FakeGateway) -> list[NoteRow]:
    """Every note in the store (none when there is no database)."""
    state = open_state_dir(gw, create=False)
    if state is None:
        return []
    with state:
        store = open_store(state, Mode.READ)
        if store is None:
            return []
        try:
            return list(store.notes(labels=True))
        finally:
            store.close()


def log_records(gw: FakeGateway) -> list[dict[str, Any]]:
    path = state_dir(gw) / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every file and directory under ``root`` with its mode and content hash."""
    out: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        )
        out[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), digest)
    return out


def error_of(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    validate_json_output("cli/error", out.encode())
    return doc["error"]  # type: ignore[no-any-return]


# -- the plain path ----------------------------------------------------------------------------


def test_add_creates_a_note_and_prints_the_documented_json(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--label", "ticket=ab-12", "--label", "area=ci", "--kind", "implement")
    assert (code, err) == (0, "")
    check_golden("add/created.json", out.encode(), schema="cli/add")
    doc = json.loads(out)
    assert doc["id"] == 1 and doc["created"] is True and doc["dry_run"] is False
    assert doc["labels"] == ["area=ci", "ticket=ab-12"]  # sorted by key, then value
    expected = EPOCH + timedelta(days=30)
    assert doc["expires"] == expected.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert doc["size"] == len(b"a note body\n")
    assert doc["body_sha256"] == hashlib.sha256(b"a note body\n").hexdigest()
    assert doc["warnings"] == []  # implement is on the default allow list


def test_add_prints_the_human_line_and_nothing_on_stderr(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--kind", "implement", json_mode=False)
    assert (code, err) == (0, "")
    check_golden("add/created.txt", out.encode())


def test_quiet_suppresses_the_result_line_but_not_json(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--quiet", json_mode=False)
    assert (code, out, err) == (0, "", "")
    assert len(stored(gw)) == 1
    code, out, _ = add(gw, "--quiet")
    assert code == 0 and json.loads(out)["id"] == 2


def test_the_note_is_stored_as_written_with_its_labels_and_dates(gw: FakeGateway) -> None:
    add(gw, "--label", "b=2", "--label", "a=1", "--label", "a=1", body="line one\nline two")
    [row] = stored(gw)
    assert (row.repo, row.topic, row.kind, row.body) == (
        REPO,
        "build",
        "unknown",
        "line one\nline two",
    )
    assert row.labels == (("a", "1"), ("b", "2"))  # identical labels collapse
    assert row.created == int(EPOCH.timestamp())
    assert row.expires == row.created + 30 * DAY
    assert row.superseded_by is None


def test_the_body_is_never_rewritten(gw: FakeGateway) -> None:
    for body in ("a", "a\n", "  indented\n\n", "tab\tseparated", "\n\n text \n"):
        add(gw, body=body)
    assert [r.body for r in stored(gw)] == [
        "a",
        "a\n",
        "  indented\n\n",
        "tab\tseparated",
        "\n\n text \n",
    ]


def test_a_body_file_path_is_read_and_a_file_named_dash_needs_dot_slash(gw: FakeGateway) -> None:
    (gw.root / "note.txt").write_text("from a file")
    (gw.root / "-").write_text("file named dash")
    assert add(gw, body=None, body_file="note.txt")[0] == 0
    assert add(gw, body=None, body_file="./-")[0] == 0
    assert [r.body for r in stored(gw)] == ["from a file", "file named dash"]
    assert gw.stdin_reads == 0


def test_the_log_gets_one_record_after_the_commit(gw: FakeGateway) -> None:
    add(gw, "--kind", "audit", "--actor", "auto:ci-bot")
    [record] = log_records(gw)
    assert record["event"] == "add" and record["note_id"] == 1
    assert (record["repo"], record["topic"], record["kind"]) == (REPO, "build", "audit")
    assert record["body_sha256"] == hashlib.sha256(b"a note body\n").hexdigest()[:16]
    assert (record["actor"], record["actor_source"]) == ("auto:ci-bot", "flag")
    assert record["supersedes"] is None and record["reinstated"] is None
    assert record["ts"].startswith("2026-01-01T00:00:00.000Z")
    assert "a note body" not in json.dumps(record)


def test_two_runs_on_the_same_inputs_are_byte_identical(tmp_path: Path) -> None:
    outs: list[tuple[str, list[NoteRow], list[dict[str, Any]]]] = []
    for name in ("one", "two"):
        g = FakeGateway(tmp_path / name)
        code, out, _ = add(g, "--label", "k=v", "--kind", "implement")
        assert code == 0
        outs.append((out, stored(g), log_records(g)))
    assert outs[0] == outs[1]


def test_ids_increase_and_the_two_notes_are_independent_rows(gw: FakeGateway) -> None:
    assert json.loads(add(gw)[1])["id"] == 1
    assert json.loads(add(gw, body="other")[1])["id"] == 2


# -- clock -------------------------------------------------------------------------------------


def test_the_clock_is_read_exactly_once(gw: FakeGateway) -> None:
    add(gw)
    assert gw.now_calls == 1


def test_created_expires_and_log_time_come_from_the_same_reading(gw: FakeGateway) -> None:
    gw.clock.set(datetime(2030, 5, 6, 7, 8, 9, 999_999, tzinfo=UTC))  # 0.5 us under 10 s
    add(gw)
    [row] = stored(gw)
    assert row.created == int(datetime(2030, 5, 6, 7, 8, 9, tzinfo=UTC).timestamp())  # floored
    assert log_records(gw)[0]["ts"] == "2030-05-06T07:08:09.999Z"


def test_a_clock_before_the_epoch_is_internal_and_writes_nothing(gw: FakeGateway) -> None:
    gw.clock.set(datetime(1969, 12, 31, tzinfo=UTC))
    code, out, err = add(gw)
    assert code == 1 and error_of(out)["code"] == "internal"
    assert err == "internal: internal error\n"
    assert not state_dir(gw).exists()


# -- expires and lifetimes -----------------------------------------------------------------------


def test_expires_inside_the_cap_is_used_and_not_warned(gw: FakeGateway) -> None:
    code, out, _ = add(gw, "--expires", "2h", "--kind", "implement")
    doc = json.loads(out)
    assert code == 0 and doc["expires"] == "2026-01-01T02:00:00Z"
    assert doc["warnings"] == []


def test_expires_over_the_cap_is_cut_with_a_warning(gw: FakeGateway) -> None:
    doc = json.loads(add(gw, "--expires", "31d", "--kind", "implement")[1])
    assert doc["expires"] == "2026-01-31T00:00:00Z"
    assert doc["warnings"] == ["expires_capped"]


def test_expires_equal_to_the_cap_is_not_a_warning(gw: FakeGateway) -> None:
    assert json.loads(add(gw, "--expires", "30d", "--kind", "implement")[1])["warnings"] == []


@pytest.mark.parametrize("bad", ["59s", "0", "", "1w", "-5m", " 1h", "1h\n", "1e3", "11111111111"])
def test_a_bad_expires_is_usage(gw: FakeGateway, bad: str) -> None:
    code, out, _ = add(gw, f"--expires={bad}")
    assert code == 2 and error_of(out)["details"] == {"field": "expires"}
    assert not state_dir(gw).exists()


def test_the_repo_override_of_retention_changes_the_cap(gw: FakeGateway) -> None:
    write_config(gw, f"repos:\n  {REPO}:\n    retention:\n      max_age: 2d\n")
    doc = json.loads(add(gw, "--kind", "implement")[1])
    assert doc["expires"] == "2026-01-03T00:00:00Z"
    other = json.loads(add(gw, "--kind", "implement", repo="example.com/other/app")[1])
    assert other["expires"] == "2026-01-31T00:00:00Z"


# -- kind ----------------------------------------------------------------------------------------


def test_kind_comes_from_the_flag_then_the_variable_then_unknown(gw: FakeGateway) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    assert json.loads(add(gw)[1])["kind"] == "audit"
    assert json.loads(add(gw, "--kind", "implement", body="b")[1])["kind"] == "implement"
    del gw.environ["CSESSION_KIND"]
    assert json.loads(add(gw, body="c")[1])["kind"] == "unknown"


@pytest.mark.parametrize("value", ["Bad Kind", "x" * 5000, "", "kind\n", "\udcff", "a;b"])
def test_a_malformed_kind_variable_becomes_unknown_and_is_never_echoed(
    gw: FakeGateway, value: str
) -> None:
    gw.environ["CSESSION_KIND"] = value
    code, out, err = add(gw)
    assert code == 0 and json.loads(out)["kind"] == "unknown"
    assert value[:8] not in out + err or value == ""


def test_the_kind_variable_name_comes_from_config(gw: FakeGateway) -> None:
    write_config(gw, "kind_env: MY_KIND\n")
    gw.environ["CSESSION_KIND"] = "audit"
    gw.environ["MY_KIND"] = "draft-ticket"
    assert json.loads(add(gw)[1])["kind"] == "draft-ticket"


def test_kind_not_shown_follows_the_effective_author_kinds(gw: FakeGateway) -> None:
    assert json.loads(add(gw, "--kind", "implement")[1])["warnings"] == []
    assert json.loads(add(gw, "--kind", "other", body="b")[1])["warnings"] == ["kind_not_shown"]
    assert json.loads(add(gw, body="c")[1])["warnings"] == ["kind_not_shown"]  # unknown
    write_config(gw, f"repos:\n  {REPO}:\n    context:\n      author_kinds: [other]\n")
    assert json.loads(add(gw, "--kind", "other", body="d")[1])["warnings"] == []


@pytest.mark.parametrize("bad", ["Upper", "-lead", "a b", "x" * 65, "", "ḱ", "\udcff"])
def test_a_bad_kind_flag_is_usage_and_not_rescued_by_the_variable(
    gw: FakeGateway, bad: str
) -> None:
    gw.environ["CSESSION_KIND"] = "audit"
    code, out, _ = add(gw, f"--kind={bad}")
    assert code == 2 and error_of(out)["details"] == {"field": "kind"}


# -- actor ---------------------------------------------------------------------------------------


def test_actor_default_is_auto_unknown_without_a_terminal(gw: FakeGateway) -> None:
    add(gw)
    assert (log_records(gw)[0]["actor"], log_records(gw)[0]["actor_source"]) == (
        "auto:unknown",
        "default",
    )


def test_actor_default_on_a_terminal_is_the_login_name(gw: FakeGateway) -> None:
    gw.terminal.tty[0] = True
    gw.login = "we ird/user"
    (gw.root / "note.txt").write_text("body")
    assert add(gw, body=None, body_file="note.txt")[0] == 0
    record = log_records(gw)[0]
    assert (record["actor"], record["actor_source"]) == ("human:we_ird_user", "tty")


@pytest.mark.parametrize("bad", ["", "bot", "human:", "robot:x", "auto:a b", "auto:" + "x" * 65])
def test_a_bad_actor_is_usage_before_anything_else(gw: FakeGateway, bad: str) -> None:
    code, out, _ = add(gw, f"--actor={bad}", repo="Bad Repo")
    assert code == 2 and error_of(out)["details"] == {"field": "actor"}
    assert gw.stdin_reads == 0 and not state_dir(gw).exists()


# -- labels --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["novalue", "=x", "Key=v", "k=v w", "k=" + "v" * 257, "k=\x00", "a\nb=c"]
)
def test_a_bad_label_is_usage_label(gw: FakeGateway, bad: str) -> None:
    code, out, _ = add(gw, f"--label={bad}")
    assert code == 2 and error_of(out)["details"] == {"field": "label"}


def test_label_count_is_checked_against_the_global_limit_with_the_limit_in_details(
    gw: FakeGateway,
) -> None:
    write_config(gw, "limits:\n  max_labels: 2\n")
    code, out, _ = add(gw, "--label", "a=1", "--label", "b=1", "--label", "c=1")
    assert code == 2 and error_of(out)["details"] == {"field": "label", "limit": 2}
    assert gw.stdin_reads == 0
    assert add(gw, "--label", "a=1", "--label", "a=1", "--label", "b=1")[0] == 0  # collapsed: 2


def test_zero_max_labels_refuses_any_label(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  max_labels: 0\n")
    assert add(gw, "--label", "a=1")[0] == 2
    assert add(gw)[0] == 0


# -- body ----------------------------------------------------------------------------------------


def test_body_over_the_limit_is_usage_with_the_limit(gw: FakeGateway) -> None:
    code, out, _ = add(gw, body="x" * 8193)
    assert code == 2 and error_of(out)["details"] == {"field": "body", "limit": 8192}
    assert add(gw, body="x" * 8192)[0] == 0  # exactly the limit is accepted


def test_the_repo_override_changes_the_body_limit(gw: FakeGateway) -> None:
    write_config(gw, f"repos:\n  {REPO}:\n    limits:\n      max_body_bytes: 300\n")
    assert add(gw, body="x" * 301)[0] == 2
    assert add(gw, body="x" * 9000, repo="example.com/other/app")[0] == 2
    assert add(gw, body="x" * 300)[0] == 0


@pytest.mark.parametrize(
    ("body", "cls", "line"),
    [
        (b"ok\n\xff\xfe", "encoding", None),
        ("one\ntwo\x00", "control", 2),
        ("a‮b", "invisible", 1),
        ("a", "private_use", 1),
        ("a͸", "unassigned", 1),
        ("e" + "́" * 31, "combining", 1),
    ],
)
def test_refused_text_is_usage_with_class_and_line(
    gw: FakeGateway, body: str | bytes, cls: str, line: int | None
) -> None:
    code, out, _ = add(gw, body=body)
    details = error_of(out)["details"]
    assert code == 2 and details["field"] == "body" and details["class"] == cls
    if line is not None:
        assert details["line"] == line
    assert not state_dir(gw).exists()


@pytest.mark.parametrize("body", ["", " ", "\n\n\t ", " "])
def test_an_empty_or_whitespace_body_is_usage_body(gw: FakeGateway, body: str) -> None:
    code, out, _ = add(gw, body=body)
    assert code == 2 and error_of(out)["details"] == {"field": "body"}


def test_stdin_a_terminal_or_unreadable_is_usage_body(gw: FakeGateway) -> None:
    gw.terminal.tty[0] = True
    assert error_of(add(gw)[1])["details"] == {"field": "body"}
    gw.terminal.tty[0] = False
    gw.terminal.stdin_error = "unreadable"
    code, out, _ = add(gw)
    assert code == 2 and error_of(out)["details"] == {"field": "body"}


def test_a_symlink_a_fifo_a_directory_and_a_missing_file_are_usage_body(gw: FakeGateway) -> None:
    (gw.root / "real.txt").write_text("hello")
    (gw.root / "link.txt").symlink_to("real.txt")
    os.mkfifo(gw.root / "pipe")
    (gw.root / "dir").mkdir()
    for name in ("link.txt", "pipe", "dir", "missing.txt", ""):
        code, out, _ = add(gw, body=None, body_file=name)
        assert code == 2 and error_of(out)["details"] == {"field": "body"}, name
    assert stored(gw) == []


def test_a_non_utf8_argument_is_usage_and_never_echoed(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--label", "k=\udcff")
    assert code == 2 and error_of(out)["details"] == {"field": "label"}
    code, out, err = add(gw, repo="ex\udcffample.com/a")
    assert code == 2 and error_of(out)["details"] == {"field": "repo"}
    assert "\udcff" not in out + err and "ex" not in err


# -- grammar of every argv field -------------------------------------------------------------------


HOSTILE = ["a\x00b", "a\nb", "a‮b", "A", "a b", "é", "x" * 100_000, "\udcff", ""]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_repo_topic_are_usage_with_their_own_field(gw: FakeGateway, value: str) -> None:
    code, out, _ = add(gw, repo=value if value else "")
    assert code == 2 and error_of(out)["details"] == {"field": "repo"}
    code, out, _ = add(gw, topic=value)
    assert code == 2 and error_of(out)["details"] == {"field": "topic"}
    assert not state_dir(gw).exists()


@pytest.mark.parametrize(
    "repo", ["a" * 256 + "/x", "example.com", "example.com/..", "Example.com/a"]
)
def test_a_bad_repo_is_usage_repo(gw: FakeGateway, repo: str) -> None:
    code, out, _ = add(gw, repo=repo)
    assert code == 2 and error_of(out)["details"] == {"field": "repo"}


@pytest.mark.parametrize("bad", ["0", "01", "-1", "1.5", "x", "1" * 19, " 1", "١"])
def test_a_bad_supersedes_id_is_usage_supersedes(gw: FakeGateway, bad: str) -> None:
    code, out, _ = add(gw, f"--supersedes={bad}")
    assert code == 2 and error_of(out)["details"] == {"field": "supersedes"}


def test_a_missing_required_flag_is_usage_argv(gw: FakeGateway) -> None:
    for kwargs in ({"repo": None}, {"topic": None}, {"body_file": None}):
        code, out, _ = add(gw, **kwargs)  # type: ignore[arg-type]
        assert code == 2 and error_of(out)["details"] == {"field": "argv"}


def test_flags_before_the_subcommand_and_after_double_dash_are_usage_argv(gw: FakeGateway) -> None:
    for argv in (
        ["--json", "add", "--repo", REPO, "--topic", "t", "--body-file", "-"],
        ["add", "--", "--repo", REPO, "--topic", "t", "--body-file", "-", "--json"],
        ["add", "--repo", REPO, "--topic", "t", "--body-file", "-", "--", "--json"],
        ["add", "--repo", REPO, "--topic", "t", "--body-file", "-", "extra", "--json"],
        ["add", "--rep", REPO, "--topic", "t", "--body-file", "-", "--json"],
    ):
        gw.terminal.stdout = gw.terminal.stderr = ""
        assert main(argv, gw) == 2, argv
        assert gw.terminal.stderr == "usage: invalid argv\n"
        if "--json" in argv[: argv.index("--")] if "--" in argv else True:
            assert error_of(gw.terminal.stdout)["details"] == {"field": "argv"}
        else:  # the --json pre-scan stops at "--": the status line only
            assert gw.terminal.stdout == ""
    assert stored(gw) == [] and gw.stdin_reads == 0


def test_dry_run_is_not_yet_accepted_so_a_note_is_never_written_by_mistake(
    gw: FakeGateway,
) -> None:
    # TODO(#19): replaced by the dry-run tests when --dry-run lands.
    code, out, _ = add(gw, "--dry-run")
    assert code == 2 and error_of(out)["details"] == {"field": "argv"}
    assert stored(gw) == []


def test_supersedes_is_refused_not_ignored_until_it_is_built(gw: FakeGateway) -> None:
    # TODO(#17): replaced by the supersede tests; the flag must never be silently ignored.
    code, out, _ = add(gw, "--supersedes", "5")
    assert code == 1 and error_of(out)["code"] == "internal"
    assert stored(gw) == []


# -- secrets ---------------------------------------------------------------------------------------


def _token() -> str:
    return "gh" + "p_" + "A1" * 18  # assembled at run time


def _check_secret(gw: FakeGateway, field: str, **kwargs: Any) -> None:
    code, out, err = add(gw, **kwargs)
    error = error_of(out)
    assert code == 20 and error["reason"] == "secret_detected"
    assert error["details"] == {"class": "github_token", "field": field}
    assert _token() not in out + err
    assert stored(gw) == [] and not state_dir(gw).exists()


def test_a_secret_in_the_body_is_refused(gw: FakeGateway) -> None:
    _check_secret(gw, "body", body=f"token {_token()} here")


def test_a_secret_in_a_label_is_refused(gw: FakeGateway) -> None:
    code, out, _ = add(gw, f"--label=k={_token()}")
    assert code == 20 and error_of(out)["details"] == {"class": "github_token", "field": "label"}
    assert not state_dir(gw).exists()


def test_a_secret_in_the_kind_from_the_variable_is_refused_by_field_kind(
    gw: FakeGateway,
) -> None:
    write_config(gw, "kind_env: MY_KIND\nmask_env: [MY_SECRET]\n")
    gw.environ["MY_SECRET"] = "ok-kind-value"
    gw.environ["MY_KIND"] = "ok-kind-value"
    code, out, _ = add(gw)
    assert code == 20 and error_of(out)["details"] == {"class": "mask_env", "field": "kind"}


def test_a_secret_in_the_kind_flag_repo_and_topic(gw: FakeGateway) -> None:
    write_config(gw, "mask_env: [MY_SECRET]\n")
    gw.environ["MY_SECRET"] = "example.com/secret/repo"
    code, out, _ = add(gw, repo="example.com/secret/repo")
    assert code == 20 and error_of(out)["details"] == {"class": "mask_env", "field": "repo"}
    gw.environ["MY_SECRET"] = "secret-topic-1"
    code, out, _ = add(gw, topic="secret-topic-1")
    assert error_of(out)["details"] == {"class": "mask_env", "field": "topic"}
    gw.environ["MY_SECRET"] = "secret-kind-1"
    code, out, _ = add(gw, "--kind", "secret-kind-1")
    assert error_of(out)["details"] == {"class": "mask_env", "field": "kind"}
    assert stored(gw) == []


def test_the_masked_value_is_never_printed_or_logged(gw: FakeGateway) -> None:
    write_config(gw, "mask_env: [MY_SECRET]\n")
    gw.environ["MY_SECRET"] = "very-secret-value"
    code, out, err = add(gw, body="has very-secret-value inside")
    assert code == 20 and "very-secret-value" not in out + err
    assert log_records(gw) == []


def test_a_secret_split_by_an_invisible_character_is_refused_as_text_first(
    gw: FakeGateway,
) -> None:
    code, out, _ = add(gw, body="gh​p_" + "A1" * 18)
    assert code == 2 and error_of(out)["details"]["class"] == "invisible"


# -- config ----------------------------------------------------------------------------------------


def test_a_config_error_wins_and_never_consumes_stdin(gw: FakeGateway) -> None:
    write_config(gw, "limits:\n  max_body_bytes: 1\n")
    code, out, _ = add(gw, body="x" * 20)
    error = error_of(out)
    assert code == 2 and error["code"] == "config_invalid"
    assert error["details"] == {"field": "limits.max_body_bytes"}
    assert gw.stdin_reads == 0 and gw.now_calls == 0


def test_an_unknown_repo_override_key_is_a_config_error(gw: FakeGateway) -> None:
    write_config(gw, f"repos:\n  {REPO}:\n    kind_env: X\n")
    code, out, _ = add(gw)
    assert code == 2 and error_of(out)["code"] == "config_invalid"


# -- state -----------------------------------------------------------------------------------------


def test_the_state_directory_and_database_are_created_private(gw: FakeGateway) -> None:
    add(gw)
    sd = state_dir(gw)
    assert stat.S_IMODE(sd.stat().st_mode) == 0o700
    assert stat.S_IMODE((sd / "state.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((sd / "log.jsonl").stat().st_mode) == 0o600


def test_an_unsafe_state_directory_is_refused_20_and_nothing_is_touched(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True)
    sd.chmod(0o777)
    before = snapshot(gw.home())
    code, out, err = add(gw)
    error = error_of(out)
    assert (code, error["reason"], error["details"]) == (20, "unsafe_state", None)
    assert err == "refused (unsafe_state): state directory is not safe to use\n"
    assert snapshot(gw.home()) == before


def test_a_state_directory_that_cannot_be_created_is_exit_12_not_internal(gw: FakeGateway) -> None:
    gw.home().chmod(0o500)
    try:
        code, out, _ = add(gw)
    finally:
        gw.home().chmod(0o700)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    error = error_of(out)
    assert (code, error["code"], error["reason"]) == (12, "failed", "db_unreadable")


def test_a_database_of_a_newer_schema_is_exit_12_and_unchanged(gw: FakeGateway) -> None:
    add(gw)
    db = state_dir(gw) / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    before = db.read_bytes()
    code, out, _ = add(gw, body="again")
    error = error_of(out)
    assert (code, error["reason"], error["details"]) == (
        12,
        "schema_newer",
        {"found": 99, "supported": 1},
    )
    assert db.read_bytes() == before and len(log_records(gw)) == 1


def test_a_foreign_database_is_exit_12_db_corrupt(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True)
    sd.chmod(0o700)
    conn = sqlite3.connect(sd / "state.db")
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    (sd / "state.db").chmod(0o600)
    code, out, _ = add(gw)
    assert code == 12 and error_of(out)["reason"] == "db_corrupt"
    assert log_records(gw) == []


def test_the_id_ceiling_in_the_sequence_is_db_corrupt_and_adds_nothing(gw: FakeGateway) -> None:
    add(gw)
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    conn.execute("UPDATE sqlite_sequence SET seq = 999999999999999999 WHERE name = 'notes'")
    conn.commit()
    conn.close()
    code, out, _ = add(gw, body="more")
    assert code == 12 and error_of(out)["reason"] == "db_corrupt"
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    conn.close()
    assert len(log_records(gw)) == 1


def test_a_locked_database_is_exit_12_db_locked_and_retryable(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(gw)
    monkeypatch.setattr(store_mod, "WRITE_BUSY_MS", 30)
    blocker = sqlite3.connect(state_dir(gw) / "state.db", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        code, out, _ = add(gw, body="blocked")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    error = error_of(out)
    assert (code, error["reason"], error["retryable"]) == (12, "db_locked", True)
    assert len(log_records(gw)) == 1
    assert add(gw, body="after")[0] == 0


# -- failure after the commit ----------------------------------------------------------------------


def test_a_log_failure_after_the_commit_warns_and_keeps_exit_0(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise LogError("write_failed")

    monkeypatch.setattr(cmd_add, "append_record", broken)
    code, out, err = add(gw)
    assert code == 0 and json.loads(out)["id"] == 1
    assert err == f"{cmd_add.LOG_FAILED_WARNING}\n"
    assert len(stored(gw)) == 1


def test_an_os_error_from_the_log_is_the_same_warning(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cmd_add, "append_record", broken)
    code, out, err = add(gw, json_mode=False)
    assert (code, out) == (0, "note 1 created\n") and "No space" not in err
    assert err == f"{cmd_add.LOG_FAILED_WARNING}\n"


def test_a_log_file_that_is_not_a_regular_file_warns_without_failing(gw: FakeGateway) -> None:
    sd = state_dir(gw)
    sd.mkdir(parents=True)
    sd.chmod(0o700)
    (sd / "log.jsonl").mkdir()
    code, out, err = add(gw)
    assert code == 0 and len(stored(gw)) == 1
    assert err == f"{cmd_add.LOG_FAILED_WARNING}\n"


def test_a_failed_insert_leaves_no_note_label_rate_row_or_log_record(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(gw)
    original = store_mod.Store.insert_note

    def failing(self: Any, **kwargs: Any) -> int:
        original(self, **kwargs)  # the rows are written, then the disk "fills up"
        raise store_mod.StoreError("db_unreadable")

    monkeypatch.setattr(store_mod.Store, "insert_note", failing)
    code, out, _ = add(gw, "--label", "a=1", body="second")
    assert (code, error_of(out)["reason"]) == (12, "db_unreadable")
    monkeypatch.undo()
    assert [r.body for r in stored(gw)] == ["a note body\n"]
    assert len(log_records(gw)) == 1
    conn = sqlite3.connect(state_dir(gw) / "state.db")
    assert conn.execute("SELECT count(*) FROM rate").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM note_labels").fetchone()[0] == 0
    conn.close()


# -- the deadline ------------------------------------------------------------


STEPS = [
    "step1_parse", "step2_load_config", "step3_check_label_count", "step4_resolve_kind",
    "step5_read_body", "step6_secret_check", "step7_read_clock", "step8_open_state_dir",
    "step9_open_store", "step10_dedupe", "step11_supersede", "step12_limits", "step13_insert",
]  # fmt: skip


@pytest.mark.parametrize("step", STEPS)
def test_a_deadline_that_passes_in_a_step_exits_12_and_leaves_no_note(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    original = getattr(cmd_add, step)

    def slow(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        gw.advance_mono(31)  # the fake timer fires its handler inline
        return result

    monkeypatch.setattr(cmd_add, step, slow)
    with pytest.raises(FakeExit) as info:
        add(gw)
    assert info.value.code == 12
    assert gw.terminal.stdout.count("\n") == 1  # the one pre-built timeout envelope
    assert error_of(gw.terminal.stdout)["reason"] == "timeout"
    assert gw.terminal.stderr == "failed (timeout): deadline passed\n"
    monkeypatch.undo()
    assert stored(gw) == [] and log_records(gw) == []


def test_the_deadline_is_disarmed_before_the_first_byte_of_output(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real_write = gw.write_out

    def write_out(text: str, *, bound: float | None = None) -> None:
        seen.append(len(gw.alarms))
        real_write(text, bound=bound)

    monkeypatch.setattr(gw, "write_out", write_out)
    assert add(gw)[0] == 0
    assert seen == [0]


def test_a_deadline_that_passed_during_the_commit_exits_12_with_the_note_stored_and_no_log(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cmd_add.step13_insert

    def mark(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        gw.alarms[0].fired = True  # the timer runs out while the commit runs
        return result

    monkeypatch.setattr(cmd_add, "step13_insert", mark)
    with pytest.raises(FakeExit) as info:
        add(gw)
    assert info.value.code == 12
    assert error_of(gw.terminal.stdout)["reason"] == "timeout"
    monkeypatch.undo()
    assert len(stored(gw)) == 1  # the change is durable
    assert log_records(gw) == []  # and, per the spec, has no record


def test_a_retry_after_that_timeout_is_not_blocked_by_the_stored_note(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TODO(#17): the retry returns the stored note with created: false; until dedupe exists
    the retry stores a second note."""
    original = cmd_add.step13_insert
    monkeypatch.setattr(
        cmd_add, "step13_insert",
        lambda *a, **k: (setattr(gw.alarms[0], "fired", True), original(*a, **k))[1],
    )  # fmt: skip
    with pytest.raises(FakeExit):
        add(gw)
    monkeypatch.undo()
    assert add(gw)[0] == 0


# -- the order of the steps ------------------------------------------------------------------------


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
    argv += ["--label", f"k={_token()}"]


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
]


def _argv() -> list[str]:
    return ["add", "--repo", REPO, "--topic", "build", "--body-file", "-", "--json"]


@pytest.mark.parametrize(("first", "second"), list(itertools.combinations(SCENARIOS, 2)),
                         ids=lambda s: s[0])  # fmt: skip
def test_of_two_failing_steps_the_earlier_wins_and_nothing_was_written(
    gw: FakeGateway, first: Scenario, second: Scenario
) -> None:
    argv = _argv()
    gw.terminal.stdin = "a body"
    for _, setup, _ in (second, first):  # order of setup must not matter
        setup(gw, argv)
    before = snapshot(gw.home())
    gw.terminal.stdout = ""
    code = main(argv, gw)
    error = error_of(gw.terminal.stdout)
    assert first[2](code, error), (first[0], second[0], code, error)
    assert gw.now_calls <= 1
    if int(first[0]) < 5:
        assert gw.stdin_reads == 0
    if int(first[0]) < 7:
        assert gw.now_calls == 0
    if int(first[0]) < 8:
        assert snapshot(gw.home()) == before  # not even the state directory
    assert log_records(gw) == []
    if not {"8", "9"} & {first[0], second[0]}:  # an unsafe or newer store cannot be read back
        assert stored(gw) == []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s[0])
def test_each_failing_step_alone_reports_itself(gw: FakeGateway, scenario: Scenario) -> None:
    argv = _argv()
    gw.terminal.stdin = "a body"
    scenario[1](gw, argv)
    gw.terminal.stdout = ""
    code = main(argv, gw)
    assert scenario[2](code, error_of(gw.terminal.stdout))
    assert log_records(gw) == []


# -- errors never leak ------------------------------------------------------------


def test_status_lines_carry_no_input_value(gw: FakeGateway) -> None:
    code, out, err = add(gw, "--label=k=" + _token(), repo=REPO)
    assert code == 20
    for value in (_token(), REPO, "build"):
        assert value not in out + err


def test_a_secret_refusal_golden(gw: FakeGateway) -> None:
    code, out, err = add(gw, body=f"x {_token()}")
    assert code == 20
    check_golden("add/secret-refused.json", out.encode(), schema="cli/error")
    check_golden("add/secret-refused-err.txt", err.encode())


# -- through the real gateway (real files, real clock, real log) --------------------------------


class RealHome(RealGateway):
    """The real gateway with a private home (never the password-database one)."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._h = home

    def home(self) -> Path:
        return self._h


def _real(tmp_path: Path) -> tuple[RealHome, Path]:
    home = tmp_path / "realhome"
    home.mkdir()
    return RealHome(home), home


def test_add_end_to_end_on_the_real_gateway(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    gateway, home = _real(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("real body\n")
    code = main(
        ["add", "--repo", REPO, "--topic", "t", "--body-file", str(body), "--json"], gateway
    )
    out = capfd.readouterr().out
    assert code == 0
    doc = validate_json_output("cli/add", out.encode())
    assert doc["id"] == 1 and doc["created"] is True
    state = home / ".local/state/memoctl"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    [record] = [json.loads(line) for line in (state / "log.jsonl").read_text().splitlines()]
    assert record["note_id"] == 1 and record["event"] == "add"
    # the real clock: the stored times are whole seconds of the same reading
    conn = sqlite3.connect(state / "state.db")
    created, expires = conn.execute("SELECT created, expires FROM notes").fetchone()
    conn.close()
    assert expires - created == 30 * DAY and abs(created - datetime.now(UTC).timestamp()) < 60


def test_the_real_gateway_refuses_a_symlink_and_a_fifo_body_and_writes_nothing(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    gateway, home = _real(tmp_path)
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    os.mkfifo(tmp_path / "fifo")
    for name in ("link.txt", "fifo"):
        argv = ["add", "--repo", REPO, "--topic", "t", "--body-file", str(tmp_path / name)]
        assert main([*argv, "--json"], gateway) == 2
        assert error_of(capfd.readouterr().out)["details"] == {"field": "body"}
    assert not (home / ".local").exists()


def test_a_body_that_grows_while_it_is_read_is_cut_at_the_limit(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """An endless-looking source (a very large file) never costs more than the limit."""
    gateway, home = _real(tmp_path)
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 5_000_000)
    argv = ["add", "--repo", REPO, "--topic", "t", "--body-file", str(big), "--json"]
    assert main(argv, gateway) == 2
    assert error_of(capfd.readouterr().out)["details"] == {"field": "body", "limit": 8192}
    assert not (home / ".local").exists()


def test_an_unexpected_error_with_a_secret_in_a_local_is_internal_and_prints_nothing_of_it(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        leaked = _token()  # noqa: F841 - a local a traceback with locals would print
        raise RuntimeError(f"unexpected {_token()}")

    monkeypatch.setattr(cmd_add, "step13_insert", boom)
    code, out, err = add(gw, body=f"{_token()[:5]} body")
    assert code == 1 and error_of(out)["code"] == "internal"
    assert _token() not in out + err
    monkeypatch.undo()
    assert stored(gw) == [] and log_records(gw) == []
