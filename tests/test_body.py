"""Body validation (interface.md, Body rules items 1 to 6): classes, lines, limits, hostile."""

from __future__ import annotations

import copy
import pickle
import time
import unicodedata
from typing import Literal

import pytest

from memoctl import body
from memoctl.body import BodyTextError, check_text, validate_body
from memoctl.bodyread import BodyError
from memoctl.cli_errors import failure_for
from memoctl.sanitize import refused_class
from tests.golden_files import check_golden

LIMIT = 8192


def refusal(data: bytes, limit: int = LIMIT) -> BodyTextError:
    with pytest.raises(BodyTextError) as info:
        validate_body(data, limit)
    return info.value


# ---------- accepted ----------


@pytest.mark.parametrize(
    "text",
    [
        "a",
        "hello world\n",
        "tab\tseparated\n\nblank line above",
        "café 日本語 \U0001f600",  # emoji without a variation selector
        "x　y",  # ideographic space is whitespace, not refused
        "  a  ",
        "a" * LIMIT,
    ],
)
def test_valid_text_is_returned_unchanged(text: str) -> None:
    assert validate_body(text.encode(), LIMIT) == text


def test_text_is_never_rewritten_to_nfkc() -> None:
    assert validate_body("ﬁ".encode(), LIMIT) == "ﬁ"  # ligature fi stays


def test_exactly_at_the_limit_is_accepted_and_one_over_is_refused() -> None:
    assert validate_body(b"a" * 256, 256) == "a" * 256
    with pytest.raises(BodyError) as info:
        validate_body(b"a" * 257, 256)
    assert info.value.reason == "too_long"
    assert info.value.details == {"field": "body", "limit": 256}


# ---------- item 1 runs first ----------


def test_over_limit_wins_over_every_later_failure() -> None:
    with pytest.raises(BodyError):
        validate_body(b"\xff" * 300, 256)


# ---------- item 2: strict UTF-8 ----------

BAD_UTF8 = {
    "lone continuation": b"\x80",
    "truncated 2": b"a\xc3",
    "truncated 3": b"a\xe2\x82",
    "overlong NUL": b"\xc0\x80",
    "overlong slash": b"\xc0\xaf",
    "overlong 3-byte": b"\xe0\x80\xaf",
    "overlong 4-byte": b"\xf0\x80\x80\xaf",
    "encoded surrogate": b"\xed\xa0\x80",
    "encoded low surrogate": b"\xed\xbf\xbf",
    "above U+10FFFF": b"\xf4\x90\x80\x80",
    "five byte": b"\xf8\x88\x80\x80\x80",
    "0xFF": b"\xff",
    "0xFE": b"\xfe",
    "latin-1 text": "caf\xe9".encode("latin-1"),
    "utf-16 text": "hi".encode("utf-16"),
}


@pytest.mark.parametrize("data", list(BAD_UTF8.values()), ids=list(BAD_UTF8))
def test_invalid_utf8_is_class_encoding(data: bytes) -> None:
    err = refusal(data)
    assert err.cls == "encoding"
    assert err.details == {"field": "body", "class": "encoding", "line": 1}


def test_encoding_line_counts_lf_before_the_offending_byte() -> None:
    assert refusal(b"a\nb\n\xff\nc").line == 3
    assert refusal(b"\n\n\n\xff").line == 4
    assert refusal(b"ok\r\n\xff").line == 2


def test_encoding_is_reported_before_a_refused_character_that_comes_first() -> None:
    err = refusal(b"\x00\xff")
    assert err.cls == "encoding"


def test_utf8_bom_is_an_invisible_character_not_skipped() -> None:
    err = refusal(b"\xef\xbb\xbfhello")
    assert (err.cls, err.line) == ("invisible", 1)


# ---------- item 3: the refused characters ----------

# One golden per refused class and length case: the error envelope the CLI prints for the body.
# Sweeps over every code point of a class stay below as plain assertions.

GOLDEN_REFUSALS: dict[str, tuple[bytes, int]] = {
    "encoding": (b"ok\nok\n\xff\xfe", LIMIT),
    "control": (b"one\ntwo\x00", LIMIT),
    "control-cr": (b"a\r\nb", LIMIT),
    "control-line-separator": ("a\u2028b".encode(), LIMIT),
    "invisible": ("a\u202eb".encode(), LIMIT),
    "invisible-bom": (b"\xef\xbb\xbfhello", LIMIT),
    "invisible-ignorable": ("a\u034fb".encode(), LIMIT),
    "invisible-unassigned-tag": ("a\U000e0000b".encode(), LIMIT),
    "private-use": ("a\ue000b".encode(), LIMIT),
    "unassigned": ("a\u0378b".encode(), LIMIT),
    "combining": (("e" + "\u0301" * 31).encode(), LIMIT),
    "whitespace-only": (b" \n\t ", LIMIT),
    "nfkc-too-long": (("\ufdfa" * 100).encode(), 400),
}


@pytest.mark.parametrize("case", list(GOLDEN_REFUSALS))
def test_refusal_golden(case: str) -> None:
    data, limit = GOLDEN_REFUSALS[case]
    with pytest.raises(BodyTextError) as info:
        validate_body(data, limit)
    failure = failure_for(info.value)
    check_golden(f"body/{case}.json", failure.envelope().encode(), schema="cli/error")
    check_golden(f"body/{case}-err.txt", failure.status_line().encode())


def test_too_long_golden() -> None:
    with pytest.raises(BodyError) as info:
        validate_body(b"a" * 257, 256)
    failure = failure_for(info.value)
    check_golden("body/too-long.json", failure.envelope().encode(), schema="cli/error")
    check_golden("body/too-long-err.txt", failure.status_line().encode())


def test_search_text_refusal_golden() -> None:
    with pytest.raises(BodyTextError) as info:
        check_text(b"a\x00", field="text")
    failure = failure_for(info.value)
    check_golden("body/text-control.json", failure.envelope().encode(), schema="cli/error")


def test_classify_is_the_sanitizers_classification_for_every_code_point() -> None:
    for cp in range(0x110000):
        ch = chr(cp)
        expected = refused_class(ch)
        if expected == "surrogate":
            expected = None  # strict UTF-8 cannot decode one; not a body class
        assert body.classify(ch) == expected, hex(cp)


def test_the_tag_block_is_invisible_to_the_sanitizer_labels_too() -> None:
    for cp in range(0xE0000, 0xE0080):
        assert refused_class(chr(cp)) == "invisible", hex(cp)


CONTROL = [chr(c) for c in [*range(0, 9), *range(11, 32), 127, *range(0x80, 0xA0), 0x2028, 0x2029]]


@pytest.mark.parametrize("ch", CONTROL, ids=[f"U+{ord(c):04X}" for c in CONTROL])
def test_control_characters_are_refused(ch: str) -> None:
    assert refusal(f"a{ch}b".encode()).cls == "control"


def test_lf_and_tab_are_the_only_allowed_controls() -> None:
    assert validate_body(b"a\tb\nc", LIMIT) == "a\tb\nc"
    assert refusal(b"a\r\nb").cls == "control"  # CR
    assert refusal(b"a\x00").cls == "control"  # NUL
    assert refusal(b"a\x1b[31m").cls == "control"  # ESC


INVISIBLE_EXTRA = [
    0x034F,
    0x115F,
    0x1160,
    0x17B4,
    0x17B5,
    0x180B,
    0x180C,
    0x180D,
    0x180F,
    0x2800,
    0x3164,
    *range(0xFE00, 0xFE10),
    0xFFA0,
    *range(0xE0100, 0xE01F0),
]


@pytest.mark.parametrize("cp", INVISIBLE_EXTRA, ids=[f"U+{c:04X}" for c in INVISIBLE_EXTRA])
def test_default_ignorables_outside_cf_are_invisible(cp: int) -> None:
    assert refusal(f"a{chr(cp)}b".encode()).cls == "invisible"


def test_every_format_character_is_invisible() -> None:
    cf = [c for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf"]
    assert cf  # the interpreter has some
    for cp in cf:
        assert body.classify(chr(cp)) == "invisible", hex(cp)


def test_named_invisibles() -> None:
    for cp in (0x200B, 0x200C, 0x200D, 0x00AD, 0xFEFF, 0x202E, 0x2066, 0x2060, 0x061C):
        assert refusal(f"a{chr(cp)}".encode()).cls == "invisible", hex(cp)


def test_whole_tag_block_is_invisible_even_where_unassigned() -> None:
    for cp in range(0xE0000, 0xE0080):
        assert body.classify(chr(cp)) == "invisible", hex(cp)


def test_zwj_and_emoji_variation_selector_are_refused() -> None:
    assert refusal("\U0001f468‍\U0001f469".encode()).cls == "invisible"
    assert refusal("❤️".encode()).cls == "invisible"


def test_private_use_planes() -> None:
    for cp in (0xE000, 0xF8FF, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD):
        assert refusal(f"a{chr(cp)}".encode()).cls == "private_use", hex(cp)


def test_unassigned_and_noncharacters() -> None:
    for cp in (0x0378, 0xFFFE, 0xFFFF, 0xFDD0, 0x10FFFF, 0x1FFFE, 0x2FFFF):
        assert refusal(f"a{chr(cp)}".encode()).cls == "unassigned", hex(cp)


def test_classify_agrees_with_unicodedata_for_all_code_points() -> None:
    allowed_extra = set()
    for cp in range(0x110000):
        ch = chr(cp)
        if 0xD800 <= cp <= 0xDFFF:
            continue  # cannot occur in decoded strict UTF-8
        cls = body.classify(ch)
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch not in "\n\t":
            assert cls == "control", hex(cp)
        elif cat in {"Cf"}:
            assert cls == "invisible", hex(cp)
        elif cat == "Co":
            assert cls == "private_use", hex(cp)
        elif cat == "Cn" and not 0xE0000 <= cp <= 0xE007F:
            assert cls == "unassigned", hex(cp)
        if cls is None:
            allowed_extra.add(cp)
    # nothing in any refused class is accepted; whitespace letters stay accepted
    assert ord("a") in allowed_extra and ord("\n") in allowed_extra and 0x3000 in allowed_extra


def test_first_offender_in_text_order_wins_across_classes() -> None:
    assert refusal("a​b\x00".encode()).cls == "invisible"
    assert refusal("a\x00b​".encode()).cls == "control"


def test_line_of_a_refused_character_counts_lf_only() -> None:
    err = refusal(b"one\ntwo\nth\x00ree")
    assert err.details == {"field": "body", "class": "control", "line": 3}
    # CR and U+2028 are not line breaks for the count
    assert refusal("a\r b c\x00".encode()).line == 1
    assert refusal(("\n" * 5 + "​").encode()).line == 6


# ---------- item 4: combining runs ----------


def test_thirty_marks_are_accepted_and_thirty_one_refused() -> None:
    assert validate_body(("a" + "́" * 30).encode(), LIMIT)
    err = refusal(("a" + "́" * 31).encode())
    assert err.details == {"field": "body", "class": "combining", "line": 1}


def test_four_thousand_marks_at_the_default_limit_are_refused_as_combining() -> None:
    err = refusal(("a" + "\u0301" * 4000).encode(), 8192)
    assert (err.cls, err.line) == ("combining", 1)


def test_a_run_may_start_without_a_base_character() -> None:
    assert refusal(("́" * 31).encode()).cls == "combining"
    assert validate_body(("́" * 30).encode(), LIMIT)


def test_a_base_character_between_marks_resets_the_run() -> None:
    assert validate_body((("a" + "́" * 30) * 50).encode(), LIMIT)


def test_mixed_class_marks_count_together() -> None:
    marks = ["̀", "̖", "̴", "ͅ", "⃐", "᪰"]  # different ccc values
    assert all(unicodedata.combining(m) for m in marks)
    run = "".join(marks[i % len(marks)] for i in range(31))
    assert refusal(("e" + run).encode()).cls == "combining"
    assert validate_body(("e" + run[:30]).encode(), LIMIT)


def test_run_is_measured_in_nfkd_not_in_source_code_points() -> None:
    # U+0344 decomposes to two marks: 16 of them is 32 marks in NFKD.
    assert unicodedata.normalize("NFKD", "̈́") == "̈́"
    assert refusal(("a" + "̈́" * 16).encode()).cls == "combining"
    assert validate_body(("a" + "̈́" * 15).encode(), LIMIT)


def test_marks_from_a_decomposed_character_join_the_run() -> None:
    # U+0344 is two marks in NFKD; a base letter first, so only marks count.
    two = "\u0344"
    assert validate_body(("a" + "\u0301" * 28 + two).encode(), LIMIT)  # 30 marks
    assert refusal(("a" + "\u0301" * 29 + two).encode()).cls == "combining"  # 31 marks
    # a precomposed letter is a base again: it resets the run
    assert validate_body(("a" + "\u0301" * 29 + "\u00e9" + "\u0301" * 29).encode(), LIMIT)


def test_combining_line_is_that_of_the_code_point_that_exceeds_the_run() -> None:
    text = "x\n" + "a" + "́" * 30 + "́\n" + "tail"
    assert refusal(text.encode()).line == 2


def test_run_line_can_differ_from_the_line_of_the_runs_start() -> None:
    # LF has combining class 0 so it ends a run; the run cannot span lines.
    text = "a" + "́" * 20 + "\n" + "́" * 20
    assert validate_body(text.encode(), LIMIT)


def test_item3_is_reported_before_item4_even_when_the_run_comes_first() -> None:
    text = "a" + "́" * 40 + "\x00"
    assert refusal(text.encode()).cls == "control"


# ---------- item 5: whitespace only ----------


@pytest.mark.parametrize(
    "data",
    [b"", b" ", b"\n", b"\t\n \n", b" " * 100, "　  ".encode(), b"\x1f"[:0]],
)
def test_empty_and_whitespace_only_is_refused(data: bytes) -> None:
    err = refusal(data)
    assert err.reason == "whitespace_only"
    assert err.cls is None
    assert err.details == {"field": "body"}


def test_one_visible_character_is_enough() -> None:
    assert validate_body(b"\n\n x \n", LIMIT) == "\n\n x \n"


def test_refused_character_in_whitespace_is_class_error_not_whitespace_error() -> None:
    assert refusal(b"  \x00  ").cls == "control"
    assert refusal(b"  \r\n").cls == "control"


# ---------- item 6: the NFKC bound ----------


def test_nfkc_form_over_the_limit_is_refused_with_the_limit_not_a_class() -> None:
    text = "ﷺ" * 100  # 3 bytes each; NFKC is 18 Arabic letters, 33+ bytes
    assert len(text.encode()) <= 400
    err = refusal(text.encode(), 400)
    assert err.reason == "nfkc_too_long"
    assert err.cls is None
    assert err.details == {"field": "body", "limit": 400}


def test_nfkc_exactly_at_the_limit_is_accepted() -> None:
    # U+FB01 (3 bytes) becomes "fi" (2 bytes), so it shrinks; U+2460 becomes "1".
    # U+33A1 (3 bytes) becomes "m2" (2 bytes). Use U+FDFA: find its NFKC byte size.
    n = len(unicodedata.normalize("NFKC", "ﷺ").encode())
    count = 256 // n
    ok = "ﷺ" * count
    assert len(unicodedata.normalize("NFKC", ok).encode()) <= 256
    assert validate_body(ok.encode(), 256) == ok
    over = "ﷺ" * (count + 1)
    if len(over.encode()) <= 256:
        assert refusal(over.encode(), 256).reason == "nfkc_too_long"


def test_nfkc_exact_boundary_with_padding() -> None:
    n = len(unicodedata.normalize("NFKC", "½").encode())  # one half: "1⁄2" 5 bytes
    assert n == 5
    text = "½" * 51 + "a"  # NFKC: 255 + 1 = 256 bytes
    assert len(unicodedata.normalize("NFKC", text).encode()) == 256
    assert validate_body(text.encode(), 256) == text
    text2 = "½" * 51 + "ab"
    assert refusal(text2.encode(), 256).reason == "nfkc_too_long"


def test_item1_before_item6_and_item5_before_item6() -> None:
    with pytest.raises(BodyError):
        validate_body(("ﷺ" * 100).encode(), 100)
    assert refusal(b" " * 10, 256).reason == "whitespace_only"


def test_cap_on_the_limit_parameter_bounds_nfkc_work() -> None:
    with pytest.raises(ValueError):
        validate_body(b"a", body.MAX_LIMIT + 1)
    for bad in (0, -1, True, 1.5, "8192"):
        with pytest.raises(ValueError):
            validate_body(b"a", bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_body("a", LIMIT)  # type: ignore[arg-type]


# ---------- hostile and performance ----------


def timed(fn) -> float:  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def test_maximum_size_inputs_are_linear_and_fast() -> None:
    cases = {
        "ascii": b"a" * 65536,
        "cjk": "日".encode() * (65536 // 3),
        "fdfa": "ﷺ".encode() * (65536 // 3),
        "ligatures": "ﬁ".encode() * (65536 // 3),
        "30-mark runs": (("a" + "́" * 30) * (65536 // 62)).encode(),
        "mixed marks": ("a" + "".join(chr(0x300 + i % 50) for i in range(30))).encode() * 500,
        "all lf": b"\n" * 65535 + b"x",
    }
    for name, data in cases.items():
        data = data[:65536]
        start = time.perf_counter()
        try:
            validate_body(data, 65536)
        except (BodyTextError, BodyError):
            pass
        elapsed = time.perf_counter() - start
        assert elapsed < 1.5, name


def test_quadratic_mark_run_is_refused_before_any_normalization_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = unicodedata.normalize

    def spy(form: Literal["NFC", "NFD", "NFKC", "NFKD"], s: str) -> str:
        calls.append(len(s))
        return real(form, s)

    monkeypatch.setattr(body.unicodedata, "normalize", spy)
    err = refusal(("a" + "́" * 20000).encode(), 65536)
    assert err.cls == "combining"
    assert max(calls, default=0) <= 1  # only per-code-point NFKD; never the whole text


def test_nfkc_runs_only_on_text_that_passed_items_1_to_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []
    real = unicodedata.normalize

    def spy(form: Literal["NFC", "NFD", "NFKC", "NFKD"], s: str) -> str:
        if form == "NFKC":
            sizes.append(len(s))
        return real(form, s)

    monkeypatch.setattr(body.unicodedata, "normalize", spy)
    validate_body(b"a" * 100, 256)
    assert sizes == [100]
    sizes.clear()
    for bad in (b"\xff", b"\x00", b"   ", b"a" * 300):
        with pytest.raises((BodyTextError, BodyError)):
            validate_body(bad, 256)
    assert sizes == []


def test_doubled_lookalikes_are_not_normalized_into_acceptance() -> None:
    # Fullwidth and math letters are accepted as written (no rewrite) but count at NFKC size.
    text = "ＡＢＣ"
    assert validate_body(text.encode(), LIMIT) == text


def test_one_hundred_thousand_ascii_is_refused_by_the_limit_without_decoding() -> None:
    with pytest.raises(BodyError):
        validate_body(b"a" * 100_000, 65536)


# ---------- errors never echo the body ----------

SECRET = "SECRET-TOKEN-xyz"


@pytest.mark.parametrize(
    "data",
    [
        f"{SECRET}\x00".encode(),
        f"{SECRET}​".encode(),
        SECRET.encode() + b"\xff",
        f"{SECRET}".encode(),
        f"{SECRET}͸".encode(),
        (SECRET + "́" * 40).encode(),
        (SECRET + "ﷺ" * 400).encode(),
    ],
)
def test_error_text_and_details_never_contain_the_input(data: bytes) -> None:
    err = refusal(data, 2000)
    shown = repr(err) + str(err) + err.message + repr(err.details) + repr(err.args)
    assert SECRET not in shown
    assert err.__cause__ is None and err.__context__ is None


def test_encoding_error_does_not_chain_the_decoder_error() -> None:
    err = refusal(b"abc\xff")
    assert err.__cause__ is None
    assert err.__context__ is None


CLONES = [copy.copy, copy.deepcopy, lambda e: pickle.loads(pickle.dumps(e))]


@pytest.mark.parametrize("clone", CLONES)
def test_error_keeps_its_fields_when_copied_or_pickled(clone) -> None:  # type: ignore[no-untyped-def]
    for data, limit in [(b"a\n\x00", LIMIT), (b" ", LIMIT), (("ﷺ" * 100).encode(), 400)]:
        err = refusal(data, limit)
        twin = clone(err)
        assert (twin.reason, twin.cls, twin.line, twin.limit, twin.field) == (
            err.reason,
            err.cls,
            err.line,
            err.limit,
            err.field,
        )
        assert twin.details == err.details


def test_field_is_configurable() -> None:
    with pytest.raises(BodyTextError) as info:
        validate_body(b"\x00", LIMIT, field="text")
    assert info.value.details == {"field": "text", "class": "control", "line": 1}


def test_body_text_error_is_not_a_value_error() -> None:
    assert not issubclass(BodyTextError, ValueError)
    with pytest.raises(ValueError) as info:
        validate_body(b"a", 0)
    assert not isinstance(info.value, BodyTextError)


def test_unknown_reason_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        BodyTextError("nope")


def test_details_have_exactly_the_documented_keys() -> None:
    assert set(refusal(b"\xff").details) == {"field", "class", "line"}
    assert set(refusal(b" ").details) == {"field"}
    assert set(refusal(("ﷺ" * 100).encode(), 400).details) == {"field", "limit"}


# ---------- search text: items 2 to 4 only ----------


def test_check_text_applies_items_2_to_4_and_nothing_else() -> None:
    assert check_text(b"", field="text") == ""  # emptiness is the caller's rule
    assert check_text(b"   ", field="text") == "   "
    assert check_text(("ﷺ" * 5000).encode(), field="text")  # no NFKC bound here
    with pytest.raises(BodyTextError) as info:
        check_text(b"a\x00", field="text")
    assert info.value.details == {"field": "text", "class": "control", "line": 1}
    with pytest.raises(BodyTextError):
        check_text(b"\xff", field="text")
    with pytest.raises(BodyTextError):
        check_text(("a" + "́" * 31).encode(), field="text")


def test_has_long_combining_run() -> None:
    from memoctl.body import has_long_combining_run

    assert not has_long_combining_run("e" + "\u0301" * 30)
    assert has_long_combining_run("e" + "\u0301" * 31)
    assert not has_long_combining_run("plain")
