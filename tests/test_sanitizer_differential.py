"""memoctl's sanitizer against claude-session's, differentially (interface.md, Sanitizer).

The reference is the pinned copy of claude-session's ``notes.sanitize``
(``tests/pinned/claude_session_sanitizer.py``, see ``tests/test_sanitizer_pinned.py``). The
contract is one direction: the text ``context`` prints is already clean for claude-session, so

    claude_session_sanitize(memoctl_sanitize(x)) == memoctl_sanitize(x)

for every ``x`` (the interface states it without a condition; D-19 builds the fixed-point loop
and the mark-run guard to make it hold for text a stored note cannot hold as well). For text a
note can hold ("storable": it passes ``body.check_text``) the output is also the NFKC form with
tab as four spaces and ``<`` escaped.

The two sanitizers behave differently on the same raw input in a closed, documented set of ways
(``DIFFERENCES``). Each entry has a reason and a direction, a predicate that says whether an
input contains the feature, and a witness. A raw input on which the two disagree must carry at
least one listed feature, and every listed feature must be seen in the sweeps, so the list can
neither hide a new difference nor go stale. Nothing here is skipped: an input outside the list
that differs fails the test.

Runs in a few seconds: one pass over every code point on its own, one with neighbours
(``e`` before and a combining acute after) over every assigned code point and a stride of the
unassigned, private-use and surrogate ones, and whole-range chunks for the contract.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

import memoctl.sanitize as san
from memoctl import body
from memoctl.sanitize import sanitize as memoctl_sanitize
from tests import sanitizer_pin
from tests.test_cmd_context import HOSTILE_BODIES
from tests.test_context_render import BAD_TEXTS

claude_session_sanitize = sanitizer_pin.claude_session_sanitize()

# What the pinned sanitizer strips as "reserved", whatever the Unicode version: they are Cn or
# Cf in memoctl's judgement too, so they are not a difference.
CS_RESERVED = {0x2065, *range(0xFFF0, 0xFFF9), *range(0xE0000, 0xE1000)}
CLOSING_LOOKALIKES = ["<", "\uff1c", "\ufe64"]  # `<` and the two code points whose NFKC is `<`
MARKS = [chr(c) for c in range(0x300, 0x370)]


def storable(text: str) -> bool:
    """True when a note can hold ``text``: strict UTF-8 and the body rules' items 2 to 4."""
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    try:
        body.check_text(data)
    except body.BodyTextError:
        return False
    return True


# --- the documented behavioural differences -------------------------------------------------------


def _cps(text: str) -> Iterable[str]:
    """The code points of ``text`` and of its NFKC form (a feature can come from the mapping)."""
    yield from text
    yield from unicodedata.normalize("NFKC", text)


def _has_lt(text: str) -> bool:
    return "<" in unicodedata.normalize("NFKC", text)


def _has_tab(text: str) -> bool:
    return "\t" in text


def _has_separator(text: str) -> bool:
    return any(c in "\u2028\u2029" for c in text)


def _has_private_use(text: str) -> bool:
    return any(unicodedata.category(c) == "Co" for c in _cps(text))


def _has_unassigned(text: str) -> bool:
    return any(unicodedata.category(c) == "Cn" and ord(c) not in CS_RESERVED for c in _cps(text))


def _has_braille_blank(text: str) -> bool:
    return any(c == "\u2800" for c in _cps(text))


def _has_surrogate(text: str) -> bool:
    return any(unicodedata.category(c) == "Cs" for c in text)


def _needs_second_pass(text: str) -> bool:
    """claude-session runs NFKC and the strips once: its own output is not a fixed point."""
    once = claude_session_sanitize(text)
    return claude_session_sanitize(once) != once


def _has_long_mark_run(text: str) -> bool:
    return body.has_long_combining_run(text) or body.has_long_combining_run(
        unicodedata.normalize("NFKC", text)
    )


@dataclass(frozen=True)
class Difference:
    name: str
    present: Callable[[str], bool]
    witness: str  # a raw input that has the feature, and no other
    claude_session: str  # what the pinned sanitizer returns for the witness
    memoctl: str  # what memoctl returns for the witness
    direction: str  # which side is the more conservative, and why it does not matter
    reason: str
    storable: bool  # can it differ on text a note can hold?
    version_dependent: bool = False


DIFFERENCES = [
    Difference(
        "lt_escape",
        _has_lt,
        "a<b",
        "a<b",
        "a&lt;b",
        "memoctl is stricter: it escapes every `<`, claude-session only `<`, optional whitespace,"
        " `/`, optional whitespace, `untrusted`",
        "D-19: the escape is last and total, so the fence needs no collision rule. The output has"
        " no `<`, so claude-session's pattern never matches it.",
        True,
    ),
    Difference(
        "tab",
        _has_tab,
        "a\tb",
        "a\tb",
        "a    b",
        "memoctl rewrites what claude-session keeps",
        "Body rules item 3 lets a note hold a tab; the renderer prints four spaces so a line"
        " cannot be widened to a different column count.",
        True,
    ),
    Difference(
        "line_paragraph_separator",
        _has_separator,
        "a\u2028b",
        "a\nb",
        "ab",
        "memoctl removes what claude-session turns into LF",
        "A stored note cannot hold U+2028/U+2029 (control class); a foreign row's separator"
        " must not add a line to a block whose lines are counted and prefixed.",
        False,
    ),
    Difference(
        "private_use",
        _has_private_use,
        "a\ue000b",
        "a\ue000b",
        "ab",
        "memoctl removes what claude-session keeps",
        "Category Co is refused by body rules item 3 (private_use); only a foreign row has one.",
        False,
    ),
    Difference(
        "unassigned",
        _has_unassigned,
        "a\u0378b",
        "a\u0378b",
        "ab",
        "memoctl removes what claude-session keeps (outside its reserved ranges)",
        "Category Cn is refused by body rules item 3 (unassigned). Cn is the running"
        " interpreter's unicodedata: a code point assigned by a newer Unicode version is kept by"
        " memoctl there and removed on an older one (3.12 has Unicode 15.0, 3.13 has 15.1). The"
        " witness is unassigned in both.",
        False,
        version_dependent=True,
    ),
    Difference(
        "braille_blank",
        _has_braille_blank,
        "a\u2800b",
        "a\u2800b",
        "ab",
        "memoctl removes what claude-session keeps",
        "U+2800 is default-ignorable outside Cf; body rules item 3 refuses it, claude-session's"
        " list does not name it.",
        False,
    ),
    Difference(
        "lone_surrogate",
        _has_surrogate,
        "a\ud800b",
        "a\ud800b",
        "ab",
        "memoctl removes what claude-session keeps (and cannot encode)",
        "A lone surrogate cannot be stored (strict UTF-8) and encoding it raises, so memoctl"
        " removes it; claude-session's output may not be encodable.",
        False,
    ),
    Difference(
        "single_pass_nfkc",
        _needs_second_pass,
        "e\u200b\u0301",
        "e\u0301",
        "\u00e9",
        "memoctl repeats NFKC and the strips until stable; claude-session runs them once",
        "Removing a character can expose a composition. memoctl's output is a fixed point of its"
        " own pass, so claude-session's single pass finds nothing left to change.",
        False,
    ),
    Difference(
        "combining_mark_run_guard",
        _has_long_mark_run,
        "e" + "\u0301\u0316" * 20,
        "\xe9" + "\u0316" * 20 + "\u0301" * 19,
        "\xe9" + "\u0316" * 15 + "\u0301" * 14,
        "memoctl drops a code point that would extend a run past 30 marks; claude-session does not",
        "A stored note holds at most 30 marks in a row; a longer run in a foreign row makes NFKC"
        " quadratic, so memoctl bounds the cost (D-19). claude-session has no such guard.",
        False,
    ),
]
BY_NAME = {d.name: d for d in DIFFERENCES}


# The cheapest predicates first; 820,000 of the 1.1 million code points are unassigned.
FAST_ORDER = sorted(DIFFERENCES, key=lambda d: d.name != "unassigned")


def features(text: str) -> set[str]:
    return {d.name for d in DIFFERENCES if d.present(text)}


def differs(text: str) -> bool:
    return memoctl_sanitize(text) != claude_session_sanitize(text)


# --- the contract ---------------------------------------------------------------------------------


def contract_violations(sanitize: Callable[[str], str], texts: Iterable[str]) -> list[str]:
    """The texts whose output claude-session's sanitizer would change."""
    bad = []
    for text in texts:
        out = sanitize(text)
        if claude_session_sanitize(out) != out:
            bad.append(text)
    return bad


def unexplained(texts: Iterable[str], seen: set[str] | None = None) -> list[str]:
    """Texts on which the sanitizers differ with no listed feature.

    With ``seen``, every feature of each differing text is recorded; without it the first
    feature found explains the difference (the sweeps over every code point use this: fast).
    """
    bad = []
    for text in texts:
        if not differs(text):
            continue
        if seen is None:
            if not any(d.present(text) for d in FAST_ORDER):
                bad.append(text)
            continue
        found = features(text)
        if not found:
            bad.append(text)
        seen |= found
    return bad


def _all_code_points() -> Iterable[str]:
    return (chr(cp) for cp in range(0x110000))


def _neighbours() -> Iterable[str]:
    for cp in range(0x110000):
        c = chr(cp)
        cat = unicodedata.category(c)
        if cat in ("Cn", "Co", "Cs") and cp % 53:
            continue
        yield f"e{c}\u0301"


def _chunks(size: int = 4096) -> Iterable[str]:
    for start in range(0, 0x110000, size):
        yield "".join(chr(cp) for cp in range(start, min(start + size, 0x110000)))


# Code points for the random corpus: each group is a class of trouble the sanitizers treat
# differently or that the documented rules name.
POOLS = {
    "ascii": list("abc XYZ019_-.,;:!?|/\\'\"()[]{}&;#") + ["\n", "\n"],
    "lt": ["<", "<", "\uff1c", "\ufe64", "\u2039", "\u27e8", "\u02c2", "\u226e", "&lt;", "</"],
    "tag": ["/", "untrusted", "UNTRUSTED", "Untrusted", " ", "\n", "\t", "\u00a0", "\u2003"],
    "controls": ["\x00", "\x07", "\x1b", "\x7f", "\x85", "\x9b", "\r", "\x0b", "\x0c", "\t"],
    "separators": ["\u2028", "\u2029"],
    "bidi": ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2069"],
    "invisible": [
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
        "\u00ad",
        "\u034f",
        "\u115f",
        "\u3164",
        "\uffa0",
        "\u2800",
        "\ufe0f",
        "\U000e0100",
    ],
    "tags": [chr(c) for c in (0xE0001, 0xE0020, 0xE0041, 0xE007F, 0xE0080, 0xE0FFF)],
    "surrogates": ["\ud800", "\udbff", "\udc00", "\udfff"],
    "unassigned": [
        "\u0378",
        "\u2065",
        "\ufff0",
        "\ufffe",
        "\U0001fffe",
        "\U0010ffff",
        "\U000e1000",
    ],
    "private": ["\ue000", "\uf8ff", "\U000f0000", "\U0010fffd"],
    "nfkc": [
        "\ufb01",
        "\u2460",
        "\uff21",
        "\u3300",
        "\u0149",
        "\u01c6",
        "\u1e9b\u0323",
        "\u2126",
        "\u212b",
        "\ufdfa",
        "\u00bd",
        "\u2122",
        "\u1100\u1161",
        "\uac00",
        "A\u030a",
        "\u00c5",
        "\u0958",
    ],
    "marks": MARKS,
    "stackers": ["\u0301", "\u0316", "\u0323", "\u0345", "\u0653", "\u1dc0"],
    "unicode": [
        "\u00e9",
        "\u00f1",
        "\u65e5\u672c\u8a9e",
        "\ud55c",
        "\u0627",
        "\U0001f642",
        "\U0001f468\u200d\U0001f469",
    ],
}


def corpus(n: int = 3000, seed: int = 29) -> list[str]:
    """Seeded random strings of mixed pools, plus runs of marks and assembled closing tags."""
    rng = random.Random(seed)
    names = sorted(POOLS)
    out: list[str] = []
    for _ in range(n):
        picks = rng.sample(names, rng.randint(1, 4))
        pool = [item for name in picks for item in POOLS[name]] + POOLS["ascii"]
        out.append("".join(rng.choice(pool) for _ in range(rng.randint(0, 40))))
    for run in (1, 29, 30, 31, 32, 61, 62, 200, 1000):
        for mark_pool in (["\u0301"], ["\u0301", "\u0316"], MARKS, POOLS["stackers"]):
            out.append("e" + "".join(rng.choice(mark_pool) for _ in range(run)))
    # A closing tag put together from pieces a removal or a normalization joins.
    gaps = ["", " ", "\u200b", "\u202e", "\x00", "\t", "\u2028", "\ue000", "\u0378", "\ud800"]
    for lt in CLOSING_LOOKALIKES:
        for g1 in gaps:
            for g2 in gaps:
                out.append(f"{lt}{g1}/{g2}untrusted")
                out.append(f'{lt}{g1}/{g2}UnTrUsTeD id="x">')
    return out


def hostile() -> list[str]:
    """Raw hostile texts: ``HOSTILE_BODIES`` of test_cmd_context and ``BAD_TEXTS`` of
    test_context_render, both imported (test_context_render's own ``HOSTILE_BODIES`` is not)."""
    return [*HOSTILE_BODIES.values(), *BAD_TEXTS.values()]


def test_the_output_is_unchanged_by_claude_session_for_every_code_point_with_neighbours() -> None:
    assert contract_violations(memoctl_sanitize, _neighbours()) == []


def test_the_output_is_unchanged_by_claude_session_for_whole_range_chunks() -> None:
    assert contract_violations(memoctl_sanitize, _chunks()) == []


def test_the_output_is_unchanged_by_claude_session_for_the_random_corpus() -> None:
    assert contract_violations(memoctl_sanitize, corpus()) == []


def test_the_output_is_unchanged_by_claude_session_for_the_hostile_bodies() -> None:
    texts = hostile()
    assert len(texts) > 40
    assert contract_violations(memoctl_sanitize, texts) == []


def test_the_output_for_storable_text_is_its_nfkc_form_with_tab_and_lt_rewritten() -> None:
    texts = [t for t in [*corpus(), *hostile(), *_neighbours()] if storable(t)]
    assert len(texts) > 1000
    for text in texts:
        expected = unicodedata.normalize("NFKC", text).replace("\t", "    ").replace("<", "&lt;")
        assert memoctl_sanitize(text) == expected, text[:40]


def test_a_storable_text_differs_only_by_the_storable_differences() -> None:
    allowed = {d.name for d in DIFFERENCES if d.storable}
    texts = [t for t in [*corpus(), *hostile(), *_neighbours()] if storable(t)]
    for text in texts:
        if differs(text):
            found = features(text)
            assert found and found <= allowed, (text[:40], found)


# --- the allowlist: every difference is listed, and every entry still happens ---


def test_every_code_point_is_unchanged_by_claude_session_and_every_difference_is_listed() -> None:
    # One pass over all 0x110000 code points, each on its own, for both properties.
    clean: list[str] = []
    unlisted: list[str] = []
    for text in _all_code_points():
        out = memoctl_sanitize(text)
        theirs = claude_session_sanitize(text)
        if claude_session_sanitize(out) != out:
            clean.append(text)
        if out != theirs and not any(d.present(text) for d in FAST_ORDER):
            unlisted.append(text)
    assert clean == []
    assert unlisted == []


def test_every_difference_on_the_neighbour_strings_has_a_listed_feature() -> None:
    seen: set[str] = set()
    assert unexplained(_neighbours(), seen) == []
    # e, a removed character, then an acute that composes
    assert "single_pass_nfkc" in seen


def test_every_difference_on_the_corpus_and_hostile_bodies_has_a_listed_feature() -> None:
    seen: set[str] = set()
    assert unexplained([*corpus(), *hostile()], seen) == []
    assert seen == set(BY_NAME)


@pytest.mark.parametrize("name", sorted(BY_NAME))
def test_each_witness_shows_its_difference_alone(name: str) -> None:
    d = BY_NAME[name]
    assert features(d.witness) == {name}
    assert claude_session_sanitize(d.witness) == d.claude_session
    assert memoctl_sanitize(d.witness) == d.memoctl
    assert d.claude_session != d.memoctl
    assert claude_session_sanitize(d.memoctl) == d.memoctl  # the required direction
    assert d.direction and d.reason


def test_unassigned_is_the_only_version_dependent_entry() -> None:
    assert [d.name for d in DIFFERENCES if d.version_dependent] == ["unassigned"]


def test_the_assigned_set_of_this_interpreter_decides_the_unassigned_feature() -> None:
    cs_keeps = [
        cp
        for cp in range(0x110000)
        if unicodedata.category(chr(cp)) == "Cn" and cp not in CS_RESERVED
    ]
    assert cs_keeps
    assert all(claude_session_sanitize(chr(cp)) == chr(cp) for cp in cs_keeps[::97])
    assert all(memoctl_sanitize(chr(cp)) == "" for cp in cs_keeps[::97])
    assert all(memoctl_sanitize(chr(cp)) == "" for cp in sorted(CS_RESERVED)[::13])
    assert all(claude_session_sanitize(chr(cp)) == "" for cp in sorted(CS_RESERVED)[::13])


def test_a_feature_free_text_is_sanitized_identically() -> None:
    plain = "Hello, caf\u00e9 \u00e9 \u65e5\u672c\u8a9e \U0001f642\nline two | x\n"
    assert features(plain) == set()
    assert memoctl_sanitize(plain) == claude_session_sanitize(plain) == plain


def test_a_difference_outside_the_list_is_reported() -> None:
    seen: set[str] = set()
    original = memoctl_sanitize

    def broken(text: str) -> str:
        return original(text).replace("x", "y")

    saved = globals()["memoctl_sanitize"]
    globals()["memoctl_sanitize"] = broken
    try:
        assert unexplained(["x", "plain"], seen) == ["x"]
    finally:
        globals()["memoctl_sanitize"] = saved


# --- the check can fail ---------------------------------------------------------------------------


def _single_pass(text: str) -> str:
    """memoctl's sanitizer without the repeat (the property D-19 added)."""
    return san._strip(unicodedata.normalize("NFKC", text)).replace("<", "&lt;")


def _no_guard_unescaped_closing(text: str) -> str:
    return memoctl_sanitize(text).replace("&lt;/", "</")


def _escape_first(text: str) -> str:
    """`<` escaped before the strips (the other order; also safe against claude-session)."""
    return san._strip(unicodedata.normalize("NFKC", text.replace("<", "&lt;")))


@pytest.mark.parametrize(
    ("mutant", "text"),
    [
        (_single_pass, "e\u200b\u0301"),
        (_no_guard_unescaped_closing, "</untrusted>"),
        (_no_guard_unescaped_closing, "<\u200b/UNTRUSTED"),
        (lambda t: t, "\u2028"),
        (lambda t: t, "\x00"),
        (lambda t: unicodedata.normalize("NFKC", t), "a\u200b\u0301"),
    ],
    ids=["no-repeat", "no-escape", "no-escape-hidden", "identity-sep", "identity-nul", "nfkc-only"],
)
def test_the_contract_check_catches_a_broken_sanitizer(
    mutant: Callable[[str], str], text: str
) -> None:
    assert contract_violations(mutant, [text]) == [text]


def test_the_fixed_point_is_not_a_property_of_one_pass() -> None:
    # The repeat is what makes the contract hold for a foreign row: without it the sweep fails.
    assert contract_violations(_single_pass, list(_neighbours())[:200_000]) != []


def test_escaping_before_or_after_the_strips_is_safe_against_claude_sessions_closing_pattern() -> (
    None
):
    # Either order gives `&lt;/untrusted` for `<`, a format character and `/untrusted`, which
    # claude-session's closing pattern (`<` first) never matches. D-19's "last" is about the
    # later fence and rendering steps, not about this pattern.
    assert contract_violations(_escape_first, ["<\u200b/untrusted"]) == []
    assert memoctl_sanitize("<\u200b/untrusted") == "&lt;/untrusted"
    assert claude_session_sanitize("<\u200b/untrusted") == "&lt;/untrusted"


# --- the residue check: nothing hides behind a listed feature ------------------------------
#
# The sweeps above accept a difference when ANY listed feature is present, so a new difference
# that co-occurs with a listed one (say memoctl wrongly drops `x` whenever the text has U+2800)
# is hidden. This check names the whole expected output instead: the documented rewrites applied
# to the input, then claude-session's own sanitizer (minus its closing-tag step, which memoctl's
# total `<` escape replaces) repeated to a fixed point, then tab and `<`.


def _claude_session_without_closing_step() -> Callable[[str], str]:
    module = sanitizer_pin.load()
    # Never matches: the one step memoctl's total `<` escape replaces.
    module._CLOSING = re.compile(r"(?!)(untrusted)")  # type: ignore[attr-defined]
    return module.sanitize  # type: ignore[no-any-return]


_cs_open = _claude_session_without_closing_step()
_FOREIGN_ONLY = {"Co", "Cn", "Cs"}


def _kept(text: str) -> str:
    """The text without the characters memoctl removes and claude-session keeps or turns to LF."""
    return "".join(
        c
        for c in text
        if unicodedata.category(c) not in _FOREIGN_ONLY and c not in "\u2800\u2028\u2029"
    )


def _fixed_point(text: str) -> str:
    """The text with the listed removals, then claude-session's steps repeated until stable."""
    cur = _kept(text)
    while (nxt := _cs_open(cur)) != cur:
        cur = nxt
    return cur


def expected_output(text: str) -> str:
    """What memoctl must print, derived from claude-session's behaviour and the listed rewrites."""
    return _fixed_point(text).replace("\t", "    ").replace("<", "&lt;")


def residue_mismatches(sanitize: Callable[[str], str], texts: Iterable[str]) -> list[str]:
    """Texts whose output is not ``expected_output``.

    Left out: a text whose characters, once the ones both sanitizers remove are gone, hold a run
    of more than 30 combining marks at the fixed point (the same text ``expected_output`` is
    built from, so a run that only appears after a second pass is skipped too): there memoctl's
    guard drops code points by design.
    """
    bad = []
    for text in texts:
        # Skipped here; the combining_mark_run_guard witness and the contract sweeps cover them.
        if body.has_long_combining_run(_fixed_point(text)):
            continue
        if sanitize(text) != expected_output(text):
            bad.append(text)
    return bad


def test_the_output_is_exactly_the_documented_rewrites_of_claude_sessions_for_every_text() -> None:
    texts = [*_all_code_points(), *_neighbours(), *_chunks(), *corpus(), *hostile()]
    assert residue_mismatches(memoctl_sanitize, texts) == []


def test_the_residue_check_sees_a_difference_that_hides_behind_a_listed_feature() -> None:
    def hides(text: str) -> str:  # drops `x` only when U+2800 is also there: a listed feature
        out = memoctl_sanitize(text)
        return out.replace("x", "") if "\u2800" in text else out

    text = "a\u2800x"
    assert hides(text) != claude_session_sanitize(text)
    assert features(text)  # the old check accepts the difference: a listed feature is present ...
    assert memoctl_sanitize(text) == "ax"
    assert residue_mismatches(hides, [text]) == [text]  # ... this one does not


def test_the_residue_gate_uses_the_fixed_point_not_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def one_bar_per_pass(text: str) -> str:  # a stand-in step: each pass removes one `|`
        return text.replace("|", "", 1)

    monkeypatch.setitem(globals(), "_cs_open", one_bar_per_pass)
    text = "e" + "\u0301" * 15 + "||" + "\u0301" * 16
    assert not body.has_long_combining_run(one_bar_per_pass(_kept(text)))  # one pass: no run
    assert body.has_long_combining_run(_fixed_point(text))  # the fixed point: a run of 31
    assert residue_mismatches(lambda t: t, [text]) == []  # skipped, not a false failure


# --- the golden blocks ---------------------------------------------------------------------------

GOLDEN_CONTEXT = sorted((Path(__file__).parent / "golden" / "context").glob("*.txt"))


def test_the_golden_context_files_were_found() -> None:
    assert GOLDEN_CONTEXT
    assert any("fenced" in p.name for p in GOLDEN_CONTEXT)
    assert any("fenced" not in p.name for p in GOLDEN_CONTEXT)


@pytest.mark.parametrize("path", [p for p in GOLDEN_CONTEXT if "fenced" not in p.name], ids=str)
def test_a_golden_plain_block_is_unchanged_by_claude_session(path: Path) -> None:
    # claude-session calls `context` without --fence (interface.md, Context block) and fences the
    # block itself, so the plain block is what it sanitizes.
    text = path.read_text(encoding="utf-8")
    assert claude_session_sanitize(text) == text


@pytest.mark.parametrize("path", [p for p in GOLDEN_CONTEXT if "fenced" in p.name], ids=str)
def test_a_golden_fenced_block_is_changed_by_claude_session(path: Path) -> None:
    # The negative companion: the fence's own closing line is what its pattern rewrites, which is
    # why claude-session is never given a `--fence` block.
    text = path.read_text(encoding="utf-8")
    assert claude_session_sanitize(text) != text
