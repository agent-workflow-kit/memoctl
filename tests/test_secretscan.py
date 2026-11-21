"""Secret check (interface.md, Secret check): patterns, mask_env, fields, linear time, no echo.

Every secret-looking string here is obviously fake and built at runtime from parts, so no
literal in this file looks like a credential to a scanner. The environment is the fake
gateway's; the real environment is never read.
"""

from __future__ import annotations

import ast
import copy
import pickle
import re
import time
import traceback
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest

from memoctl import secretscan
from memoctl.body import BodyTextError, validate_body
from memoctl.secretscan import SecretDetected, check_secrets
from tests.fake_gateway import FakeGateway

A = "a"
UP = "A"
DASH = "-" * 5


def run(
    gw: FakeGateway,
    *,
    body: str = "ok",
    repo: str = "owner/name",
    topic: str = "topic",
    labels: tuple[str, ...] = (),
    kind: str = "unknown",
    mask_env: tuple[str, ...] = (),
) -> None:
    check_secrets(gw, mask_env, repo=repo, topic=topic, labels=labels, kind=kind, body=body)


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path)


def refusal(gw: FakeGateway, **kw: object) -> SecretDetected:
    with pytest.raises(SecretDetected) as info:
        run(gw, **kw)  # type: ignore[arg-type]
    return info.value


# name -> (positive builder, near-miss builder). Near-misses are one character short of the
# shortest form, or have the wrong lead.
CASES: dict[str, tuple[Callable[[], str], Callable[[], str]]] = {
    "private_key": (
        lambda: DASH + "BEGIN " + "RSA PRIV" + "ATE KEY" + DASH,
        lambda: DASH + "BEGIN " + "RSA PUBLIC KEY" + DASH,
    ),
    "github_token": (
        lambda: "gh" + "p_" + A * 36,
        lambda: "gh" + "p_" + A * 35,
    ),
    "gitlab_token": (
        lambda: "glp" + "at-" + A * 20,
        lambda: "glp" + "at-" + A * 19,
    ),
    "slack_token": (
        lambda: "xo" + "xb-" + "1" * 10,
        lambda: "xo" + "xb-" + "1" * 9,
    ),
    "aws_key_id": (
        lambda: "AK" + "IA" + "B" * 16,
        lambda: "AK" + "IA" + "B" * 15,
    ),
    "npm_token": (
        lambda: "np" + "m_" + A * 36,
        lambda: "np" + "m_" + A * 35,
    ),
    "anthropic_key": (
        lambda: "sk-" + "ant-" + A * 20,
        lambda: "sk-" + "ant-" + A * 19,
    ),
    "google_api_key": (
        lambda: "AI" + "za" + A * 35,
        lambda: "AI" + "za" + A * 34,
    ),
    "stripe_key": (
        lambda: "sk" + "_live_" + A * 24,
        lambda: "sk" + "_live_" + A * 23,
    ),
    "openai_key": (
        lambda: "sk-" + "proj-" + A * 20,
        lambda: "sk-" + "proj-" + A * 19,
    ),
    "jwt": (
        lambda: "ey" + "J" + A * 10 + ".ey" + "J" + A * 10 + ".sig",
        lambda: "ey" + "J" + A * 9 + ".ey" + "J" + A * 10 + ".sig",
    ),
    "slack_webhook": (
        lambda: "https://hooks." + "slack.com/services/" + "T" * 20,
        lambda: "https://hooks." + "slack.com/services/" + "T" * 19,
    ),
}


# ---------- the table ----------


def test_every_class_of_the_table_has_a_case() -> None:
    assert set(CASES) | {"mask_env"} == set(secretscan.CLASSES)
    assert secretscan.CLASSES[-1] == "mask_env"


@pytest.mark.parametrize("cls", sorted(CASES))
@pytest.mark.parametrize("where", ["prefix", "inside", "suffix"])
def test_pattern_is_found_anywhere_in_the_body(gw: FakeGateway, cls: str, where: str) -> None:
    secret = CASES[cls][0]()
    text = {
        "prefix": secret + " tail",
        "inside": f"head {secret} tail",
        "suffix": "head " + secret,
    }[where]
    err = refusal(gw, body=text)
    assert (err.cls, err.field) == (cls, "body")


@pytest.mark.parametrize("cls", sorted(CASES))
def test_near_miss_is_accepted(gw: FakeGateway, cls: str) -> None:
    run(gw, body="before " + CASES[cls][1]() + " after")


@pytest.mark.parametrize("cls", sorted(CASES))
def test_pattern_prefix_is_case_sensitive(gw: FakeGateway, cls: str) -> None:
    secret = CASES[cls][0]()
    # swap the case of the literal lead only; the tail alphabets are not what is tested
    start = next(i for i, ch in enumerate(secret) if ch.isalpha())
    swapped = secret[:start] + secret[start : start + 5].swapcase() + secret[start + 5 :]
    assert swapped != secret
    run(gw, body=swapped)


@pytest.mark.parametrize(
    "text",
    [
        DASH + "BEGIN " + "PRIVATE KEY" + DASH,
        DASH + "BEGIN " + "PGP PRIVATE KEY BLOCK" + DASH,
        DASH + "BEGIN " + "OPENSSH PRIVATE KEY" + DASH,
    ],
)
def test_private_key_header_variants(gw: FakeGateway, text: str) -> None:
    assert refusal(gw, body=text).cls == "private_key"


def test_private_key_header_needs_both_dash_runs_and_uppercase(gw: FakeGateway) -> None:
    run(gw, body="-----BEGIN " + "private key" + DASH)
    run(gw, body="-----BEGIN " + "PRIVATE KEY" + "----")  # four dashes close
    run(gw, body="----BEGIN " + "PRIVATE KEY" + DASH)


@pytest.mark.parametrize("lead", ["p", "o", "u", "s", "r"])
def test_github_token_leads(gw: FakeGateway, lead: str) -> None:
    assert refusal(gw, body="gh" + lead + "_" + A * 36).cls == "github_token"


def test_github_token_other_lead_is_accepted(gw: FakeGateway) -> None:
    run(gw, body="gh" + "x_" + A * 36)


def test_github_fine_grained_token(gw: FakeGateway) -> None:
    assert refusal(gw, body="github" + "_pat_" + "A1_" * 8).cls == "github_token"
    run(gw, body="github" + "_pat_" + A * 21)


@pytest.mark.parametrize("lead", ["AKIA", "ASIA"])
def test_aws_leads(gw: FakeGateway, lead: str) -> None:
    assert refusal(gw, body=lead + "B" * 16).cls == "aws_key_id"
    run(gw, body=lead + "b" * 16)  # lowercase tail is not a key id


def test_aws_key_id_is_not_bounded_on_the_right(gw: FakeGateway) -> None:
    assert refusal(gw, body="AKIA" + "B" * 40).cls == "aws_key_id"


def test_jwt_needs_a_non_token_character_before_the_header(gw: FakeGateway) -> None:
    jwt = CASES["jwt"][0]()
    assert refusal(gw, body="Bearer " + jwt).cls == "jwt"
    assert refusal(gw, body="(" + jwt).cls == "jwt"
    run(gw, body="x" + jwt)  # glued to a token character: not started
    run(gw, body="a-" + jwt)
    run(gw, body="a_" + jwt)


def test_jwt_after_a_dot_or_slash_is_found(gw: FakeGateway) -> None:
    assert refusal(gw, body="a." + CASES["jwt"][0]()).cls == "jwt"


def test_jwt_needs_the_second_segment_and_dot(gw: FakeGateway) -> None:
    run(gw, body="ey" + "J" + A * 10 + ".ey" + "J" + A * 10)  # no closing dot


def test_slack_webhook_charset(gw: FakeGateway) -> None:
    assert refusal(gw, body="https://hooks." + "slack.com/services/T0/B0/" + "x" * 14).cls == (
        "slack_webhook"
    )
    run(gw, body="http://hooks." + "slack.com/services/" + "T" * 20)


# ---------- order and reporting ----------


def test_first_match_in_table_order_not_text_order(gw: FakeGateway) -> None:
    text = CASES["npm_token"][0]() + " " + CASES["github_token"][0]()
    assert refusal(gw, body=text).cls == "github_token"


def test_raw_text_wins_over_normalized_text(gw: FakeGateway) -> None:
    # raw: an npm token; normalized: the fullwidth github token also becomes ASCII
    wide = "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in CASES["github_token"][0]())
    text = CASES["npm_token"][0]() + " " + wide
    assert refusal(gw, body=text).cls == "npm_token"


def test_fields_are_reported_in_spec_order(gw: FakeGateway) -> None:
    secret = CASES["github_token"][0]()
    assert refusal(
        gw, repo=secret, topic=secret, labels=(secret,), kind=secret, body=secret
    ).field == ("repo")
    assert refusal(gw, topic=secret, labels=(secret,), kind=secret, body=secret).field == "topic"
    assert refusal(gw, labels=(secret,), kind=secret, body=secret).field == "label"
    assert refusal(gw, kind=secret, body=secret).field == "kind"
    assert refusal(gw, body=secret).field == "body"


def test_each_field_is_checked(gw: FakeGateway) -> None:
    secret = CASES["slack_token"][0]()
    for field in ("repo", "topic", "kind", "body"):
        err = refusal(gw, **{field: secret})
        assert (err.cls, err.field) == ("slack_token", field)


def test_label_is_checked_as_the_whole_key_value_text(gw: FakeGateway) -> None:
    secret = CASES["github_token"][0]()
    err = refusal(gw, labels=("a=b", "k=" + secret))
    assert (err.cls, err.field) == ("github_token", "label")


def test_no_labels_and_clean_input_pass(gw: FakeGateway) -> None:
    run(gw)


def test_details_shape(gw: FakeGateway) -> None:
    err = refusal(gw, body=CASES["jwt"][0]())
    assert err.details == {"class": "jwt", "field": "body"}
    assert err.reason == "secret_detected"


# ---------- NFKC ----------


def _wide(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" and c.isalnum() else c for c in text)


@pytest.mark.parametrize("cls", sorted(CASES))
def test_fullwidth_form_is_caught_after_nfkc(gw: FakeGateway, cls: str) -> None:
    secret = CASES[cls][0]()
    wide = _wide(secret)
    assert unicodedata.normalize("NFKC", wide) == secret or cls in {"private_key"}
    if wide == secret:
        pytest.skip("no letters to widen")
    assert wide.isascii() is False
    err = refusal(gw, body="x " + wide)
    assert err.cls == cls


def test_nfkc_applies_to_the_short_fields_too(gw: FakeGateway) -> None:
    wide = _wide(CASES["npm_token"][0]())
    for field in ("repo", "topic", "kind"):
        assert refusal(gw, **{field: wide}).field == field
    assert refusal(gw, labels=(wide,)).field == "label"


def test_nfkc_ligature_and_compat_forms(gw: FakeGateway) -> None:
    # U+FF0D FULLWIDTH HYPHEN-MINUS normalizes to "-": the glpat dash
    token = "glp" + "at－" + A * 20
    assert refusal(gw, body=token).cls == "gitlab_token"


def test_nfkc_does_not_assemble_a_match_from_a_near_miss(gw: FakeGateway) -> None:
    run(gw, body=_wide(CASES["github_token"][1]()))


# ---------- mask_env ----------


def test_mask_env_value_is_refused(gw: FakeGateway) -> None:
    gw.environ["MEMO_FAKE_A"] = "correct-horse-battery"
    err = refusal(gw, body="x correct-horse-battery y", mask_env=("MEMO_FAKE_A",))
    assert (err.cls, err.field) == ("mask_env", "body")


@pytest.mark.parametrize("field", ["repo", "topic", "kind", "body"])
def test_mask_env_value_is_checked_in_every_field(gw: FakeGateway, field: str) -> None:
    gw.environ["V"] = "abcdefgh12345"
    err = refusal(gw, mask_env=("V",), **{field: "xxabcdefgh12345xx"})
    assert (err.cls, err.field) == ("mask_env", field)


def test_mask_env_value_in_a_label(gw: FakeGateway) -> None:
    gw.environ["V"] = "abcdefgh12345"
    assert refusal(gw, mask_env=("V",), labels=("k=abcdefgh12345",)).field == "label"


def test_mask_env_value_is_matched_case_sensitively(gw: FakeGateway) -> None:
    gw.environ["V"] = "AbCdEfGh1234"
    run(gw, body="abcdefgh1234", mask_env=("V",))


def test_mask_env_minimum_length_is_eight_code_points(gw: FakeGateway) -> None:
    gw.environ["SHORT"] = "x" * 7
    run(gw, body="xxxxxxx and more xxxxxxxx", mask_env=("SHORT",))
    gw.environ["EXACT"] = "y" * 8
    assert refusal(gw, body="yyyyyyyy", mask_env=("EXACT",)).cls == "mask_env"


def test_mask_env_length_counts_code_points_not_bytes(gw: FakeGateway) -> None:
    gw.environ["SHORT"] = "é" * 7  # 14 bytes, 7 code points
    run(gw, body="é" * 7, mask_env=("SHORT",))
    gw.environ["LONG"] = "é" * 8
    assert refusal(gw, body="é" * 8, mask_env=("LONG",)).cls == "mask_env"


def test_mask_env_value_of_astral_code_points(gw: FakeGateway) -> None:
    gw.environ["V"] = "\U0001f600" * 8
    assert refusal(gw, body="a" + "\U0001f600" * 8, mask_env=("V",)).cls == "mask_env"
    gw.environ["V"] = "\U0001f600" * 7
    run(gw, body="\U0001f600" * 7, mask_env=("V",))


def test_unset_and_empty_variables_never_match(gw: FakeGateway) -> None:
    gw.environ["EMPTY"] = ""
    gw.environ["SPACES"] = "   "  # short: skipped
    run(gw, body="anything at all", mask_env=("UNSET", "EMPTY", "SPACES"))


def test_a_long_whitespace_value_is_matched_only_as_that_exact_run(gw: FakeGateway) -> None:
    gw.environ["WS"] = " " * 8
    run(gw, body="a b c  d", mask_env=("WS",))


def test_value_match_after_nfkc_of_the_text(gw: FakeGateway) -> None:
    gw.environ["V"] = "abcdefgh12345"
    err = refusal(gw, body=_wide("abcdefgh12345"), mask_env=("V",))
    assert err.cls == "mask_env"


def test_value_is_compared_as_written_against_both_forms(gw: FakeGateway) -> None:
    # the value holds a fullwidth form; the raw text holds the same: matched raw
    value = _wide("abcdefgh12345")
    gw.environ["V"] = value
    assert refusal(gw, body="x" + value, mask_env=("V",)).cls == "mask_env"


def test_value_with_a_newline_matches_the_same_lines(gw: FakeGateway) -> None:
    gw.environ["V"] = "first-line\nsecond-line"
    assert refusal(gw, body="a first-line\nsecond-line b", mask_env=("V",)).cls == "mask_env"
    run(gw, body="first-line second-line", mask_env=("V",))


def test_patterns_win_over_mask_env_in_table_order(gw: FakeGateway) -> None:
    gw.environ["V"] = "abcdefgh12345"
    text = "abcdefgh12345 " + CASES["npm_token"][0]()
    assert refusal(gw, body=text, mask_env=("V",)).cls == "npm_token"


def test_first_mask_name_that_matches_is_enough(gw: FakeGateway) -> None:
    gw.environ["A"] = "aaaaaaaa1"
    gw.environ["B"] = "bbbbbbbb2"
    assert refusal(gw, body="bbbbbbbb2", mask_env=("A", "B")).cls == "mask_env"


def test_mask_env_reads_only_the_gateway(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMO_REAL_ENV_PROBE", "must-never-be-read-12345")
    run(gw, body="must-never-be-read-12345", mask_env=("MEMO_REAL_ENV_PROBE",))


def test_mask_env_value_is_never_in_the_error(gw: FakeGateway) -> None:
    gw.environ["V"] = "distinctive-value-zq81"
    err = refusal(gw, body="zz distinctive-value-zq81 zz", mask_env=("V",))
    assert_clean(err, ["distinctive-value-zq81", "zq81"])
    assert "MEMO" not in str(err)


def test_mask_env_with_many_unset_names_is_cheap(gw: FakeGateway) -> None:
    names = tuple(f"NAME_{i}" for i in range(32))
    run(gw, body="a" * 65536, mask_env=names)


# ---------- never echoed ----------


def assert_clean(err: SecretDetected, needles: list[str]) -> None:
    rendered = [
        str(err),
        repr(err),
        repr(err.args),
        str(err.details),
        err.message,
        "".join(traceback.format_exception(err)),
        repr(pickle.loads(pickle.dumps(err)).__dict__),
        repr(copy.deepcopy(err).__dict__),
        repr(err.__dict__),
    ]
    for text in rendered:
        for needle in needles:
            assert needle not in text
    assert err.__cause__ is None
    assert err.__context__ is None


@pytest.mark.parametrize("cls", sorted(CASES))
def test_secret_is_never_in_the_error(gw: FakeGateway, cls: str) -> None:
    secret = CASES[cls][0]()
    err = refusal(gw, body="note: " + secret + " end")
    needles = [secret, secret[len(secret) // 2 :], secret[:12]]
    assert_clean(err, needles)


def test_error_has_no_chained_exception(gw: FakeGateway) -> None:
    err = refusal(gw, body=CASES["jwt"][0]())
    assert err.__cause__ is None and err.__context__ is None


def test_error_survives_pickle_and_copy(gw: FakeGateway) -> None:
    err = refusal(gw, topic=CASES["jwt"][0]())
    for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
        assert isinstance(clone, SecretDetected)
        assert clone.details == err.details
        assert str(clone) == str(err)


def test_error_rejects_an_unknown_class_or_field() -> None:
    with pytest.raises(ValueError):
        SecretDetected("not_a_class", "body")
    with pytest.raises(ValueError):
        SecretDetected("jwt", "not_a_field")


def test_no_logging_of_the_input(gw: FakeGateway, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(0)
    refusal(gw, body=CASES["npm_token"][0]())
    run(gw, body="clean")
    assert caplog.records == []


def test_module_does_not_log_print_or_warn() -> None:
    tree = ast.parse(Path(secretscan.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"logging", "warnings", "traceback", "sys"}
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"print", "repr", "format"}


# ---------- order against body validation ----------


def test_secret_split_by_a_zero_width_character_is_refused_by_body_validation_first() -> None:
    secret = CASES["github_token"][0]()
    split = secret[:10] + "​" + secret[10:]
    with pytest.raises(BodyTextError) as info:
        validate_body(split.encode(), 8192)
    assert info.value.cls == "invisible"


def test_secret_split_by_a_newline_is_not_detected_by_design(gw: FakeGateway) -> None:
    secret = CASES["github_token"][0]()
    run(gw, body=secret[:10] + "\n" + secret[10:])  # residual risk, documented in the interface


def test_secret_split_by_a_control_character_is_refused_by_body_validation_first() -> None:
    secret = CASES["aws_key_id"][0]()
    with pytest.raises(BodyTextError):
        validate_body((secret[:6] + "\x00" + secret[6:]).encode(), 8192)


# ---------- linear time ----------

MB = 1 << 20


def timed(gw: FakeGateway, text: str, **kw: object) -> float:
    start = time.perf_counter()
    run(gw, body=text, **kw)  # type: ignore[arg-type]
    return time.perf_counter() - start


ADVERSARIAL: dict[str, Callable[[int], str]] = {
    "jwt_prefix_repeated": lambda n: ("ey" + "J" + A * 12) * (n // 15),
    "jwt_prefix_spaced": lambda n: ("ey" + "J" + A * 12 + " ") * (n // 16),
    "jwt_one_long_run": lambda n: "ey" + "J" + A * n,
    "jwt_first_segment_dots": lambda n: ("ey" + "J" + A * 9 + ".") * (n // 13),
    "jwt_second_segment_no_dot": lambda n: "ey" + "J" + A * 12 + ".ey" + "J" + A * n,
    "key_header_repeated": lambda n: (DASH + "BEGIN ") * (n // 11),
    "key_header_long_label": lambda n: DASH + "BEGIN " + "A" * n,
    "key_header_spaces": lambda n: DASH + "BEGIN " + "A " * (n // 2),
    "key_header_near_miss_repeated": lambda n: (DASH + "BEGIN " + "PRIVATE KE ") * (n // 22),
    "github_short_repeated": lambda n: ("gh" + "p_" + A * 35 + " ") * (n // 39),
    "github_pat_short": lambda n: ("github" + "_pat_" + A * 21 + " ") * (n // 33),
    "gitlab_short_repeated": lambda n: ("glp" + "at-" + A * 19 + " ") * (n // 29),
    "slack_short_repeated": lambda n: ("xo" + "xb-" + "1" * 9 + " ") * (n // 15),
    "aws_short_repeated": lambda n: ("AK" + "IA" + "B" * 15 + " ") * (n // 21),
    "npm_short_repeated": lambda n: ("np" + "m_" + A * 35 + " ") * (n // 39),
    "anthropic_short_repeated": lambda n: ("sk-" + "ant-" + A * 19 + " ") * (n // 27),
    "google_short_repeated": lambda n: ("AI" + "za" + A * 34 + " ") * (n // 40),
    "stripe_short_repeated": lambda n: ("sk" + "_live_" + A * 23 + " ") * (n // 31),
    "openai_short_repeated": lambda n: ("sk-" + "proj-" + A * 19 + " ") * (n // 28),
    "webhook_short_repeated": lambda n: (
        ("https://hooks." + "slack.com/services/" + "T" * 19 + " ") * (n // 53)
    ),
    "dashes": lambda n: "-" * n,
    "token_chars": lambda n: "aZ0_-" * (n // 5),
    "slashes": lambda n: ("https://hooks." + "slack.com/services/" + "/" * 19 + " ") * (n // 53),
    "slashes_long": lambda n: "https://hooks." + "slack.com/services/" + "/" * 19 + "-" * n,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_input_of_a_megabyte_is_accepted_fast(gw: FakeGateway, name: str) -> None:
    text = ADVERSARIAL[name](MB)
    assert timed(gw, text) < 2.0


def test_large_mask_env_value_over_a_large_body(gw: FakeGateway) -> None:
    gw.environ["BIG"] = A * 4096 + "b"
    assert timed(gw, A * MB, mask_env=("BIG",)) < 2.0


def test_nfkc_of_a_megabyte_body_is_bounded(gw: FakeGateway) -> None:
    assert timed(gw, "ﬁ" * (MB // 3)) < 2.0


def test_patterns_have_no_nested_quantifiers() -> None:
    # a cheap tripwire: no group followed by a quantifier in any pattern source
    for pattern in secretscan.PATTERN_SOURCES:
        assert not re.search(r"\)[*+{]", pattern), pattern


def test_every_pattern_is_compiled_without_ignorecase() -> None:
    for _cls, compiled in secretscan.PATTERNS:
        assert not compiled.flags & re.IGNORECASE


# ---------- hostile input ----------


@pytest.mark.parametrize(
    "text",
    ["", "\n", "\t", "a", "é", "\U0010ffff", "x" * 100],
)
def test_odd_clean_text_is_accepted(gw: FakeGateway, text: str) -> None:
    run(gw, body=text, topic=text, kind="unknown")


def test_lone_surrogate_in_text_does_not_crash(gw: FakeGateway) -> None:
    run(gw, body="a\udcff b")  # a surrogateescape leftover from an environment value


def test_lone_surrogate_in_an_env_value_does_not_crash(gw: FakeGateway) -> None:
    gw.environ["V"] = "abcdefg\udcff1"
    run(gw, body="nothing", mask_env=("V",))
    assert refusal(gw, body="abcdefg\udcff1", mask_env=("V",)).cls == "mask_env"


def test_injection_shaped_text_is_just_text(gw: FakeGateway) -> None:
    run(gw, body="'; DROP TABLE notes; -- $(rm -rf /) {{7*7}} %s %n <script>")


def test_repeated_calls_are_independent(gw: FakeGateway) -> None:
    for _ in range(3):
        assert refusal(gw, body=CASES["jwt"][0]()).cls == "jwt"
        run(gw, body="clean")


def test_nothing_is_cached_between_calls_when_the_environment_changes(gw: FakeGateway) -> None:
    gw.environ["V"] = "abcdefgh12345"
    assert refusal(gw, body="abcdefgh12345", mask_env=("V",)).cls == "mask_env"
    gw.environ["V"] = "something-else-entirely"
    run(gw, body="abcdefgh12345", mask_env=("V",))


def test_raising_frame_holds_only_class_and_field(gw: FakeGateway) -> None:
    sentinel = "sent" + "inel-" + "env-value-1"
    gw.environ["V"] = sentinel
    err = refusal(gw, body="x " + sentinel, mask_env=("V",))
    exc = traceback.TracebackException(type(err), err, err.__traceback__, capture_locals=True)
    tb = err.__traceback__
    assert tb is not None
    while tb.tb_next is not None:
        tb = tb.tb_next
    assert set(tb.tb_frame.f_locals) == {"cls", "field"}
    assert sentinel not in repr(tb.tb_frame.f_locals)
    last = exc.stack[-1]
    assert last.locals is not None
    assert sentinel not in "".join(last.locals.values())
    walker = err.__traceback__
    while walker is not None:
        assert "values" not in walker.tb_frame.f_locals
        walker = walker.tb_next
