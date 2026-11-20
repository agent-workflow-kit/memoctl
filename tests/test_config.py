"""Config loading (interface.md, Config): home, drop-ins, strict YAML, ranges, overrides."""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any

import pytest

from memoctl.config import Config, ConfigError, load_config, resolve_home
from memoctl.gateway import GatewayError
from tests.fake_gateway import FakeGateway


@pytest.fixture
def gw(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path / "root")


def cdir(gw: FakeGateway) -> Path:
    d = gw.home() / ".config" / "memoctl"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o755)
    return d


def put(gw: FakeGateway, name: str, text: str | bytes, mode: int = 0o644) -> Path:
    base = cdir(gw)
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    path.chmod(mode)
    return path


def bad(gw: FakeGateway) -> str:
    with pytest.raises(ConfigError) as info:
        load_config(gw)
    assert info.value.code == "config_invalid"
    assert str(info.value) == "invalid configuration"
    return info.value.field


def test_defaults_when_nothing_exists(gw: FakeGateway) -> None:
    c = load_config(gw)
    assert c.kind_env == "CSESSION_KIND"
    assert c.mask_env == []
    assert c.retention.max_age == 30 * 86400
    assert c.limits.window == 600
    assert c.limits.max_body_bytes == 8192
    assert c.context.deadline == 2
    assert c.context.author_kinds == ["implement", "draft-ticket", "audit", "improve"]
    assert c.context.max_kind_share == 0.5
    assert c.repos == {}


def test_defaults_when_dir_exists_but_no_file_and_drop_ins_still_read(gw: FakeGateway) -> None:
    cdir(gw)
    assert load_config(gw).limits.total == 5000
    put(gw, "config.d/a.yaml", "limits: {total: 700}\n")
    assert load_config(gw).limits.total == 700


def test_env_and_home_variables_are_never_read(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    (other / "memoctl").mkdir(parents=True)
    (other / "memoctl" / "config.yaml").write_text("limits: {total: 3}\n")
    gw.environ.update({"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(other)})
    monkeypatch.setenv("MEMOCTL_LIMITS_TOTAL", "3")
    assert load_config(gw).limits.total == 5000


# --- home ----------------------------------------------------------------------------------


def test_home_ok(gw: FakeGateway) -> None:
    assert resolve_home(gw) == gw.home()


def test_home_lookup_failure(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Path:
        raise GatewayError("x")

    monkeypatch.setattr(gw, "home", boom)
    assert bad(gw) == "home"


@pytest.mark.parametrize("value", ["", "relative/dir", "."])
def test_home_empty_or_relative(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(gw, "home", lambda: Path(value))
    assert bad(gw) == "home"


def test_home_missing_or_file(gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "home", lambda: gw.root / "nope")
    assert bad(gw) == "home"
    f = gw.root / "afile"
    f.write_text("x")
    monkeypatch.setattr(gw, "home", lambda: f)
    assert bad(gw) == "home"


# --- files, permissions, symlinks -------------------------------------------------------------


def test_file_values_load(gw: FakeGateway) -> None:
    put(
        gw,
        "config.yaml",
        "kind_env: MY_KIND\nmask_env: [A, B_1]\nretention: {max_age: 7d}\n"
        "limits: {window: 90, max_labels: 0}\ncontext: {deadline: 5s, max_kind_share: 1}\n",
    )
    c = load_config(gw)
    assert (c.kind_env, c.mask_env) == ("MY_KIND", ["A", "B_1"])
    assert c.retention.max_age == 7 * 86400
    assert (c.limits.window, c.limits.max_labels) == (90, 0)
    assert (c.context.deadline, c.context.max_kind_share) == (5, 1)


def test_empty_and_comment_only_files_are_empty_mappings(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "")
    put(gw, "config.d/a.yaml", "# nothing\n")
    assert load_config(gw).limits.total == 5000


def test_symlinked_config_file_is_refused(gw: FakeGateway) -> None:
    real = gw.home() / "real.yaml"
    real.write_text("limits: {total: 9}\n")
    (cdir(gw) / "config.yaml").symlink_to(real)
    assert bad(gw).endswith("config.yaml")


def test_symlinked_drop_in_is_refused_even_when_target_is_fine(gw: FakeGateway) -> None:
    real = gw.home() / "real.yaml"
    real.write_text("limits: {total: 9}\n")
    put(gw, "config.d/x.yaml", "")
    (cdir(gw) / "config.d" / "x.yaml").unlink()
    (cdir(gw) / "config.d" / "a.yaml").symlink_to(real)
    assert bad(gw).endswith("config.d/a.yaml")


def test_symlinked_config_dir_and_drop_in_dir_are_refused(gw: FakeGateway) -> None:
    real = gw.home() / "realdir"
    real.mkdir()
    (gw.home() / ".config").mkdir()
    (gw.home() / ".config" / "memoctl").symlink_to(real)
    assert bad(gw).endswith(".config/memoctl")


def test_symlinked_drop_in_dir_is_refused(gw: FakeGateway) -> None:
    real = gw.home() / "realdir"
    real.mkdir()
    cdir(gw)
    (cdir(gw) / "config.d").symlink_to(real)
    assert bad(gw).endswith("config.d")


def test_drop_in_that_is_a_directory_or_fifo_is_refused(gw: FakeGateway) -> None:
    put(gw, "config.d/x.yaml", "")
    (cdir(gw) / "config.d" / "x.yaml").unlink()
    (cdir(gw) / "config.d" / "a.yaml").mkdir()
    assert bad(gw).endswith("a.yaml")
    (cdir(gw) / "config.d" / "a.yaml").rmdir()
    os.mkfifo(cdir(gw) / "config.d" / "a.yaml")
    assert bad(gw).endswith("a.yaml")


def test_config_file_fifo_is_refused_without_blocking(gw: FakeGateway) -> None:
    os.mkfifo(cdir(gw) / "config.yaml")
    assert bad(gw).endswith("config.yaml")


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o620, 0o602])
def test_group_or_world_writable_file_is_refused(gw: FakeGateway, mode: int) -> None:
    put(gw, "config.yaml", "{}\n", mode=mode)
    assert bad(gw).endswith("config.yaml")


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o400])
def test_non_writable_modes_are_accepted(gw: FakeGateway, mode: int) -> None:
    put(gw, "config.yaml", "{}\n", mode=mode)
    load_config(gw)


def test_group_writable_drop_in_is_refused(gw: FakeGateway) -> None:
    put(gw, "config.d/a.yaml", "{}\n", mode=0o660)
    assert bad(gw).endswith("a.yaml")


def test_group_or_world_writable_directories_are_refused(gw: FakeGateway) -> None:
    cdir(gw).chmod(0o775)
    assert bad(gw).endswith("memoctl")
    cdir(gw).chmod(0o755)
    put(gw, "config.d/x.yaml", "")
    (cdir(gw) / "config.d").chmod(0o777)
    assert bad(gw).endswith("config.d")


def test_foreign_owner_is_refused_for_file_and_directories(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "{}\n")
    put(gw, "config.d/a.yaml", "{}\n")
    gw.uid_value = os.getuid() + 1
    assert bad(gw).endswith("memoctl")
    gw.uid_value = os.getuid()
    load_config(gw)


def test_unreadable_file_is_config_invalid(gw: FakeGateway) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads everything")
    put(gw, "config.yaml", "{}\n", mode=0o000)
    assert bad(gw).endswith("config.yaml")


def test_oversize_file_is_refused_and_exact_limit_is_accepted(gw: FakeGateway) -> None:
    pad = b"# " + b"x" * (65536 - 3) + b"\n"
    assert len(pad) == 65536
    put(gw, "config.yaml", pad)
    load_config(gw)
    put(gw, "config.yaml", pad + b"#")
    assert bad(gw).endswith("config.yaml")


def test_error_field_is_bounded_and_printable(
    gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(ConfigError("a" * 9000).field) == 4096
    assert ConfigError("a\nb\x00").field == "a?b?"


# --- drop-ins ----------------------------------------------------------------------------------


def test_drop_in_names_are_filtered_by_grammar(gw: FakeGateway) -> None:
    put(gw, "config.d/ok.yaml", "limits: {total: 1100}\n")
    for name in (".hidden.yaml", "a.yml", "a.yaml.bak", "a.yaml~", "b c.yaml", "é.yaml", "a.YAML"):
        put(gw, f"config.d/{name}", "not: [valid\n")  # would fail if read
    assert load_config(gw).limits.total == 1100


def test_drop_ins_merge_in_sorted_order_over_config_yaml(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "limits: {total: 1, live_per_repo: 1}\nmask_env: [A]\n")
    put(gw, "config.d/20-b.yaml", "limits: {total: 3}\n")
    put(gw, "config.d/10-a.yaml", "limits: {total: 2, window: 120}\nmask_env: [B, C]\n")
    put(gw, "config.d/B.yaml", "limits: {window: 61}\n")  # uppercase sorts before lowercase
    put(gw, "config.d/_z.yaml", "limits: {window: 300}\n")  # '_' sorts after uppercase letters
    c = load_config(gw)
    assert c.limits.total == 3  # 20-b after 10-a
    assert c.limits.live_per_repo == 1  # maps merge recursively
    assert c.limits.window == 300  # order: 10-a, 20-b, B, _z
    assert c.mask_env == ["B", "C"]  # a list replaces


def test_drop_in_can_empty_a_list(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "mask_env: [A]\ncontext: {author_kinds: [x]}\n")
    put(gw, "config.d/a.yaml", "mask_env: []\ncontext: {author_kinds: []}\n")
    c = load_config(gw)
    assert c.mask_env == [] and c.context.author_kinds == []


def test_drop_in_may_set_kind_env_and_mask_env(gw: FakeGateway) -> None:
    put(gw, "config.d/awkit.yaml", "kind_env: OTHER_KIND\nmask_env: [TOKEN]\n")
    c = load_config(gw)
    assert (c.kind_env, c.mask_env) == ("OTHER_KIND", ["TOKEN"])


def test_too_many_drop_ins_are_refused(gw: FakeGateway) -> None:
    for i in range(257):
        put(gw, f"config.d/{i:04}.yaml", "")
    assert bad(gw).endswith("config.d")


def test_bad_drop_in_names_its_file(gw: FakeGateway) -> None:
    put(gw, "config.d/a.yaml", "- list\n")
    assert bad(gw).endswith("config.d/a.yaml")


# --- strict YAML --------------------------------------------------------------------------------

HOSTILE = {
    "duplicate key": "limits: {total: 1}\nlimits: {total: 2}\n",
    "nested duplicate": "limits: {total: 1, total: 2}\n",
    "anchor": "x: &a 1\n",
    "alias": "limits: &l {total: 1}\ncontext: *l\n",
    "merge key": "limits: &l {total: 1}\n<<: *l\n",
    "inline merge": "<<: {kind_env: A}\n",
    "python tag": "kind_env: !!python/object/apply:os.system ['id']\n",
    "custom tag": "kind_env: !foo bar\n",
    "binary tag": "kind_env: !!binary aGk=\n",
    "set tag": "kind_env: !!set {a}\n",
    "int key": "1: x\n",
    "complex key": "? [a, b]\n: x\n",
    "list root": "- a\n",
    "scalar root": "just text\n",
    "number root": "5\n",
    "multi doc": "a: 1\n---\nb: 2\n",
    "unclosed": "limits: {total: [\n",
    "tab indent": "limits:\n\ttotal: 1\n",
    "NUL": "limits: {total: 1}\x00\n",
    "bell": "kind_env: A\x07\n",
}


@pytest.mark.parametrize("text", HOSTILE.values(), ids=HOSTILE.keys())
def test_hostile_yaml_is_config_invalid_and_never_leaks(gw: FakeGateway, text: str) -> None:
    put(gw, "config.yaml", text)
    assert bad(gw).endswith("config.yaml")


def test_non_utf8_is_refused(gw: FakeGateway) -> None:
    put(gw, "config.yaml", b"kind_env: \xff\xfe\n")
    assert bad(gw).endswith("config.yaml")


def test_billion_laughs_is_refused_quickly(gw: FakeGateway) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 9):
        refs = ", ".join([f"*a{i - 1}"] * 9)
        lines.append(f"a{i}: &a{i} [{refs}]")
    put(gw, "config.yaml", "\n".join(lines) + "\n")
    assert bad(gw).endswith("config.yaml")


def test_deep_nesting_is_refused_not_a_crash(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "[" * 20000 + "]" * 20000)
    assert bad(gw).endswith("config.yaml")
    put(gw, "config.yaml", "a: " + "{b: " * 30 + "1" + "}" * 30 + "\n")
    assert bad(gw).endswith("config.yaml")


def test_huge_integer_is_refused(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "limits: {total: " + "9" * 20000 + "}\n")
    assert bad(gw)  # a ConfigError, not a crash


def test_yaml_scalar_rules(gw: FakeGateway) -> None:
    # no, off, 010, 1e3, .nan, dates stay strings and fail the type check of an integer key
    for literal in (
        "no",
        "off",
        "yes",
        "on",
        "010",
        "1e3",
        ".nan",
        ".inf",
        "0x10",
        "1_000",
        "2020-01-01",
    ):
        put(gw, "config.yaml", f"limits: {{total: {literal}}}\n")
        assert bad(gw) == "limits.total", literal


def test_explicit_tags_cannot_bypass_the_scalar_rules(gw: FakeGateway) -> None:
    for text in ("limits: {total: !!int 0x10}\n", "limits: {total: !!bool yes}\n"):
        put(gw, "config.yaml", text)
        assert bad(gw).endswith("config.yaml")
    put(gw, "config.yaml", "kind_env: !!str A\n")
    assert load_config(gw).kind_env == "A"


def test_true_and_false_are_booleans_so_fail_int_keys(gw: FakeGateway) -> None:
    for literal in ("true", "false"):
        put(gw, "config.yaml", f"limits: {{max_labels: {literal}}}\n")
        assert bad(gw) == "limits.max_labels"


def test_null_values_fail_the_type_check(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "limits:\n  total:\n")
    assert bad(gw) == "limits.total"
    put(gw, "config.yaml", "limits: ~\n")
    assert bad(gw) == "limits"


# --- types and ranges ----------------------------------------------------------------------------

INT_KEYS = {
    "limits.max_body_bytes": (256, 65536),
    "limits.max_labels": (0, 8),
    "limits.live_per_repo": (1, 100000),
    "limits.total": (1, 1000000),
    "limits.adds_per_window": (1, 10000),
    "limits.adds_per_window_total": (1, 100000),
    "context.max_chars": (1000, 9000),
    "context.max_bytes": (1000, 65536),
    "context.max_notes": (1, 100),
    "context.max_per_topic": (1, 20),
    "context.note_max_chars": (100, 8192),
}


def _yaml_for(key: str, value: object) -> str:
    section, name = key.split(".")
    return f"{section}: {{{name}: {value}}}\n"


@pytest.mark.parametrize("key", INT_KEYS)
def test_integer_key_boundaries(gw: FakeGateway, key: str) -> None:
    low, high = INT_KEYS[key]
    # keep the cross-key checks out of the way for the limits that have them
    # companions keep the cross-key checks out of the way
    companion = {
        "limits.live_per_repo": "total: 1000000",
        "limits.total": "live_per_repo: 1",
        "limits.adds_per_window": "adds_per_window_total: 100000",
        "limits.adds_per_window_total": "adds_per_window: 1",
    }.get(key)
    for value, ok in ((low, True), (high, True), (low - 1, False), (high + 1, False)):
        text = _yaml_for(key, value)
        if companion:
            text = text.replace("}", f", {companion}}}")
        put(gw, "config.yaml", text)
        if ok:
            load_config(gw)
        else:
            assert bad(gw) == key


@pytest.mark.parametrize("key", INT_KEYS)
@pytest.mark.parametrize("literal", ["8000.0", "true", "false", '"8000"', "[8000]", "{a: 1}"])
def test_integer_keys_accept_only_integers(gw: FakeGateway, key: str, literal: str) -> None:
    put(gw, "config.yaml", _yaml_for(key, literal))
    assert bad(gw) == key


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0.1", True),
        ("1", True),
        ("1.0", True),
        ("0.5", True),
        ("0.09", False),
        ("1.01", False),
        ("0", False),
        ("-0.5", False),
        ("2", False),
        ("true", False),
        ('"0.5"', False),
    ],
)
def test_max_kind_share(gw: FakeGateway, value: str, ok: bool) -> None:
    put(gw, "config.yaml", f"context: {{max_kind_share: {value}}}\n")
    if ok:
        assert load_config(gw).context.max_kind_share == float(value)
    else:
        assert bad(gw) == "context.max_kind_share"


def test_nan_and_infinite_shares_cannot_pass() -> None:
    for v in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(Exception):  # noqa: B017, PT011
            Config.model_validate({"context": {"max_kind_share": v}})


DURATIONS = {
    "retention.max_age": (3600, 365 * 86400),
    "limits.window": (60, 86400),
    "context.deadline": (1, 10),
}


@pytest.mark.parametrize("key", DURATIONS)
def test_duration_keys(gw: FakeGateway, key: str) -> None:
    low, high = DURATIONS[key]
    for value, ok in (
        (low, True),
        (high, True),
        (low - 1, False),
        (high + 1, False),
        (f'"{low}s"', True),
        (f'"{high}s"', True),
        (f'"{low - 1}"', False),
        (f'"{high}.5s"', False),
        (f'"{low - 1}.1s"', True if low - 1 + 1 >= low else False),
        ("true", False),
        ("1.5", False),
        ('"abc"', False),
        ('""', False),
        ('"1 h"', False),
        ('"-5s"', False),
        ("[1]", False),
        ('"1e3"', False),
    ):
        put(gw, "config.yaml", _yaml_for(key, value))
        if ok:
            load_config(gw)
        else:
            assert bad(gw) == key, value


def test_duration_strings_round_up_before_the_range_check(gw: FakeGateway) -> None:
    put(gw, "config.yaml", 'limits: {window: "59.5s"}\n')
    assert load_config(gw).limits.window == 60
    put(gw, "config.yaml", 'retention: {max_age: "0.5d"}\n')
    assert load_config(gw).retention.max_age == 43200


def test_duration_unit_arithmetic(gw: FakeGateway) -> None:
    put(gw, "config.yaml", 'limits: {window: 2h}\ncontext: {deadline: "2.5"}\n')
    c = load_config(gw)
    assert c.limits.window == 7200 and c.context.deadline == 3


def test_kind_env_and_mask_env_grammar(gw: FakeGateway) -> None:
    for text, field in (
        ("kind_env: lower\n", "kind_env"),
        ("kind_env: 1A\n", "kind_env"),
        ("kind_env: " + "A" * 65 + "\n", "kind_env"),
        ('kind_env: "A\\n"\n', "kind_env"),
        ("kind_env: [A]\n", "kind_env"),
        ("mask_env: [a]\n", "mask_env"),
        ("mask_env: A\n", "mask_env"),
        ('mask_env: ["A\\n"]\n', "mask_env"),
        ("mask_env: [" + ", ".join(f"A{i}" for i in range(33)) + "]\n", "mask_env"),
    ):
        put(gw, "config.yaml", text)
        assert bad(gw) == field, text
    put(
        gw,
        "config.yaml",
        "kind_env: " + "A" * 64 + "\nmask_env: [" + ", ".join(f"A{i}" for i in range(32)) + "]\n",
    )
    load_config(gw)


def test_author_kinds_rules(gw: FakeGateway) -> None:
    for text in (
        "context: {author_kinds: [Implement]}\n",
        "context: {author_kinds: [a, a]}\n",
        "context: {author_kinds: [a b]}\n",
        "context: {author_kinds: x}\n",
        "context: {author_kinds: [1]}\n",
        "context: {author_kinds: [" + ", ".join(f"k{i}" for i in range(65)) + "]}\n",
    ):
        put(gw, "config.yaml", text)
        assert bad(gw) == "context.author_kinds", text
    put(gw, "config.yaml", "context: {author_kinds: [unknown, my-kind_2]}\n")
    assert load_config(gw).context.author_kinds == ["unknown", "my-kind_2"]


def test_cross_key_checks_global(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "limits: {live_per_repo: 11, total: 10}\n")
    assert bad(gw) == "limits.live_per_repo"
    put(gw, "config.yaml", "limits: {live_per_repo: 10, total: 10}\n")
    load_config(gw)
    put(gw, "config.yaml", "limits: {adds_per_window: 61}\n")
    assert bad(gw) == "limits.adds_per_window"
    put(gw, "config.yaml", "limits: {adds_per_window: 60}\n")
    load_config(gw)


def test_cross_key_check_runs_on_the_merged_result(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "limits: {total: 5}\n")
    put(gw, "config.d/a.yaml", "limits: {live_per_repo: 6}\n")
    assert bad(gw) == "limits.live_per_repo"


def test_defaults_alone_satisfy_cross_checks_after_lowering_total_with_live(
    gw: FakeGateway,
) -> None:
    put(gw, "config.yaml", "limits: {total: 100}\n")  # live_per_repo default 200 > 100
    assert bad(gw) == "limits.live_per_repo"


# --- unknown keys ---------------------------------------------------------------------------------


def test_unknown_keys_are_refused_and_named(gw: FakeGateway) -> None:
    for text, field in (
        ("bogus: 1\n", "bogus"),
        ("limits: {bogus: 1}\n", "limits.bogus"),
        ("context: {bogus: 1}\n", "context.bogus"),
        ("retention: {bogus: 1}\n", "retention.bogus"),
        ("MEMOCTL_X: 1\n", "MEMOCTL_X"),
    ):
        put(gw, "config.yaml", text)
        assert bad(gw) == field


def test_unknown_key_with_hostile_name_is_not_echoed(gw: FakeGateway) -> None:
    secret = "sk-ABCDEF1234567890 secret value"
    put(gw, "config.yaml", f"limits:\n  '{secret}': 1\n")
    field = bad(gw)
    assert field == "limits"
    assert "sk-" not in field
    put(gw, "config.yaml", "limits:\n  '" + "k" * 300 + "': 1\n")
    assert bad(gw) == "limits"


def test_unknown_key_is_refused_before_its_value_is_examined(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "bogus: {a: [1, 2, {b: c}]}\n")
    assert bad(gw) == "bogus"


def test_error_never_contains_the_value(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "kind_env: TOPSECRETvalue_lower\nlimits: {total: sekrit}\n")
    with pytest.raises(ConfigError) as info:
        load_config(gw)
    text = repr(info.value) + str(info.value) + info.value.field
    assert "sekrit" not in text and "TOPSECRET" not in text
    assert info.value.__cause__ is None or "sekrit" not in str(info.value.__cause__)


def test_config_error_pickles() -> None:
    err = pickle.loads(pickle.dumps(ConfigError("limits.total")))
    assert err.field == "limits.total"


def test_deadline_is_global_only(gw: FakeGateway) -> None:
    put(gw, "config.yaml", "repos:\n  github.com/o/r:\n    context: {deadline: 3}\n")
    assert bad(gw) == "repos.github.com/o/r.context.deadline"


# --- per-repo overrides -----------------------------------------------------------------------------  # noqa: E501

REPO = "github.com/o/r"


def test_override_merges_over_global_and_lists_replace(gw: FakeGateway) -> None:
    put(
        gw,
        "config.yaml",
        "limits: {max_body_bytes: 1000, live_per_repo: 50}\n"
        "context: {author_kinds: [a, b]}\n"
        f"repos:\n  {REPO}:\n    retention: {{max_age: 2d}}\n"
        "    limits: {max_body_bytes: 4000, adds_per_window: 5}\n"
        "    context: {author_kinds: [c]}\n",
    )
    c = load_config(gw)
    eff = c.for_repo(REPO)
    assert eff.retention.max_age == 2 * 86400
    assert eff.limits.max_body_bytes == 4000
    assert eff.limits.adds_per_window == 5
    assert eff.limits.live_per_repo == 50  # not overridden: global
    assert eff.context.author_kinds == ["c"]
    assert eff.context.max_chars == 8000
    assert eff.repos == {}
    # the global config is untouched
    assert c.limits.max_body_bytes == 1000 and c.context.author_kinds == ["a", "b"]


def test_empty_override_list_replaces(gw: FakeGateway) -> None:
    put(gw, "config.yaml", f"repos:\n  {REPO}:\n    context: {{author_kinds: []}}\n")
    assert load_config(gw).for_repo(REPO).context.author_kinds == []


def test_repo_without_override_gets_global(gw: FakeGateway) -> None:
    put(
        gw,
        "config.yaml",
        f"limits: {{total: 7700}}\nrepos:\n  {REPO}:\n    limits: {{live_per_repo: 3}}\n",
    )
    c = load_config(gw)
    assert c.for_repo("github.com/x/y").limits.total == 7700
    assert c.for_repo("github.com/x/y").limits == c.limits
    # no pattern matching: a prefix or case variant is a different repo
    assert c.for_repo(REPO + "/sub").limits.live_per_repo == c.limits.live_per_repo


def test_override_cannot_set_other_keys(gw: FakeGateway) -> None:
    for text, field in (
        ("kind_env: A", "kind_env"),
        ("mask_env: [A]", "mask_env"),
        ("limits: {total: 5}", "limits.total"),
        ("limits: {max_labels: 5}", "limits.max_labels"),
        ("limits: {window: 60}", "limits.window"),
        ("limits: {adds_per_window_total: 5}", "limits.adds_per_window_total"),
        ("context: {max_chars: 2000}", "context.max_chars"),
        ("context: {deadline: 3}", "context.deadline"),
        ("repos: {}", "repos"),
        ("bogus: 1", "bogus"),
    ):
        put(
            gw,
            "config.yaml",
            "repos:\n  "
            + REPO
            + ":\n    "
            + text.replace(": {", ":\n      ", 1).replace("}", "")
            + "\n"
            if text.startswith(("limits", "context"))
            else f"repos:\n  {REPO}:\n    {text}\n",
        )
        assert bad(gw) == f"repos.{REPO}.{field}", text


def test_override_values_are_range_checked(gw: FakeGateway) -> None:
    for body, field in (
        ("retention: {max_age: 59m}", "retention.max_age"),
        ("retention: {max_age: 366d}", "retention.max_age"),
        ("limits: {max_body_bytes: 255}", "limits.max_body_bytes"),
        ("limits: {max_body_bytes: 8192.0}", "limits.max_body_bytes"),
        ("limits: {live_per_repo: 0}", "limits.live_per_repo"),
        ("limits: {adds_per_window: true}", "limits.adds_per_window"),
        ("context: {author_kinds: [A]}", "context.author_kinds"),
    ):
        put(gw, "config.yaml", f"repos:\n  {REPO}: {{{body}}}\n")
        assert bad(gw) == f"repos.{REPO}.{field}", body


def test_override_cross_key_checks_use_the_effective_config(gw: FakeGateway) -> None:
    put(
        gw,
        "config.yaml",
        f"limits: {{total: 100, live_per_repo: 10}}\nrepos:\n  {REPO}:\n    limits: {{live_per_repo: 101}}\n",  # noqa: E501
    )
    assert bad(gw) == f"repos.{REPO}.limits.live_per_repo"
    put(
        gw,
        "config.yaml",
        f"limits: {{adds_per_window_total: 30}}\nrepos:\n  {REPO}:\n    limits: {{adds_per_window: 31}}\n",  # noqa: E501
    )
    assert bad(gw) == f"repos.{REPO}.limits.adds_per_window"
    put(
        gw,
        "config.yaml",
        f"limits: {{adds_per_window_total: 30}}\nrepos:\n  {REPO}:\n    limits: {{adds_per_window: 30}}\n",  # noqa: E501
    )
    load_config(gw)


def test_repo_ids_must_match_the_grammar_and_are_not_echoed(gw: FakeGateway) -> None:
    for key in (
        "GitHub.com/o/r",
        "github.com/o/*",
        "github.com",
        "github.com/o/..",
        "gh.com/o/r\\n",
        "*",
    ):
        put(gw, "config.yaml", f"repos:\n  '{key}': {{}}\n")
        assert bad(gw) == "repos", key
    put(gw, "config.yaml", "repos:\n  5: {}\n")
    assert bad(gw).endswith("config.yaml")  # a non-string key is refused by the loader
    put(gw, "config.yaml", "repos: [a]\n")
    assert bad(gw) == "repos"
    put(gw, "config.yaml", f"repos:\n  {REPO}:\n")
    assert bad(gw) == f"repos.{REPO}"


def test_repos_merge_across_files(gw: FakeGateway) -> None:
    put(gw, "config.yaml", f"repos:\n  {REPO}:\n    limits: {{max_body_bytes: 300}}\n")
    put(
        gw,
        "config.d/a.yaml",
        f"repos:\n  {REPO}:\n    limits: {{live_per_repo: 9}}\n  github.com/p/q:\n    retention: {{max_age: 1d}}\n",  # noqa: E501
    )
    c = load_config(gw)
    eff = c.for_repo(REPO)
    assert (eff.limits.max_body_bytes, eff.limits.live_per_repo) == (300, 9)
    assert c.for_repo("github.com/p/q").retention.max_age == 86400


def test_defaults_models_are_frozen() -> None:
    c = Config()
    with pytest.raises(Exception):  # noqa: B017, PT011
        c.kind_env = "X"  # type: ignore[misc]


def test_example_config_loads_and_equals_defaults(gw: FakeGateway) -> None:
    example = Path(__file__).parent.parent / "config.example.yaml"
    put(gw, "config.yaml", example.read_bytes())
    assert load_config(gw) == Config()


def test_example_validates_against_the_schema() -> None:
    import json

    import jsonschema
    import yaml

    root = Path(__file__).parent.parent
    schema = json.loads((root / "schemas" / "config.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    doc: Any = yaml.safe_load((root / "config.example.yaml").read_text())
    jsonschema.validate(doc, schema)
