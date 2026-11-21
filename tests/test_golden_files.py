"""The golden-file harness: bytes-exact comparison, explicit regeneration, no escapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.golden_files import (
    GOLDEN_ROOT,
    UPDATE_ENV,
    GoldenError,
    check_golden,
    golden_path,
    normalize,
)

ERR_ENVELOPE = (
    b'{"error": {"code": "usage", "message": "m", "reason": null,'
    b' "details": {"field": "limit"}, "retryable": false}}\n'
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "golden"
    r.mkdir()
    return r


def test_the_repository_root_is_tests_golden() -> None:
    assert GOLDEN_ROOT == Path(__file__).parent / "golden"


# --- naming convention -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["add/ok.json", "context/cut-line.txt", "a/b/c_d-1.bin"])
def test_conforming_names_resolve_below_the_root(name: str, root: Path) -> None:
    p = golden_path(name, root)
    assert root.resolve() in p.parents


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ok.json",  # needs an area directory
        "../x/ok.json",
        "/abs/ok.json",
        "add/../ok.json",
        "add//ok.json",
        "add/ok",  # no extension
        "add/ok.yaml",
        "add/OK.json",
        "add/ok.tar.json",
        "add/.json",
        "add/ok.json\n",
        "add/ok\x00.json",
        "add/ok.json/",
        "ad d/ok.json",
        "add/ök.json",
        "add\\ok.json",
    ],
)
def test_bad_names_are_refused(name: str, root: Path) -> None:
    with pytest.raises(GoldenError, match="golden name"):
        golden_path(name, root)


def test_a_symlinked_directory_cannot_leave_the_root(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GoldenError, match="outside"):
        check_golden("evil/x.txt", b"x", root=root, environ={UPDATE_ENV: "1"})
    assert list(outside.iterdir()) == []


# --- comparison -------------------------------------------------------------------------------


def _seed(root: Path, name: str, data: bytes) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_equal_bytes_pass(root: Path) -> None:
    _seed(root, "t/a.txt", b"hello\n")
    check_golden("t/a.txt", b"hello\n", root=root, environ={})


def test_a_missing_golden_fails_and_is_not_created(root: Path) -> None:
    with pytest.raises(GoldenError, match="missing"):
        check_golden("t/a.txt", b"x", root=root, environ={})
    assert not (root / "t").exists()


@pytest.mark.parametrize(
    ("golden", "actual"),
    [
        (b"a\n", b"a"),  # trailing newline dropped
        (b"a", b"a\n"),  # trailing newline added
        (b"a\n", b"a\r\n"),  # CRLF
        (b"a\nb\n", b"a\r\nb\r\n"),
        (b"a\0b", b"a\0c"),  # NUL
        (b"a\0b", b"ab"),
        (b"\xff\xfe", b"\xff\xfd"),  # not UTF-8
        (b"\xc3\xa9", b"e\xcc\x81"),  # NFC versus NFD
        (b"", b"\n"),
    ],
)
def test_every_byte_difference_fails(root: Path, golden: bytes, actual: bytes) -> None:
    _seed(root, "t/a.bin", golden)
    with pytest.raises(GoldenError):
        check_golden("t/a.bin", actual, root=root, environ={})


def test_non_utf8_bytes_compare_equal_when_identical(root: Path) -> None:
    data = b"\xff\xfe\x00\r\n\x80"
    _seed(root, "t/a.bin", data)
    check_golden("t/a.bin", data, root=root, environ={})


def test_the_failure_shows_a_diff_with_visible_control_characters(root: Path) -> None:
    _seed(root, "t/a.txt", b"one\ntwo\n")
    with pytest.raises(GoldenError) as e:
        check_golden("t/a.txt", b"one\r\ntwo\0\n", root=root, environ={})
    text = str(e.value)
    assert "t/a.txt" in text
    assert "-b'one\\n'" in text
    assert "+b'one\\r\\n'" in text
    assert "+b'two\\x00\\n'" in text
    assert UPDATE_ENV in text


def test_the_golden_must_be_a_regular_file(root: Path) -> None:
    (root / "t").mkdir()
    (root / "t" / "a.txt").mkdir()
    with pytest.raises(GoldenError, match="regular file"):
        check_golden("t/a.txt", b"x", root=root, environ={})


def test_a_symlinked_golden_file_is_refused(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"x")
    (root / "t").mkdir()
    (root / "t" / "a.txt").symlink_to(target)
    with pytest.raises(GoldenError, match="symlink|regular file"):
        check_golden("t/a.txt", b"x", root=root, environ={})


def test_actual_must_be_bytes(root: Path) -> None:
    with pytest.raises(TypeError):
        check_golden("t/a.txt", "text", root=root, environ={})  # type: ignore[arg-type]


# --- regeneration -----------------------------------------------------------------------------


def test_regeneration_needs_the_explicit_variable(root: Path) -> None:
    _seed(root, "t/a.txt", b"old")
    for env in ({}, {UPDATE_ENV: ""}, {UPDATE_ENV: "0"}, {UPDATE_ENV: "yes"}, {"CI": "1"}):
        with pytest.raises(GoldenError):
            check_golden("t/a.txt", b"new", root=root, environ=env)
    assert (root / "t" / "a.txt").read_bytes() == b"old"


def test_regeneration_writes_then_passes(root: Path) -> None:
    check_golden("t/new.txt", b"fresh\n", root=root, environ={UPDATE_ENV: "1"})
    assert (root / "t" / "new.txt").read_bytes() == b"fresh\n"
    check_golden("t/new.txt", b"fresh\n", root=root, environ={})


def test_regeneration_replaces_a_stale_golden(root: Path) -> None:
    _seed(root, "t/a.txt", b"old")
    check_golden("t/a.txt", b"new", root=root, environ={UPDATE_ENV: "1"})
    assert (root / "t" / "a.txt").read_bytes() == b"new"


def test_regeneration_keeps_exact_bytes(root: Path) -> None:
    data = b"\xff\r\n\0 end"
    check_golden("t/a.bin", data, root=root, environ={UPDATE_ENV: "1"})
    assert (root / "t" / "a.bin").read_bytes() == data


@pytest.mark.parametrize("ci", ["CI", "GITHUB_ACTIONS", "BUILD_NUMBER", "TF_BUILD"])
def test_regeneration_is_refused_in_ci(root: Path, ci: str) -> None:
    with pytest.raises(GoldenError, match="CI"):
        check_golden("t/a.txt", b"x", root=root, environ={UPDATE_ENV: "1", ci: "true"})
    assert not (root / "t").exists()


def test_regeneration_leaves_no_temporary_files(root: Path) -> None:
    check_golden("t/a.txt", b"x", root=root, environ={UPDATE_ENV: "1"})
    assert [p.name for p in (root / "t").iterdir()] == ["a.txt"]


# --- JSON goldens are always validated --------------------------------------------------------


def test_a_json_golden_needs_a_schema(root: Path) -> None:
    _seed(root, "t/a.json", ERR_ENVELOPE)
    with pytest.raises(GoldenError, match="schema"):
        check_golden("t/a.json", ERR_ENVELOPE, root=root, environ={})


def test_a_valid_json_golden_passes(root: Path) -> None:
    _seed(root, "t/a.json", ERR_ENVELOPE)
    check_golden("t/a.json", ERR_ENVELOPE, schema="cli/error", root=root, environ={})


def test_an_invalid_json_output_fails_even_when_it_equals_the_golden(root: Path) -> None:
    bad = b'{"error": {}}\n'
    _seed(root, "t/a.json", bad)
    with pytest.raises(AssertionError, match="error"):
        check_golden("t/a.json", bad, schema="cli/error", root=root, environ={})


def test_regeneration_never_writes_an_invalid_json_golden(root: Path) -> None:
    with pytest.raises(AssertionError):
        check_golden("t/a.json", b"{}", schema="cli/error", root=root, environ={UPDATE_ENV: "1"})
    assert not (root / "t").exists()


def test_a_schema_is_refused_for_a_non_json_golden(root: Path) -> None:
    with pytest.raises(GoldenError, match="schema"):
        check_golden("t/a.txt", b"x", schema="cli/error", root=root, environ={UPDATE_ENV: "1"})


# --- volatile values --------------------------------------------------------------------------


def test_normalize_replaces_paths_longest_first() -> None:
    out = normalize(b"/t/home/.local and /t/home/x and /t", paths={"/t": "<T>", "/t/home": "<H>"})
    assert out == b"<H>/.local and <H>/x and <T>"


def test_normalize_replaces_timestamps_and_pids() -> None:
    raw = (
        b'{"ts": "2026-11-21T10:11:12.345Z", "t2": "2026-11-21T10:11:12Z", "pid": 4242, "n": 14242}'
    )
    out = normalize(raw, pids=[4242])
    assert out == b'{"ts": "<TS>", "t2": "<TS>", "pid": <PID>, "n": 14242}'


def test_normalize_is_bytes_safe_and_idempotent() -> None:
    raw = b"\xff /t \0\r\n"
    once = normalize(raw, paths={"/t": "<T>"})
    assert once == b"\xff <T> \0\r\n"
    assert normalize(once, paths={"/t": "<T>"}) == once


def test_normalize_ignores_an_empty_path_key() -> None:
    assert normalize(b"abc", paths={"": "<X>"}) == b"abc"


def test_a_golden_with_a_volatile_path_is_refused(root: Path, tmp_path: Path) -> None:
    leaked = f"home is {Path.home()}/x\n".encode()
    with pytest.raises(GoldenError, match="volatile"):
        check_golden("t/a.txt", leaked, root=root, environ={UPDATE_ENV: "1"})
    with pytest.raises(GoldenError, match="volatile"):
        check_golden("t/a.txt", b"/tmp/pytest-of-x/y", root=root, environ={UPDATE_ENV: "1"})
    assert not (root / "t").exists()


# --- the committed goldens --------------------------------------------------------------------


def test_the_committed_harness_golden_round_trips() -> None:
    check_golden("harness/envelope.json", ERR_ENVELOPE, schema="cli/error")


def test_no_committed_golden_name_breaks_the_convention() -> None:
    for p in sorted(GOLDEN_ROOT.rglob("*")):
        if p.is_file():
            assert not p.is_symlink()
            assert golden_path(p.relative_to(GOLDEN_ROOT).as_posix()) == p.resolve()
