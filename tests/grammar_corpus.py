"""A boundary corpus for comparing memoctl's grammars with a pinned upstream pattern.

Built to catch a one-character or one-bound change in a pattern: every printable ASCII
character in each position of a valid string, every port length from 0 to 7 digits, and each
``{0,N}`` bound at N-1, N and N+1. Every string also appears with a trailing newline.
"""

from __future__ import annotations

PRINTABLE = [chr(c) for c in range(32, 127)]


def _repo_strings() -> list[str]:
    out = ["a/b", "h/.", "h/..", "h/./x", "h/../x", "h/x/.", "h/x/..", "h/a//b", "h/"]
    for c in PRINTABLE:
        out += [
            f"h{c}h/x",
            f"{c}h/x",
            f"h{c}/x",
            f"h:1{c}/x",
            f"h/x{c}",
            f"h/{c}",
            f"h/x/{c}y",
            f"h{c}/{c}",
        ]
    for n in range(8):
        out += [f"h:{'1' * n}/x", f"h:{'0' * n}/x", f"h:{'9' * n}/x", f"h:{'a' * n}/x"]
    out += ["h::1/x", "h:1:1/x", ":1/x", "h:/x"]
    return out


def _word_strings(bound: int, first: str) -> list[str]:
    """Strings for ``first{1}rest{0,bound}``-shaped patterns (kind, label key, topic)."""
    out = [""]
    for c in PRINTABLE:
        out += [c, f"{first}{c}", f"{first}{c}{first}", f"{first}{first}{c}"]
    for n in (bound - 1, bound, bound + 1, bound + 2):
        out += [first * n, first * (n + 1), "a" + "-" * n, "a" + "." * n, "a" + "_" * n]
    return out


def _value_strings(bound: int) -> list[str]:
    out = [""]
    for c in PRINTABLE:
        out += [c, f"a{c}", f"{c}a", f"a{c}a"]
    for n in (bound - 1, bound, bound + 1):
        out += ["a" * n, "a" * (n - 1) + "~", "a" * (n - 1) + "=", "-" * n]
    return out


def pinned_corpus() -> list[str]:
    """Strings for the repo, kind, label key and label value comparisons."""
    base = _repo_strings() + _word_strings(63, "a") + _word_strings(63, "0") + _value_strings(256)
    out = list(dict.fromkeys(base))
    return out + [s + "\n" for s in out]
