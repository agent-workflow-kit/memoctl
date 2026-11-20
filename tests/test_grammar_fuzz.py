"""Every grammar against an independent, regex-free oracle over a hostile corpus.

The oracles are hand-written character scans, so a flaw in a pattern (``$`` instead of
``fullmatch``, ``\\d`` matching non-ASCII digits, a missing anchor) shows as a disagreement.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from fractions import Fraction

import pytest

from memoctl import grammar
from memoctl.grammar import GrammarError

LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = LOWER.upper()
DIGITS = "0123456789"


def _all_in(text: str, allowed: str) -> bool:
    return all(c in allowed for c in text)


def oracle_topic(s: str) -> bool:
    return 1 <= len(s) <= 64 and s[0] in LOWER + DIGITS and _all_in(s[1:], LOWER + DIGITS + "._-")


def oracle_kind(s: str) -> bool:
    return 1 <= len(s) <= 64 and s[0] in LOWER + DIGITS and _all_in(s[1:], LOWER + DIGITS + "_-")


def oracle_label_key(s: str) -> bool:
    return 1 <= len(s) <= 64 and s[0] in LOWER + DIGITS and _all_in(s[1:], LOWER + DIGITS + "_.-")


def oracle_label_value(s: str) -> bool:
    return len(s) <= 256 and _all_in(s, LOWER + UPPER + DIGITS + "._:/#@+=,-")


def oracle_id(s: str) -> bool:
    return 1 <= len(s) <= 18 and s[0] in "123456789" and _all_in(s, DIGITS)


def oracle_actor(s: str) -> bool:
    for prefix in ("human:", "auto:"):
        if s.startswith(prefix):
            rest = s[len(prefix) :]
            return 1 <= len(rest) <= 64 and _all_in(rest, LOWER + UPPER + DIGITS + "._@/-")
    return False


def oracle_kind_env(s: str) -> bool:
    return 1 <= len(s) <= 64 and s[0] in UPPER and _all_in(s[1:], UPPER + DIGITS + "_")


def oracle_repo(s: str) -> bool:
    head, sep, tail = s.partition("/")
    if not sep:
        return False
    host, colon, port = head.partition(":")
    if colon and not (1 <= len(port) <= 5 and _all_in(port, DIGITS)):
        return False
    if ":" in port or not host:
        return False
    alnum = LOWER + DIGITS
    if host[0] not in alnum or host[-1] not in alnum or not _all_in(host, alnum + ".-"):
        return False
    for segment in tail.split("/"):
        if (
            not segment
            or segment in (".", "..")
            or not _all_in(segment, LOWER + UPPER + DIGITS + "._-")
        ):
            return False
    return True


ORACLES: dict[str, Callable[[str], bool]] = {
    "repo": oracle_repo,
    "topic": oracle_topic,
    "label_key": oracle_label_key,
    "label_value": oracle_label_value,
    "kind": oracle_kind,
    "id": oracle_id,
    "actor": oracle_actor,
    "kind_env": oracle_kind_env,
}

ALPHABET = (
    "abcxyzAZ019"
    "._-:/#@+=,"
    " \t\n\r\x00\x0b\x0c\x7f\x85 ​"
    "éÉſK٣１²①ı"  # noqa: RUF001
    "\U0001d7d9"
)
SEEDS = [
    "example.com/org/repo",
    "host:8080/a/b",
    "a/b",
    "implement",
    "human:jake",
    "CSESSION_KIND",
    "k=v",
    "a.b_c-d",
    "123456789",
    "a/./b",
    "a/../b",
]
SUFFIXES = ["\n", "\r\n", "\x00", " ", "٣", "é", "/", ".", "..", "/.."]
LENGTHS = [0, 1, 2, 5, 17, 18, 19, 63, 64, 65, 255, 256, 257, 300, 1000]


def corpus() -> list[str]:
    rng = random.Random(0x6D656D6F)
    out: list[str] = [""]
    for seed in SEEDS:
        out.append(seed)
        for suffix in SUFFIXES:
            out.append(seed + suffix)
            out.append(suffix + seed)
        for i in range(len(seed) + 1):
            for ch in "٣１é\n/.-A:":
                out.append(seed[:i] + ch + seed[i:])
                out.append(seed[:i] + ch + seed[i + 1 :])
    for n in LENGTHS:
        for ch in "a0A_.-/é٣":
            out.append(ch * n)
            out.append("a" * max(n - 1, 0) + ch)
            out.append("a/" + "b" * n)
            out.append("h:" + "1" * n + "/x")
            out.append("human:" + "x" * n)
            out.append("A" + "B" * n)
            out.append("a" * n + "\n")
            out.append("1" * n)
    for _ in range(40000):
        n = rng.choice([1, 2, 3, 4, 6, 10, 20])
        out.append("".join(rng.choice(ALPHABET) for _ in range(n)))
    for _ in range(20000):
        base = list(rng.choice(SEEDS))
        for _ in range(rng.choice([1, 1, 2, 3])):
            pos = rng.randrange(len(base) + 1)
            if rng.random() < 0.5 and base:
                base[min(pos, len(base) - 1)] = rng.choice(ALPHABET)
            else:
                base.insert(pos, rng.choice(ALPHABET))
        out.append("".join(base))
    return out


CORPUS = corpus()


def test_corpus_is_big_and_contains_the_hostile_shapes() -> None:
    assert len(CORPUS) > 60000
    assert any(s.endswith("\n") for s in CORPUS)
    assert any(c in s for s in CORPUS for c in "٣１")
    assert any(len(s) > 256 for s in CORPUS)


@pytest.mark.parametrize("field", sorted(ORACLES))
def test_grammar_agrees_with_the_oracle_on_the_whole_corpus(field: str) -> None:
    oracle = ORACLES[field]
    mismatches = [s for s in CORPUS if grammar.matches(field, s) != oracle(s)]
    assert mismatches[:5] == []
    # The corpus must exercise both outcomes, or agreement proves nothing.
    verdicts = {oracle(s) for s in CORPUS}
    assert verdicts == {True, False}


@pytest.mark.parametrize("field", sorted(ORACLES))
def test_trailing_newline_is_rejected_for_every_accepted_corpus_string(field: str) -> None:
    accepted = [s for s in CORPUS if ORACLES[field](s)]
    assert accepted
    for s in accepted:
        assert not grammar.matches(field, s + "\n")


def test_every_grammar_has_an_oracle() -> None:
    assert set(grammar.GRAMMARS) == set(ORACLES)


# ---------- durations ----------

UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def oracle_duration(s: str) -> int | None:
    unit = ""
    if s and s[-1] in "smhd":
        unit, s = s[-1], s[:-1]
    whole, dot, frac = s.partition(".")
    if not (1 <= len(whole) <= 10 and _all_in(whole, DIGITS)):
        return None
    if dot and not (1 <= len(frac) <= 9 and _all_in(frac, DIGITS)):
        return None
    value = Fraction(int(whole)) + (Fraction(int(frac), 10 ** len(frac)) if dot else 0)
    return math.ceil(value * UNITS[unit])


def duration_corpus() -> list[str]:
    rng = random.Random(0x64757261)
    out = ["", "0", "59.5s", "12345678901", "5\n", "٥", "1.٥s", "1e3", ".5", "5."]
    for _ in range(40000):
        whole = "".join(rng.choice(DIGITS + "٣１") for _ in range(rng.choice([0, 1, 2, 9, 10, 11])))
        frac = ""
        if rng.random() < 0.5:
            frac = "." + "".join(rng.choice(DIGITS) for _ in range(rng.choice([0, 1, 5, 9, 10])))
        unit = rng.choice(["", "s", "m", "h", "d", "ms", "S", "\n", " ", "x"])
        out.append(whole + frac + unit)
    return out


def test_duration_agrees_with_the_exact_fraction_oracle() -> None:
    corpus_ = duration_corpus()
    seen_ok = seen_bad = 0
    for s in corpus_:
        expected = oracle_duration(s)
        if expected is None:
            seen_bad += 1
            with pytest.raises(GrammarError):
                grammar.parse_duration(s)
        else:
            seen_ok += 1
            assert grammar.parse_duration(s) == expected, s
    assert seen_ok > 1000
    assert seen_bad > 1000
