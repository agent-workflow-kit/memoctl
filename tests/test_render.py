"""Terminal-safe rendering: hostile text, properties over every code point, goldens."""

from __future__ import annotations

import random
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
from tests.schema_check import validate

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


def test_every_code_point_matches_the_spec_set_and_the_output_is_safe() -> None:
    """One pass over every code point (a space excluded: its escape depends on its
    neighbours): the exact escape of each, which is stronger than a safety scan."""
    chars, expected_field, expected_body = [], [], []
    for code in range(0x110000):
        if code == 0x20:
            continue
        char = chr(code)
        chars.append(char)
        if code in (0x0A, 0x09) or spec_unsafe(code) or code < 0x20 or code == 0x7F:
            esc = f"\\u{{{code:04X}}}"
            expected_field.append(esc)
            expected_body.append(char if code == 0x0A else "    " if code == 0x09 else esc)
        else:
            expected_field.append(char)
            expected_body.append(char)
    everything = "".join(chars)
    field, body = escape_field(everything), escape(everything)
    assert field == "".join(expected_field)
    assert body == "".join(expected_body)


def test_idempotent() -> None:
    for text in [*HOSTILE, "".join(map(chr, range(0x3000)))]:
        once = escape(text)
        assert escape(once) == once
        once_f = escape_field(text)
        assert escape_field(once_f) == once_f


def test_concatenation_and_cutting_cannot_create_danger() -> None:
    rng = random.Random(20261121)
    pieces = [*HOSTILE, "plain", " ", "\n", "\t", "\x1b", "[2J", "\u202e", "\u0301"]
    for _ in range(400):
        a, b = rng.choice(pieces), rng.choice(pieces)
        assert escape(a) + escape(b) == escape(a + b)
        assert safe_output(escape_field(a) + escape_field(b), allow_lf=False)
        text = a + b + rng.choice(pieces)
        cut = rng.randrange(len(text) + 1)
        for piece in (text[:cut], text[cut:]):
            assert safe_output(escape(piece), allow_lf=True)
            assert safe_output(escape_field(piece), allow_lf=False)
    for a, b in [("\x1b", "[2J"), ("\u202e", "x"), ("a\x1b", "]52;c;")]:
        assert escape(a) + escape(b) == escape(a + b)


def test_huge_inputs_are_linear() -> None:
    out = escape("\x1b" * 1_000_000 + "\u0301" * 1_000_000 + "\n".join(["x" * 1000] * 1000))
    assert out.count("\\u{001B}") == 1_000_000
    assert len(out) == 10_000_999
    start = time.monotonic()
    escape_field(" " * 1_000_000 + "\x1b" * 100_000)
    assert time.monotonic() - start < 30  # generous: guards against quadratic, not speed


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
    assert busy.out == "prune skipped: store busy\n"
    assert busy.warnings.startswith("warning (prune_busy)")
    assert format_cron("installed", dry_run=False).out == "cron block installed\n"
    assert format_cron("installed", dry_run=True).out == "dry run: cron block would be installed\n"
    assert format_delete([1, 2], {2: (6, True)}).out == (
        "deleted note 1\ndeleted note 2; reinstated note 6 (expired)\n"
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


def test_space_runs_and_edge_spaces_are_escaped_in_fields() -> None:
    assert escape_field("a  b") == "a\\u{0020}\\u{0020}b"
    assert escape_field(" a b ") == "\\u{0020}a b\\u{0020}"
    assert escape_field("a b") == "a b"
    assert escape_field(" ") == "\\u{0020}"
    assert escape_field("a    b") == "a" + "\\u{0020}" * 4 + "b"


def test_a_field_cannot_forge_columns_with_spaces() -> None:
    line = format_note_line(note(topic="x  live  2026-01-01  evil"))
    assert line.count("  ") == 6  # exactly the six separators of seven columns
    assert "x\\u{0020}\\u{0020}live" in line


def test_a_field_with_a_tab_cannot_forge_columns() -> None:
    assert "  " not in format_note_line(note(topic="a\t\tb")).replace("  ", "", 6)


def test_separator_inside_a_label_is_escaped() -> None:
    assert format_note_line(note(labels=["a=b,c", "d=e"])).endswith("  a=b\\u{002C}c,d=e")
    shown = format_show(note(labels=["a=b,c", "d=e"])).out
    assert "labels: a=b\\u{002C}c, d=e\n" in shown


def test_empty_string_values_render_as_dash() -> None:
    line = format_note_line(note(created="", topic="", kind=""))
    assert line == "7  live  -  owner/repo  -  -  a=b,c=d"
    assert "topic: -\n" in format_show(note(topic="", expires="")).out
    assert "expires: -\n" in format_show(note(expires="")).out


def test_non_str_values_are_coerced_without_exception_or_leak() -> None:
    n = note(created=20260102, labels=[1, None, b"\x1b"], body=12345, topic=3.5)
    line = format_note_line(n)
    shown = format_show(n).out
    assert line.startswith("7  live  20260102  ")
    assert "\x1b" not in line + shown and safe_output(shown, allow_lf=True)
    assert "| 12345\n" in shown
    assert format_show(note(body=None)).out.endswith("body:\n|\n")


def test_a_bare_string_labels_value_is_one_label() -> None:
    assert format_note_line(note(labels="ab")).endswith("  ab")
    assert "labels: ab\n" in format_show(note(labels="ab")).out
    assert format_note_line(note(labels=None)).endswith("  -")


def test_hostile_created_is_escaped() -> None:
    line = format_note_line(note(created="\x1b[2Jabcdefgh"))
    assert "\x1b" not in line and safe_output(line, allow_lf=False)
    assert "\\u{001B}[2Jabcd" not in line or "  \\u{001B}" in line  # cut after ten characters
    assert safe_output(format_show(note(created="\x1b]0;t\x07")).out, allow_lf=True)


def test_hostile_cron_outcome_is_escaped() -> None:
    for dry in (False, True):
        out = format_cron("in\x1b[2J\nstalled  x", dry_run=dry).out
        assert safe_output(out, allow_lf=True) and out.count("\n") == 1
    assert format_cron("", dry_run=False).out == "cron block -\n"


def test_delete_one_reinstated_per_deleted_and_unknown_key_raises() -> None:
    assert format_delete([1, 2], {2: (5, True)}).out == (
        "deleted note 1\ndeleted note 2; reinstated note 5 (expired)\n"
    )
    with pytest.raises(ValueError):
        format_delete([1], {9: (5, False)})


def test_busy_warning_is_a_warning_not_a_notice(tmp_path: Path) -> None:
    busy = format_prune({"outcome": "busy"})
    assert busy.err == "" and busy.warnings.startswith("warning (prune_busy)")
    gw = FakeGateway(tmp_path)
    emit(gw, Rendered("o\n", "e\n", "w\n"))
    assert (gw.terminal.stdout, gw.terminal.stderr) == ("o\n", "e\nw\n")


# --- the bytes that reach the terminal ---------------------------------------------------------


def assert_clean_bytes(data: bytes) -> None:
    assert 0x1B not in data and 0x7F not in data
    assert b"\xc2\x9b" not in data
    assert not re.search(rb"\xc2[\x80-\x9f]", data)
    assert not [b for b in data if b < 0x20 and b != 0x0A]


def test_emitted_bytes_of_hostile_show_and_list_are_clean(tmp_path: Path) -> None:
    hostile = "\n".join(HOSTILE)
    n = note(topic=HOSTILE[0], kind=HOSTILE[7], labels=HOSTILE[:8], body=hostile)
    for rendered in (
        format_show(n),
        format_list([n, n], 9, 2, 3),
        format_cron(hostile, dry_run=False),
    ):
        gw = FakeGateway(tmp_path)
        emit(gw, rendered)
        assert_clean_bytes(gw.terminal.stdout.encode("utf-8"))
        assert_clean_bytes(gw.terminal.stderr.encode("utf-8"))


# --- fixtures match the schemas ----------------------------------------------------------------


def test_fixtures_match_the_schemas() -> None:
    n = note()
    validate("cli/show", n)
    listed = {k: v for k, v in n.items() if k not in ("body", "body_sha256", "supersedes")}
    validate("cli/list", {"notes": [listed], "total": 1, "shown": 1, "hidden": 0})
    validate("cli/prune", {"expired": 2, "remaining": 5, "dry_run": False})
    validate("cli/prune", {"outcome": "busy"})
    validate(
        "cli/validate",
        {
            "ok": True,
            "checks": [{"name": "config", "ok": True, "reason": None, "message": "valid"}],
            "warnings": [],
        },
    )
    validate("cli/delete", {"deleted": [1, 2], "reinstated": [5]})


# --- goldens (bytes-exact) -------------------------------------------------------------------


def _golden(name: str, rendered: Rendered) -> None:
    """Compare each non-empty part: stdout as ``<name>.txt``, stderr notices as
    ``<name>-err.txt`` and stderr warnings as ``<name>-warnings.txt``."""
    parts = {"": rendered.out, "-err": rendered.err, "-warnings": rendered.warnings}
    assert any(parts.values())
    for suffix, text in parts.items():
        if text:
            check_golden(f"render/{name}{suffix}.txt", text.encode("utf-8"))


def test_golden_list() -> None:
    notes = [
        note(id=9, topic="t\x1b[2Jx", labels=["k=v"]),
        note(id=8, status="superseded", repo="a/b", kind="reviewer", labels=[]),
    ]
    _golden("list-hostile", format_list(notes, 3, 2, 1))


def test_golden_list_forged_columns() -> None:
    _golden(
        "list-forged",
        format_list([note(topic="x  live  2026-01-01  evil", labels=["a,b"])], 1, 1, 0),
    )


def test_golden_list_expired() -> None:
    _golden("list-expired", format_list([note(id=4, status="expired")], 1, 1, 0))


def test_golden_list_empty() -> None:
    _golden("list-empty", format_list([], 0, 0, 0))


def test_golden_show_hostile() -> None:
    body = "plain\n\x1b]0;t\x07 title\n\u202eevil\u202c\ttab\r\n\ud800\n\n"
    n = note(body=body, supersedes=3, labels=["a=b\x1b"])
    _golden("show-hostile", format_show(n))


def test_golden_show_plain() -> None:
    _golden("show-plain", format_show(note()))


def test_golden_show_expired() -> None:
    _golden("show-expired", format_show(note(status="expired", expires="2026-01-03T03:04:05Z")))


def test_golden_show_superseded() -> None:
    _golden("show-superseded", format_show(note(status="superseded", superseded_by=12)))


def test_golden_add() -> None:
    _golden("add-created", format_add(3, created=True))
    _golden("add-exists", format_add(3, created=False))
    _golden("add-dry-run", format_add_dry_run())


def test_golden_delete() -> None:
    _golden("delete-plain", format_delete([1, 2]))
    _golden("delete-reinstated", format_delete([1, 2, 3], {2: (5, False), 3: (6, True)}))


def test_golden_prune() -> None:
    _golden("prune-done", format_prune({"expired": 2, "remaining": 5, "dry_run": False}))
    _golden("prune-dry", format_prune({"expired": 2, "remaining": 5, "dry_run": True}))
    _golden("prune-busy", format_prune({"outcome": "busy"}))


def test_golden_cron() -> None:
    _golden("cron-installed", format_cron("installed", dry_run=False))
    _golden("cron-dry-run", format_cron("installed", dry_run=True))


def test_golden_validate() -> None:
    ok = {"name": "config", "ok": True, "reason": None, "message": "valid"}
    bad = {"name": "state_dir", "ok": False, "reason": "unsafe_state", "message": "bad mode"}
    _golden("validate-ok", format_validate({"ok": True, "checks": [ok], "warnings": []}))
    _golden(
        "validate-fail-warning",
        format_validate({"ok": False, "checks": [ok, bad], "warnings": ["unsafe_filesystem"]}),
    )
