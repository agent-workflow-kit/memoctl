"""Terminal-safe rendering: hostile text, properties over every code point, goldens."""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from memoctl.render import (
    Rendered,
    emit,
    escape,
    escape_field,
    format_add,
    format_add_dry_run,
    format_cron,
    format_delete,
    format_list,
    format_note_line,
    format_prune,
    format_show,
    format_validate,
)
from tests.fake_gateway import FakeGateway
from tests.golden_files import check_golden

# The refused set, written out from interface.md (Body rules item 3) and independent of the
# implementation: categories plus the listed default-ignorables, plus surrogates.
LISTED = (
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x180E))
    | {0x180F}
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)


def spec_unsafe(code: int) -> bool:
    if code in (0x0A, 0x09):
        return False
    c = chr(code)
    return (
        code in LISTED
        or code in (0x2028, 0x2029)
        or unicodedata.category(c) in ("Cc", "Cf", "Co", "Cn", "Cs")
    )


def decode(text: str) -> str:
    return re.sub(r"\\u\{([0-9A-F]{4,6})\}", lambda m: chr(int(m.group(1), 16)), text)


def safe_output(text: str, *, allow_lf: bool) -> bool:
    for ch in text:
        if ch == "\n":
            if not allow_lf:
                return False
        elif ch != " " and spec_unsafe(ord(ch)):
            return False
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    return True


HOSTILE = [
    "\x1b[2J\x1b[H",  # CSI clear screen
    "\x1b]0;title\x07",  # OSC window title
    "\x1b]52;c;ZXZpbA==\x07",  # OSC 52 clipboard
    "\x1b]8;;http://example.invalid\x1b\\link\x1b]8;;\x1b\\",  # OSC 8 hyperlink
    "\x1bPq#0\x1b\\",  # DCS
    "\x1b_Gi=1\x1b\\",  # APC
    "\x1bXsos\x1b\\\x1b^pm\x1b\\",  # SOS, PM
    "\x9b2J\x9d0;t\x9c\x90x\x9c",  # C1 forms of CSI, OSC, ST, DCS
    "\x00nul\x7fdel\x08\x08back",
    "fake\roverwrite",
    "a\u202eb\u2066c\u2069d\u200bz\u200d\ufeff",  # bidi, zero width, BOM
    "\U000e0041\U000e007f",  # tag characters
    "x" + "\u0301" * 500,  # combining flood (not refused at render, harmless)
    "lone\ud800surrogate\udfff",
    "a\u2028b\u2029c\x0bd\x0ce\x85f",  # other line breaks
    "\U0010ffff\ufdd0\U000f0000",  # noncharacter, unassigned, private use
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_is_safe_in_both_forms(text: str) -> None:
    assert safe_output(escape(text), allow_lf=True)
    assert safe_output(escape_field(text), allow_lf=False)


def test_esc_is_visible_and_reversible() -> None:
    assert escape("\x1b[31m") == "\\u{001B}[31m"
    assert decode(escape("\x1b[31m\u202e")) == "\x1b[31m\u202e"


def test_escape_format_is_uppercase_hex_at_least_four_digits() -> None:
    assert escape("\U000e0041") == "\\u{E0041}"
    assert escape("\x7f") == "\\u{007F}"
    assert escape("\u00ad") == "\\u{00AD}"
    assert escape("\ud800") == "\\u{D800}"


def test_lf_kept_tab_to_four_spaces_in_multiline_text() -> None:
    assert escape("a\tb\nc") == "a    b\nc"


def test_field_form_escapes_lf_and_tab() -> None:
    assert escape_field("a\nb\tc") == "a\\u{000A}b\\u{0009}c"


def test_cr_and_backspace_cannot_overwrite() -> None:
    out = escape("safe\rEVIL\b\b\b")
    assert "\r" not in out and "\b" not in out


def test_ordinary_text_is_untouched() -> None:
    text = "Grüße, 日本語, emoji \U0001f600, é (NFD) é\u00a0nbsp ~ !"
    assert escape(text) == text
    assert escape(text) == escape_field(text)


def test_stored_text_is_not_normalized_or_lt_rewritten() -> None:
    assert escape("<b>\ufb01") == "<b>\ufb01"


def test_every_code_point_matches_the_spec_set() -> None:
    bad = []
    for code in range(0x110000):
        out = escape_field(chr(code))
        expect_escape = code in (0x0A, 0x09) or spec_unsafe(code) or code < 0x20 or code == 0x7F
        if expect_escape != (out != chr(code)):
            bad.append(hex(code))
        if expect_escape and out != f"\\u{{{code:04X}}}":
            bad.append(hex(code))
    assert not bad[:10]


def test_output_over_all_code_points_has_no_unsafe_character() -> None:
    everything = "".join(map(chr, range(0x110000)))
    assert safe_output(escape(everything), allow_lf=True)
    assert safe_output(escape_field(everything), allow_lf=False)


def test_idempotent() -> None:
    for text in [*HOSTILE, "".join(map(chr, range(0x3000)))]:
        once = escape(text)
        assert escape(once) == once
        once_f = escape_field(text)
        assert escape_field(once_f) == once_f


def test_concatenation_and_cutting_cannot_create_danger() -> None:
    for a, b in [("\x1b", "[2J"), ("\u202e", "x"), ("a\x1b", "]52;c;")]:
        assert escape(a) + escape(b) == escape(a + b)
    text = "ok\x1b]52;c;eA==\x07tail"
    for cut in range(len(text) + 1):
        assert safe_output(escape(text[:cut]), allow_lf=True)
        assert safe_output(escape(text[cut:]), allow_lf=True)


def test_huge_inputs_are_linear() -> None:
    start = time.monotonic()
    out = escape("\x1b" * 1_000_000 + "\u0301" * 1_000_000 + "\n".join(["x" * 1000] * 1000))
    assert out.count("\\u{001B}") == 1_000_000
    assert time.monotonic() - start < 5


def test_very_long_line_stays_one_line() -> None:
    assert "\n" not in escape_field("a" * 100_000 + "\x1b")


# --- human output shapes ---------------------------------------------------------------------


def note(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "repo": "owner/repo",
        "topic": "queue",
        "kind": "unknown",
        "labels": ["a=b", "c=d"],
        "created": "2026-01-02T03:04:05Z",
        "expires": "2026-02-01T03:04:05Z",
        "status": "live",
        "superseded_by": None,
        "supersedes": None,
        "size": 11,
        "body": "hello\n\nworld\n",
        "body_sha256": "0" * 64,
    }
    base.update(over)
    return base


def test_note_line_shape() -> None:
    assert format_note_line(note()) == "7  live  2026-01-02  owner/repo  queue  unknown  a=b,c=d"


def test_note_line_missing_labels_is_dash() -> None:
    assert format_note_line(note(labels=[])).endswith("  unknown  -")


def test_hostile_fields_cannot_forge_lines_or_columns() -> None:
    line = format_note_line(note(topic="x\n99  live  fake\x1b[2J", kind="a\tb", labels=["\r=x"]))
    assert "\n" not in line and "\x1b" not in line and "\t" not in line and "\r" not in line
    assert safe_output(line, allow_lf=False)


def test_list_footers() -> None:
    r = format_list([note()], total=3, shown=1, hidden=2)
    assert r.err == "1 of 3 shown\n2 superseded or expired notes hidden; use --all\n"
    assert format_list([note()], 1, 1, 0).err == ""


def test_list_empty() -> None:
    assert format_list([], 0, 0, 0) == Rendered("", "no notes\n")


def test_show_prefixes_every_body_line_and_marks_trailing_lf() -> None:
    out = format_show(note()).out.split("\n")
    assert out[out.index("body:") + 1 :] == ["| hello", "|", "| world", "|", ""]


def test_show_hostile_body_cannot_forge_structure() -> None:
    out = format_show(note(body="x\nbody:\n\x1b]52;c;eA==\x07\r| fake\u2028status: live")).out
    assert safe_output(out, allow_lf=True)
    for line in out.split("\n")[out.split("\n").index("body:") + 1 : -1]:
        assert line == "|" or line.startswith("| ")


def test_show_missing_values_are_dash() -> None:
    out = format_show(note()).out
    assert "superseded_by: -\n" in out and "supersedes: -\n" in out


def test_status_lines() -> None:
    assert format_add(3, created=True).out == "note 3 created\n"
    assert format_add(3, created=False).out == "note 3 exists\n"
    assert format_add_dry_run().out.startswith("dry run: valid;")
    assert format_prune({"expired": 2, "remaining": 5, "dry_run": False}).out == (
        "pruned 2 expired notes, 5 remain\n"
    )
    assert format_prune({"expired": 2, "remaining": 5, "dry_run": True}).out == (
        "dry run: would have pruned 2 expired notes, 5 would remain\n"
    )
    busy = format_prune({"outcome": "busy"})
    assert busy.out == "prune skipped: store busy\n" and busy.err.startswith("warning (prune_busy)")
    assert format_cron("installed", dry_run=False).out == "cron block installed\n"
    assert format_cron("installed", dry_run=True).out == "dry run: cron block would be installed\n"
    assert format_delete([1, 2], {2: [(5, False), (6, True)]}).out == (
        "deleted note 1\ndeleted note 2; reinstated note 5; reinstated note 6 (expired)\n"
    )


def test_validate_lines() -> None:
    doc = {
        "ok": False,
        "checks": [
            {"name": "config", "ok": True, "reason": None, "message": "fine"},
            {"name": "state_dir", "ok": False, "reason": "unsafe_state", "message": "bad"},
        ],
        "warnings": ["unsafe_filesystem"],
    }
    assert format_validate(doc).out == (
        "ok  config  fine\nFAIL  state_dir  bad\nwarning  unsafe_filesystem\n"
    )


def test_emit_goes_through_the_gateway(tmp_path: Path) -> None:
    gw = FakeGateway(tmp_path)
    emit(gw, Rendered("out\n", "err\n"))
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("out\n", "err\n")


# --- goldens (bytes-exact) -------------------------------------------------------------------


def _bytes(r: Rendered) -> bytes:
    return r.out.encode("utf-8")


def test_golden_list() -> None:
    notes = [
        note(id=9, topic="t\x1b[2Jx", labels=["k=v"]),
        note(id=8, status="superseded", repo="a/b", kind="reviewer", labels=[]),
    ]
    check_golden("render/list-hostile.txt", _bytes(format_list(notes, 3, 2, 1)))


def test_golden_show_hostile() -> None:
    body = "plain\n\x1b]0;t\x07 title\n\u202eevil\u202c\ttab\r\n\ud800\n\n"
    n = note(body=body, supersedes=3, labels=["a=b\x1b"])
    check_golden("render/show-hostile.txt", _bytes(format_show(n)))


def test_golden_show_plain() -> None:
    check_golden("render/show-plain.txt", _bytes(format_show(note())))


def test_golden_validate() -> None:
    doc = {
        "ok": True,
        "checks": [{"name": "config", "ok": True, "reason": None, "message": "valid"}],
        "warnings": [],
    }
    check_golden("render/validate-ok.txt", _bytes(format_validate(doc)))
