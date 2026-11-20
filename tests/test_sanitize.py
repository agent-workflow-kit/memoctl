"""The read-time sanitizer (interface.md, Sanitizer): hostile text in, safe text out.

The oracle below is written from the interface text, one code point at a time, and shares no
code with ``memoctl.sanitize``.
"""

from __future__ import annotations

import random
import time
import unicodedata

import pytest

import memoctl.sanitize as san
from memoctl.sanitize import MAX_MARK_RUN, refused_class, sanitize

DEFAULT_IGNORABLE = (
    [0x34F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0]
    + list(range(0x180B, 0x180E))
    + [0x180F]
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)


def oracle_removed(c: str) -> bool:
    """True when the interface says the sanitizer removes ``c`` (tab and ``<`` aside)."""
    if c == "\n" or c == "\t":
        return False
    cat = unicodedata.category(c)
    if cat in ("Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"):
        return True
    return ord(c) in DEFAULT_IGNORABLE


def oracle_once(text: str) -> str:
    """NFKC, then remove, then tab to four spaces, then escape: the interface's three steps."""
    out = []
    for c in unicodedata.normalize("NFKC", text):
        if oracle_removed(c):
            continue
        out.append("    " if c == "\t" else c)
    return "".join(out).replace("<", "&lt;")


def oracle_guard(text: str) -> str:
    """Drop a code point that would make a run of non-zero combining classes exceed 30."""
    run = 0
    out = []
    for c in text:
        d = unicodedata.normalize("NFKD", c)
        lead = 0
        for x in d:
            if unicodedata.combining(x) == 0:
                break
            lead += 1
        if lead == len(d):
            if run + lead > 30:
                continue
            run += lead
        else:
            if run + lead > 30:
                continue
            run = 0
            for x in reversed(d):
                if unicodedata.combining(x) == 0:
                    break
                run += 1
        out.append(c)
    return "".join(out)


def oracle(text: str) -> str:
    """Repeat guard, NFKC and removal until stable, then escape ``<``."""
    cur = text
    while True:
        nxt = "".join(
            ("    " if c == "\t" else c)
            for c in unicodedata.normalize("NFKC", oracle_guard(cur))
            if not oracle_removed(c)
        )
        if nxt == cur:
            return cur.replace("<", "&lt;")
        cur = nxt


# --- the three documented steps -------------------------------------------------------------


def test_plain_text_is_unchanged() -> None:
    assert sanitize("hello\nworld, naïve café") == "hello\nworld, naïve café"


def test_empty_string() -> None:
    assert sanitize("") == ""


def test_nfkc_first() -> None:
    assert sanitize("ﬁ ① Ａ") == "fi 1 A"


def test_tab_becomes_four_spaces() -> None:
    assert sanitize("a\tb") == "a    b"


def test_lf_survives() -> None:
    assert sanitize("a\nb\n") == "a\nb\n"


@pytest.mark.parametrize("c", ["\r", "\x00", "\x1b", "\x7f", "\x85", "\x9b", "\x0b", "\x0c"])
def test_controls_are_removed(c: str) -> None:
    assert sanitize(f"a{c}b") == "ab"


def test_crlf_loses_the_cr() -> None:
    assert sanitize("a\r\nb") == "a\nb"


def test_ansi_and_osc_escapes_lose_their_introducers() -> None:
    # The ESC and BEL bytes go; the remaining printable text is plain text, not an escape.
    out = sanitize("\x1b[31mred\x1b[0m \x1b]0;title\x07 \x9b31m")
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert out == "[31mred[0m ]0;title 31m"


@pytest.mark.parametrize("c", ["\u2028", "\u2029"])
def test_line_and_paragraph_separators_are_removed(c: str) -> None:
    assert sanitize(f"a{c}b") == "ab"


@pytest.mark.parametrize(
    "c",
    [
        "\u202e",  # right-to-left override
        "\u202a",
        "\u2066",
        "\u2069",
        "\u200b",  # zero width space
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
        "\u00ad",
        "\u061c",
        "\U000e0041",  # tag letter
        "\U000e007f",
    ],
)
def test_format_characters_are_removed(c: str) -> None:
    assert sanitize(f"a{c}b") == "ab"


@pytest.mark.parametrize("cp", DEFAULT_IGNORABLE)
def test_default_ignorables_are_removed(cp: int) -> None:
    assert sanitize(f"a{chr(cp)}b") == "ab"


def test_private_use_and_unassigned_are_removed() -> None:
    assert sanitize("a\ue000b\U000f0000c\U0010fffdd") == "abcd"
    assert sanitize("a\U0010ffffb\ufdd0c") == "abc"  # noncharacters
    unassigned = next(chr(i) for i in range(0x378, 0x400) if unicodedata.category(chr(i)) == "Cn")
    assert sanitize(f"a{unassigned}b") == "ab"


def test_unpaired_surrogates_are_removed_and_output_is_encodable() -> None:
    out = sanitize("a\ud800b\udfffc\udc00\ud800")
    assert out == "abc"
    out.encode("utf-8")


def test_paired_looking_surrogates_stay_two_unpaired_ones() -> None:
    # A Python str has no surrogate pairs: a high then a low code point are both unpaired.
    assert sanitize("\ud83d\ude00") == ""


def test_lt_is_escaped() -> None:
    assert sanitize("<b>") == "&lt;b>"
    assert sanitize("</untrusted>") == "&lt;/untrusted>"


@pytest.mark.parametrize(
    "lookalike",
    ["＜", "﹤", "<\u200b/untrusted", "<\x00/untrusted", "<\u202e/untrusted", "&#60;", "&lt;"],
)
def test_no_lt_survives_lookalikes(lookalike: str) -> None:
    out = sanitize(f"x{lookalike}/untrusted>")
    assert "<" not in out


def test_fullwidth_lt_is_normalized_then_escaped() -> None:
    assert sanitize("＜/untrusted>") == "&lt;/untrusted>"
    assert sanitize("﹤/untrusted>") == "&lt;/untrusted>"


def test_removed_characters_cannot_assemble_a_tag() -> None:
    # Removal happens before the escape, so the text between the pieces is gone first.
    assert sanitize("<\u200b/\x00untrusted>") == "&lt;/untrusted>"


def test_lt_with_combining_long_solidus() -> None:
    # "<" + U+0338 would compose to U+226E under NFC; NFKC first, so no "<" can come back.
    out = sanitize("<\u0338")
    assert "<" not in out
    assert sanitize(out) == out


def test_lookalike_fence_markers_are_plain_text_except_lt() -> None:
    assert sanitize("--- note 1 ---") == "--- note 1 ---"  # the block's | prefix handles it
    assert sanitize("[memoctl: note 1 cut]") == "[memoctl: note 1 cut]"


# --- contract details --------------------------------------------------------------------


def test_removal_can_expose_a_composition_and_the_result_is_stable() -> None:
    # e, a removed character, then U+0301: after removal NFKC composes them, so one pass is not
    # enough for sanitize(sanitize(x)) == sanitize(x).
    x = "e\u200b\u0301"
    assert sanitize(x) == "é"
    assert sanitize(sanitize(x)) == sanitize(x)


def test_combining_run_of_30_is_kept_and_31st_dropped() -> None:
    mark = "\u0301"
    assert sanitize("a" + mark * 30) == unicodedata.normalize("NFKC", "a" + mark * 30)
    out = sanitize("a" + mark * 31)
    assert out == unicodedata.normalize("NFKC", "a" + mark * 30)


def test_combining_runs_cannot_be_rejoined_by_removed_characters() -> None:
    mark = "\u0301"
    x = "a" + mark * 30 + "\u200b" + mark * 30
    assert sanitize(x) == sanitize("a" + mark * 30)


def test_max_mark_run_is_30() -> None:
    assert MAX_MARK_RUN == 30


def test_refused_class_names() -> None:
    assert refused_class("\x00") == "control"
    assert refused_class("\r") == "control"
    assert refused_class("\u2028") == "control"
    assert refused_class("\u200b") == "invisible"
    assert refused_class("\ufe0f") == "invisible"
    assert refused_class("\ue000") == "private_use"
    assert refused_class("\ufdd0") == "unassigned"
    assert refused_class("\ud800") == "surrogate"
    assert refused_class("a") is None
    assert refused_class("\n") is None
    assert refused_class("\t") is None
    assert refused_class(" ") is None


def test_every_ascii_char_matches_the_oracle() -> None:
    for i in range(0x80):
        c = chr(i)
        assert sanitize(c) == oracle(c), hex(i)


def test_every_bmp_code_point_matches_the_oracle() -> None:
    for i in range(0x10000):
        c = chr(i)
        assert sanitize("a" + c + "b") == oracle("a" + c + "b"), hex(i)


def test_every_astral_plane_sample_matches_the_oracle() -> None:
    for i in range(0x10000, 0x110000, 7):
        c = chr(i)
        assert sanitize(c) == oracle(c), hex(i)
    for i in range(0xE0000, 0xE0200):
        assert sanitize(chr(i)) == oracle(chr(i)), hex(i)


def test_does_not_mutate_or_depend_on_state_between_calls() -> None:
    a = sanitize("x\u200by")
    assert sanitize("x\u200by") == a == "xy"


# --- property and fuzz -------------------------------------------------------------------------

ALPHABET = (
    list("ab <>/&;lt\n\t\r -|[]")
    + ["\x00", "\x1b", "\x7f", "\x9b", "\u2028", "\u200b", "\u202e", "\ufeff", "\u00ad"]
    + ["\u0301", "\u0338", "\u0300", "\u0316", "\u0345", "\u3099", "\ufe0f", "\ue000"]
    + ["＜", "﹤", "ﬁ", "①", "é", "e", "ᄀ", "ᅡ", "ᆨ", "\ud800", "\udc00"]
    + ["\U0001f600", "\U000e0041", "\U0010ffff", "\u0344", "\u0f73", "Ω", "K"]
)


def random_text(rng: random.Random) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 80)))


def random_flood_text(rng: random.Random) -> str:
    """Runs of marks with removed characters between them, to hit the run guard."""
    parts = []
    for _ in range(rng.randint(1, 6)):
        parts.append(
            rng.choice("ae<") + rng.choice(["\u0301", "\u0316", "\u0338"]) * rng.randint(0, 45)
        )
        parts.append(rng.choice(["", "\u200b", "\x00", "\ufe0f"]))
    return "".join(parts)


@pytest.mark.parametrize("gen", [random_text, random_flood_text])
def test_fuzz_against_the_oracle_and_invariants(gen) -> None:  # type: ignore[no-untyped-def]
    rng = random.Random(24)
    for _ in range(3000):
        x = gen(rng)
        out = sanitize(x)
        assert out == oracle(x), repr(x)
        assert sanitize(out) == out, repr(x)  # idempotent
        assert "<" not in out
        assert all(not oracle_removed(c) for c in out)
        assert "\t" not in out and "\r" not in out
        out.encode("utf-8")
        # the output without the escape is NFKC-stable, so it needs no further normalization
        raw = out.replace("&lt;", "<")
        assert unicodedata.normalize("NFKC", raw) == raw


def test_fuzz_matches_the_literal_spec_when_one_pass_is_stable() -> None:
    rng = random.Random(7)
    checked = 0
    for _ in range(3000):
        x = random_text(rng)
        literal = oracle_once(x)
        if (
            unicodedata.normalize("NFKC", literal.replace("&lt;", "<"))
            == literal.replace("&lt;", "<")
            and "\u0301" * 31 not in x
        ):
            assert sanitize(x) == literal, repr(x)
            checked += 1
    assert checked > 500


def test_text_already_clean_for_the_stored_grammar_is_unchanged_but_for_lt() -> None:
    # A stored body (body rules: no refused character, runs of marks at most 30) passes
    # through as its NFKC form with "<" escaped: the contract claude-session's sanitizer sees.
    body = "line one\n\tindented ünï ＜ 日本語 🙂\nlast"
    assert sanitize(body) == unicodedata.normalize("NFKC", body).replace("\t", "    ").replace(
        "<", "&lt;"
    )


# --- cost ---------------------------------------------------------------------------------------


def timed(text: str) -> float:
    start = time.perf_counter()
    sanitize(text)
    return time.perf_counter() - start


def test_combining_mark_flood_is_linear_not_quadratic() -> None:
    marks = "".join(chr(0x300 + (i * 7) % 0x70) for i in range(200_000))
    # NFKC alone on a mixed-class run of this size takes many seconds.
    assert timed("a" + marks) < 2.0


def test_flood_with_removed_separators_is_fast() -> None:
    chunk = "a" + "\u0316\u0301" * 15 + "\u200b"
    assert timed(chunk * 5000) < 2.0


def test_huge_ascii_and_huge_non_ascii_are_fast() -> None:
    assert timed("hello <world>\n" * 200_000) < 2.0
    assert timed("日本語ﬁ＜\u200b\t" * 100_000) < 2.0


def test_lt_flood_is_linear() -> None:
    n = 1_000_000
    assert sanitize("<" * n) == "&lt;" * n


def test_many_distinct_code_points_stay_fast() -> None:
    text = "".join(chr(i) for i in range(0x110000) if not 0xD800 <= i < 0xE000)
    assert timed(text) < 5.0


def test_time_scales_linearly() -> None:
    unit = "ab\u0301 <x>\u200b\n日本ﬁ"
    small = min(timed(unit * 20_000) for _ in range(3))
    big = min(timed(unit * 200_000) for _ in range(3))
    assert big < small * 30 + 0.05


# --- the vectorized mark-run guard (#24 review) ---------------------------------------------


class _Spy(san._Table):
    calls: list[int]

    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    def __missing__(self, key: int) -> str | None:
        self.calls.append(key)
        return super().__missing__(key)


def test_strip_goes_through_the_translate_table_once_per_distinct_code_point(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = _Spy()
    monkeypatch.setattr(san, "_TABLE", spy)
    text = "ab\t\u200b日本日本ab" * 50
    sanitize(text)
    assert sorted(spy.calls) == sorted({ord(c) for c in text} | {ord(c) for c in "    "})
    n = len(spy.calls)
    sanitize(text * 3)
    assert len(spy.calls) == n  # cached: no Python call for a code point seen before


def test_mark_table_decides_each_code_point_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    table = san._MarkTable()
    seen: list[int] = []
    orig = san._MarkTable.__missing__

    def missing(self, key):  # type: ignore[no-untyped-def]
        seen.append(key)
        return orig(self, key)

    monkeypatch.setattr(san._MarkTable, "__missing__", missing)
    monkeypatch.setattr(san, "_MARKS", table)
    text = "日本語Привет\u0301é" * 100
    san._limit_mark_runs(text)
    san._limit_mark_runs(text)
    assert sorted(seen) == sorted({ord(c) for c in text})


@pytest.mark.parametrize("name", ["_TABLE", "_MARKS"])
def test_caches_stop_growing_at_the_limit(monkeypatch, name: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(san, "_CACHE_LIMIT", 50)
    table = type(getattr(san, name))()
    monkeypatch.setattr(san, name, table)
    text = "".join(chr(0x4E00 + i) for i in range(500)) + "\u0301"
    out = sanitize(text)
    assert len(table) <= 50
    assert out == oracle(text)


@pytest.mark.parametrize(
    "text",
    [
        "日本語の文章です。" * 5000,
        "Привет, мир! " * 5000,
        "déjà vu café " * 5000,
        "한국어 텍스트 " * 5000,
    ],
)
def test_guard_does_not_enter_the_slow_loop_for_ordinary_text(monkeypatch, text: str) -> None:  # type: ignore[no-untyped-def]
    def boom(_text: str) -> str:
        raise AssertionError("slow mark-run loop entered")

    monkeypatch.setattr(san, "_drop_mark_runs", boom)
    assert san._limit_mark_runs(text) == text
    sanitize(text)


def test_guard_enters_the_slow_loop_for_a_long_candidate_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = []
    orig = san._drop_mark_runs
    monkeypatch.setattr(san, "_drop_mark_runs", lambda t: (called.append(1), orig(t))[1])
    assert san._limit_mark_runs("a" + "\u0301" * 31) == "a" + "\u0301" * 30
    assert called


MARKY = ["\u0301", "\u0316", "\u0338", "\u0344", "\u0f73", "\u00a8", "\u1fed", "\u0385"]


def mark_heavy(rng: random.Random) -> str:
    pool = MARKY + ["a", "e", "é", "日", "я", "\u200b", "<", " "]
    weights = [6] * len(MARKY) + [1] * 8
    return "".join(rng.choices(pool, weights, k=rng.randint(0, 120)))


def max_nfkd_run(text: str) -> int:
    best = run = 0
    for c in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(c) != 0 or unicodedata.category(c).startswith("M"):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def test_guard_matches_the_oracle_guard_on_mark_heavy_and_cjk_text() -> None:
    rng = random.Random(1129)
    for _ in range(4000):
        x = mark_heavy(rng) if rng.random() < 0.8 else "日本語" * rng.randint(0, 40)
        assert san._limit_mark_runs(x) == oracle_guard(x), repr(x)


def test_output_never_holds_more_than_30_combining_marks_in_a_row() -> None:
    # Measured directly on the output's NFKD form, independent of the guard's bookkeeping.
    rng = random.Random(30)
    for _ in range(3000):
        x = mark_heavy(rng)
        assert max_nfkd_run(sanitize(x)) <= MAX_MARK_RUN, repr(x)
    for m in MARKY:
        assert max_nfkd_run(sanitize("a" + m * 200)) <= MAX_MARK_RUN, repr(m)
        assert max_nfkd_run(sanitize(("a" + m * 20 + "\u200b") * 5)) <= MAX_MARK_RUN


def test_cjk_ten_million_characters_stay_near_the_vectorized_path() -> None:
    cjk = "日本語の文章" * 1_666_667
    start = time.perf_counter()
    cjk.translate({0x65E5: "x"})  # one C-speed pass over the same text, as the yardstick
    yardstick = time.perf_counter() - start
    t_cjk = min(timed(cjk) for _ in range(2))
    assert t_cjk < 2.0  # the per-character loop took 2.1 s
    assert t_cjk < yardstick * 4 + 0.3


@pytest.mark.parametrize("bad", [None, b"abc", 5, ["a"]])
def test_sanitize_refuses_a_non_str(bad: object) -> None:
    with pytest.raises(TypeError):
        sanitize(bad)  # type: ignore[arg-type]


def test_sanitize_refuses_a_str_subclass() -> None:
    class S(str):
        pass

    with pytest.raises(TypeError):
        sanitize(S("abc"))


def test_fixed_point_needs_at_most_three_passes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real = unicodedata.normalize
    passes = []

    def spy(form: str, s: str) -> str:
        if form == "NFKC":
            passes.append(1)
        return real(form, s)  # type: ignore[arg-type]

    inputs = [
        "e\u200b\u0301",
        "e\u200b\u200c\u0301\u200b\u0323",
        "\u1100\u200b\u1161\u200b\u11a8",
        "a" + "\u0316\u200b" * 40,
    ]
    monkeypatch.setattr(san.unicodedata, "normalize", spy)
    for x in inputs:
        passes.clear()
        sanitize(x)
        assert len(passes) <= 3, repr(x)
