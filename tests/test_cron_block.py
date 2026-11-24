"""The managed block as pure functions on bytes (``memoctl.cron_block``)."""

from __future__ import annotations

import pytest

from memoctl.cron_block import (
    BlockMalformedError,
    block_bytes,
    entry_line,
    home_is_safe,
    parse,
    plan_install,
    plan_uninstall,
)

HOME = "/home/example"
BLOCK = block_bytes(HOME)
START = b"# memoctl managed\n"
END = b"# memoctl managed end\n"
OTHER = b"MAILTO=me@example.com\n0 * * * * /usr/bin/true\n"


def test_block_is_exactly_three_lf_lines() -> None:
    assert BLOCK == (
        b"# memoctl managed\n"
        b"17 3 * * * PATH=/home/example/.local/bin:/usr/local/bin:/usr/bin:/bin"
        b" memoctl prune --quiet --actor auto:memoctl-cron\n"
        b"# memoctl managed end\n"
    )


@pytest.mark.parametrize(
    "home",
    ["/home/a", "/", "/home/a.b_c-d@e+f", "/Users/Jo/x/y"],
)
def test_safe_homes(home: str) -> None:
    assert home_is_safe(home)


@pytest.mark.parametrize(
    "home",
    [
        "", "relative", "/home/a b", "/home/a%b", "/home/a\n", "/home/a\nb", "/home/$x",
        "/home/`x`", "/home/a'b", '/home/a"b', "/home/a;b", "/home/a:b", "/home/é",
        "/home/a\\b", "/home/a\x00", "/home/a#b", "/home/a\udc80", "/home/a\t",
    ],
)  # fmt: skip
def test_unsafe_homes_are_refused_and_never_written(home: str) -> None:
    assert not home_is_safe(home)
    with pytest.raises(ValueError):
        entry_line(home)


def test_install_into_nothing_and_empty() -> None:
    for table in (None, b""):
        assert plan_install(table, HOME) == ("installed", BLOCK, [*BLOCK.split(b"\n")[:3]])


def test_install_appends_after_a_missing_trailing_lf_only() -> None:
    assert plan_install(b"0 * * * * x", HOME)[1] == b"0 * * * * x\n" + BLOCK
    assert plan_install(b"0 * * * * x\n", HOME)[1] == b"0 * * * * x\n" + BLOCK
    assert plan_install(b"\n", HOME)[1] == b"\n" + BLOCK  # a blank line is kept, not doubled


def test_install_twice_is_unchanged_and_writes_nothing() -> None:
    first = plan_install(OTHER, HOME)[1]
    assert first is not None
    outcome, new, block = plan_install(first, HOME)
    assert (outcome, new) == ("unchanged", None)
    assert block == BLOCK.split(b"\n")[:3]


def test_replace_keeps_everything_outside_byte_identical() -> None:
    before = b"\xff\x00 raw\r\n" + START + b"1 1 * * * old\n" + END + b"after\r\nlast"
    outcome, new, _ = plan_install(before, HOME)
    assert outcome == "replaced"
    assert new == b"\xff\x00 raw\r\n" + BLOCK + b"after\r\nlast"


def test_replace_a_block_at_eof_without_lf() -> None:
    outcome, new, _ = plan_install(START + b"x\n" + END[:-1], HOME)
    assert (outcome, new) == ("replaced", BLOCK)


def test_unchanged_block_at_eof_without_lf_is_not_rewritten() -> None:
    table = BLOCK[:-1]
    assert plan_install(table, HOME)[:2] == ("unchanged", None)


def test_a_different_home_replaces() -> None:
    old = block_bytes("/home/other")
    outcome, new, _ = plan_install(old, HOME)
    assert (outcome, new) == ("replaced", BLOCK)


def test_uninstall_leaves_outside_byte_identical_and_reports_the_block_as_found() -> None:
    before = OTHER + START + b"hand edited \xff\n" + END + b"tail"
    outcome, new, block = plan_uninstall(before)
    assert outcome == "removed"
    assert new == OTHER + b"tail"
    assert block == [b"# memoctl managed", b"hand edited \xff", b"# memoctl managed end"]


def test_uninstall_only_block_writes_empty_table() -> None:
    assert plan_uninstall(BLOCK)[:2] == ("removed", b"")


@pytest.mark.parametrize("table", [None, b"", OTHER, b"no lf"])
def test_uninstall_absent_is_unchanged(table: bytes | None) -> None:
    assert plan_uninstall(table) == ("unchanged", None, [])


def test_install_then_uninstall_restores_the_original_bytes() -> None:
    for original in (b"", OTHER, OTHER + b"\n\n", b"\xff\x00\r\n"):
        installed = plan_install(original, HOME)[1]
        assert installed is not None
        assert plan_uninstall(installed)[1] == original


@pytest.mark.parametrize(
    "table",
    [
        START,  # a start without an end
        END,  # an end without a start
        START + b"x\n" + END + START + b"x\n" + END,  # two blocks
        START + START + b"x\n" + END,  # two starts
        START + b"x\n" + END + END,  # two ends
        END + b"x\n" + START,  # reversed
        START + END,  # nothing in between
        START + b"a\nb\n" + END,  # too long
        START + b"x\n" + b"y\n" + END,
        b"# memoctl managed \n" + b"x\n" + END,  # trailing space
        b"# memoctl managed\r\n" + b"x\n" + END,  # CR
        START + b"x\n" + b"# memoctl managed end\r\n",
        b"#memoctl managed\nx\n" + END,  # spacing
        b"  # memoctl managed\nx\n" + END,  # indent
        b"# memoctl  managed\nx\n" + END,
        b"#\tmemoctl managed\nx\n" + END,
        START + b"x\n" + b"# memoctl managed end \n",
        BLOCK + b"# memoctl managed\n",  # a stray lookalike after a good block
        b"# memoctl managed \n",  # a lookalike alone
    ],
)
def test_malformed_blocks_are_refused_for_both_commands(table: bytes) -> None:
    with pytest.raises(BlockMalformedError):
        parse(table)
    with pytest.raises(BlockMalformedError):
        plan_install(table, HOME)
    with pytest.raises(BlockMalformedError):
        plan_uninstall(table)


@pytest.mark.parametrize(
    "table",
    [
        b"# memoctl managed by hand\n",
        b"# memoctl\n",
        b"FOO=# memoctl managed\n",
        b"0 0 * * * echo # memoctl managed\n",
        b"# Memoctl Managed\n",
        b"# memoctl managed endpoint\n",
        b"@reboot echo hi\n",
        b"\n\n\n",
        b"# memoctl managed\x00\n",
    ],
)
def test_lookalikes_that_are_not_markers_are_ordinary_lines(table: bytes) -> None:
    assert parse(table).start is None
    outcome, new, _ = plan_install(table, HOME)
    assert outcome == "installed"
    assert new == table + BLOCK


def test_lines_split_on_lf_only() -> None:
    # CR, FF and VT are not line ends: this is one line that merely contains them.
    table = b"a\rb\x0cc\x0bd\n"
    assert parse(table).lines == [table]
    assert plan_install(table, HOME)[1] == table + BLOCK


def test_huge_inputs_are_linear_and_intact() -> None:
    big = b"x" * 5_000_000 + b"\n" + b"0 * * * * y\n" * 50_000
    new = plan_install(big, HOME)[1]
    assert new == big + BLOCK
    assert plan_uninstall(new)[1] == big
    huge_line = START + b"z" * 2_000_000 + b"\n" + END
    assert plan_uninstall(huge_line)[1] == b""
