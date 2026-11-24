# Pinned copy of claude-session's notes sanitizer, used only by the sanitizer tests of memoctl
# (tests/test_sanitizer_differential.py, tests/test_sanitizer_pinned.py).
#
# Provenance (see tests/test_sanitizer_pinned.py, which fails when any of this drifts):
#   project:   claude-session (Copyright (c) 2026 agent-workflow-kit contributors, MIT licence,
#              which allows copying with this notice)
#   source:    src/claude_session/notes.py, function ``sanitize`` and the module constants it uses
#   version:   v0.38.4 (also unchanged in v0.45.2: the whole notes.py is byte-identical)
#   commit:    50b4b252c7ebf36b0f82d1827cd7bf8b78b85449
#   source SHA-256 (whole notes.py at that commit):
#              135ca651b653255a420d5ce2f281886b384f4725be1f3fde07a56acd8eca3f2d
#   copied:    2026-11-24
# The lines below the two import lines are copied verbatim from that file (lines 79-92 and
# 231-243: comments and code, in source order, the function's own lazy import included); the
# two module-level imports are the only additions. Do not edit: a change in claude-session is a
# new pinned file (new name), a changed test and a reviewed allowlist, never an edit here.
# Standard library only. Importing it runs nothing but three ``re.compile`` calls and one
# ``str.maketrans``.
import re
import unicodedata

# C0 (except newline and tab), DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Line and paragraph separators: line breaks to a reader, so kept as newlines.
_SEPARATORS = str.maketrans({"\u2028": "\n", "\u2029": "\n"})
# Default_Ignorable_Code_Point characters outside category Cf (Cf is stripped separately):
# U+034F, the Hangul fillers, the Khmer inherent vowels, the Mongolian variation selectors,
# the variation selectors and their supplement, and the reserved default-ignorable ranges
# (U+2065, U+FFF0-U+FFF8, U+E0000-U+E0FFF), so a later Unicode version cannot add one.
_IGNORABLE = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2065\u3164\ufe00-\ufe0f\uffa0"
    "\ufff0-\ufff8\U000e0000-\U000e0fff]"
)
# A closing tag, also with whitespace around the slash (after NFKC and stripping).
_CLOSING = re.compile(r"<\s*/\s*(untrusted)", re.IGNORECASE)


def sanitize(output: str) -> str:
    """The command's output made safe to fence: NFKC-normalized (fullwidth and other
    compatibility forms become their plain characters), format characters (category ``Cf``),
    the other default-ignorable characters and control characters stripped, U+2028/U+2029
    turned into newlines, and only then (last, so nothing after it can assemble a tag) every
    closing-tag look-alike neutralized."""
    import unicodedata  # lazy: only hooks that show notes load it

    text = unicodedata.normalize("NFKC", output)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = _IGNORABLE.sub("", text)
    text = _CONTROL.sub("", text.translate(_SEPARATORS))
    return _CLOSING.sub(r"&lt;/\1", text)
