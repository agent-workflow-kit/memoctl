"""Load and validate the config (interface.md, Config).

Layers, lowest first: defaults, ``config.yaml``, ``config.d/*.yaml`` in ``sorted()`` order, then
a repo's override (:meth:`Config.for_repo`). There is no environment layer and no flag layer here
(flags are applied by the commands): this module never reads ``HOME``, ``XDG_*`` or any other
variable. The home is the password-database home from the gateway.

Every failure is a :class:`ConfigError` (code ``config_invalid``) whose ``field`` is a dotted
key, ``home`` or a file path, and whose message is fixed text: no value from a file is ever
placed in an error. Files are read only through the gateway.

todo(#6): the CLI maps :class:`ConfigError` to the exit 2 ``config_invalid`` envelope
(``details.field`` is :attr:`ConfigError.field`). todo(#8): the state directory uses
:func:`resolve_home` too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from memoctl.gateway import FileRead, Gateway, GatewayError
from memoctl.grammar import GrammarError, matches, parse_duration

__all__ = [
    "CONFIG_DIR",
    "Config",
    "ConfigError",
    "Context",
    "Limits",
    "Retention",
    "load_config",
    "resolve_home",
]

CONFIG_DIR = Path(".config") / "memoctl"
MAX_FILE_BYTES = 65536
MAX_DROP_INS = 256  # beyond the interface: bounds the work a hostile directory can cause
MAX_DEPTH = 8  # the config nests four levels (repos.<id>.limits.<key>); more is refused
MAX_FIELD_CHARS = 4096
DROP_IN_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\.yaml", re.ASCII)
# A key name is echoed in an error only when it is plainly a name; anything else is reported
# at the parent so that an attacker-chosen key never reaches a message.
SAFE_KEY = re.compile(r"[A-Za-z0-9_.:/-]{1,255}", re.ASCII)


class ConfigError(Exception):
    """Invalid configuration (exit 2 ``config_invalid``). ``field`` is a dotted key, ``home`` or
    a file path (at most 4096 characters). The message is fixed and never has a value."""

    code = "config_invalid"

    def __init__(self, field: str) -> None:
        cleaned = "".join(c if c.isprintable() else "?" for c in field)[:MAX_FIELD_CHARS]
        super().__init__("invalid configuration")
        self.field = cleaned

    def __reduce__(self) -> tuple[type[ConfigError], tuple[str]]:
        return (ConfigError, (self.field,))


# ---------------------------------------------------------------------------------------------
# Strict YAML
# ---------------------------------------------------------------------------------------------

_BOOL = re.compile(r"(?:true|false)\Z")
_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)\Z")
_FLOAT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)\.[0-9]+\Z")
_NULL = re.compile(r"(?:null|~|)\Z")


class _StrictLoader(yaml.SafeLoader):
    """``safe_load`` with duplicate keys, anchors, aliases, merge keys and unusual scalars refused.

    Scalars: ``true`` and ``false`` are booleans, ``0`` or a decimal without leading zeros
    (with an optional fraction) is a number, ``null``, ``~`` and the empty scalar are null, and
    everything else (``no``, ``off``, ``010``, ``1e3``, ``.nan``, dates) stays a string.
    Aliases are refused outright, so a small file cannot expand into a huge structure.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError("anchor or alias")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.YAMLError("not a mapping")
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise yaml.YAMLError("non-scalar key")
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str) or key in seen or key == "<<":
                raise yaml.YAMLError("duplicate or non-string key")
            seen.add(key)
        return {
            self.construct_object(k, deep=True): self.construct_object(v, deep=deep)
            for k, v in node.value
        }


def _scalar(pattern: re.Pattern[str], convert: Any) -> Any:
    def construct(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        text = loader.construct_scalar(node)  # type: ignore[arg-type]
        if not isinstance(text, str) or pattern.fullmatch(text) is None:
            raise yaml.YAMLError("bad scalar")
        return convert(text)

    return construct


def _construct_str(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node))  # type: ignore[arg-type]


def _construct_seq(loader: yaml.SafeLoader, node: yaml.Node) -> list[Any]:
    if not isinstance(node, yaml.SequenceNode):
        raise yaml.YAMLError("not a sequence")
    return [loader.construct_object(child, deep=True) for child in node.value]


def _construct_map(loader: _StrictLoader, node: yaml.Node) -> dict[Any, Any]:
    return loader.construct_mapping(node, deep=True)


def _construct_unknown(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    raise yaml.YAMLError("unsupported tag")


def _install_scalars() -> None:
    cls = _StrictLoader
    cls.yaml_multi_constructors = {}
    cls.yaml_implicit_resolvers = {}
    for tag, pattern, first in (
        ("tag:yaml.org,2002:bool", _BOOL, list("tf")),
        ("tag:yaml.org,2002:int", _INT, list("-+0123456789")),
        ("tag:yaml.org,2002:float", _FLOAT, list("-+0123456789")),
        ("tag:yaml.org,2002:null", _NULL, ["~", "n", ""]),
    ):
        cls.add_implicit_resolver(tag, pattern, first)
    cls.yaml_constructors = {
        "tag:yaml.org,2002:bool": _scalar(_BOOL, lambda t: t == "true"),
        "tag:yaml.org,2002:int": _scalar(_INT, int),
        "tag:yaml.org,2002:float": _scalar(_FLOAT, float),
        "tag:yaml.org,2002:null": _scalar(_NULL, lambda t: None),
        "tag:yaml.org,2002:str": _construct_str,
        "tag:yaml.org,2002:seq": _construct_seq,
        "tag:yaml.org,2002:map": _construct_map,
        None: _construct_unknown,
    }


_install_scalars()


def _depth_ok(value: Any) -> bool:
    """Iterative nesting check (a recursive walk of a hostile file could itself overflow)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def _parse_yaml(data: bytes, where: str) -> dict[str, Any]:
    """Parse one file's bytes into a mapping; any problem raises ``ConfigError(where)``."""
    if b"\x00" in data:
        raise ConfigError(where)
    try:
        text = data.decode("utf-8")
        loaded = yaml.load(text, Loader=_StrictLoader)  # noqa: S506  (a restricted SafeLoader)
    except (yaml.YAMLError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ConfigError(where) from exc
    if loaded is None:
        return {}  # an empty file (or only comments) is an empty mapping
    if not isinstance(loaded, dict) or not _depth_ok(loaded):
        raise ConfigError(where)
    return loaded


def _merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Maps merge recursively; a list or scalar in ``top`` replaces the value in ``base``."""
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------------

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


def _check_duration(value: Any, field: str, low: int, high: int) -> int:
    """An integer (seconds, not a bool) or a duration string; the range applies to the result.

    ``field`` is the leaf key; the error's location (the full dotted key) is added by the model.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PydanticCustomError("duration", "bad duration")
    if isinstance(value, str):
        try:
            value = parse_duration(value, field=field)
        except GrammarError as exc:
            raise PydanticCustomError("duration", "bad duration") from exc
    if not low <= value <= high:
        raise PydanticCustomError("range", "out of range")
    return value


def _kind_names(values: list[str]) -> list[str]:
    if len(set(values)) != len(values) or not all(matches("kind", v) for v in values):
        raise PydanticCustomError("kinds", "bad kind list")
    return values


class Retention(BaseModel):
    model_config = _STRICT
    max_age: Any = 30 * 86400  # seconds, 1 hour to 365 days

    @field_validator("max_age", mode="before")
    @classmethod
    def _max_age(cls, value: Any) -> int:
        return _check_duration(value, "retention.max_age", 3600, 365 * 86400)


class Limits(BaseModel):
    model_config = _STRICT
    max_body_bytes: Annotated[int, Field(ge=256, le=65536)] = 8192
    max_labels: Annotated[int, Field(ge=0, le=8)] = 8
    live_per_repo: Annotated[int, Field(ge=1, le=100000)] = 200
    total: Annotated[int, Field(ge=1, le=1000000)] = 5000
    adds_per_window: Annotated[int, Field(ge=1, le=10000)] = 20
    adds_per_window_total: Annotated[int, Field(ge=1, le=100000)] = 60
    window: Any = 600  # seconds, 1 minute to 1 day

    @field_validator("window", mode="before")
    @classmethod
    def _window(cls, value: Any) -> int:
        return _check_duration(value, "limits.window", 60, 86400)


class Context(BaseModel):
    model_config = _STRICT
    author_kinds: Annotated[list[str], Field(max_length=64)] = [  # noqa: RUF012
        "implement",
        "draft-ticket",
        "audit",
        "improve",
    ]
    max_chars: Annotated[int, Field(ge=1000, le=9000)] = 8000
    max_bytes: Annotated[int, Field(ge=1000, le=65536)] = 15000
    max_notes: Annotated[int, Field(ge=1, le=100)] = 20
    max_per_topic: Annotated[int, Field(ge=1, le=20)] = 2
    max_kind_share: Any = 0.5  # an integer or a decimal, 0.1 to 1
    note_max_chars: Annotated[int, Field(ge=100, le=8192)] = 2000
    deadline: Any = 2  # seconds, 1 to 10

    _kinds = field_validator("author_kinds")(_kind_names)

    @field_validator("max_kind_share", mode="before")
    @classmethod
    def _share(cls, value: Any) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PydanticCustomError("type", "bad number")
        if not (0.1 <= value <= 1):  # false for NaN
            raise PydanticCustomError("range", "out of range")
        return value

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, value: Any) -> int:
        return _check_duration(value, "context.deadline", 1, 10)


class OverrideRetention(BaseModel):
    model_config = _STRICT
    max_age: Any = None

    @field_validator("max_age", mode="before")
    @classmethod
    def _max_age(cls, value: Any) -> int:
        return _check_duration(value, "retention.max_age", 3600, 365 * 86400)


class OverrideLimits(BaseModel):
    model_config = _STRICT
    max_body_bytes: Annotated[int, Field(ge=256, le=65536)] | None = None
    live_per_repo: Annotated[int, Field(ge=1, le=100000)] | None = None
    adds_per_window: Annotated[int, Field(ge=1, le=10000)] | None = None


class OverrideContext(BaseModel):
    model_config = _STRICT
    author_kinds: Annotated[list[str], Field(max_length=64)] | None = None

    _kinds = field_validator("author_kinds")(lambda v: v if v is None else _kind_names(v))


class RepoOverride(BaseModel):
    """The only keys a repo may override (interface.md, Config)."""

    model_config = _STRICT
    retention: OverrideRetention = OverrideRetention()
    limits: OverrideLimits = OverrideLimits()
    context: OverrideContext = OverrideContext()


def _mask_names(values: list[str]) -> list[str]:
    if not all(matches("kind_env", v) for v in values):
        raise PydanticCustomError("names", "bad name list")
    return values


class Config(BaseModel):
    """The merged config. Durations are whole seconds. ``repos`` holds the raw overrides."""

    model_config = _STRICT
    kind_env: str = "CSESSION_KIND"
    mask_env: Annotated[list[str], Field(max_length=32)] = []  # noqa: RUF012
    retention: Retention = Retention()
    limits: Limits = Limits()
    context: Context = Context()
    repos: dict[str, RepoOverride] = {}  # noqa: RUF012

    _masks = field_validator("mask_env")(_mask_names)

    @field_validator("kind_env")
    @classmethod
    def _kind_env(cls, value: str) -> str:
        if not matches("kind_env", value):
            raise PydanticCustomError("grammar", "bad name")
        return value

    @field_validator("repos", mode="before")
    @classmethod
    def _repo_ids(cls, value: Any) -> Any:
        if isinstance(value, dict) and not all(
            isinstance(k, str) and matches("repo", k) for k in value
        ):
            raise PydanticCustomError("repo", "bad repo id")  # loc is ``repos``, never the key
        return value

    def for_repo(self, repo: str) -> Config:
        """The effective config of ``repo``: the override merged over the global values.

        A repo with no override gets the global config. The result has no ``repos``.
        """
        override = self.repos.get(repo)
        if override is None:
            return self.model_copy(update={"repos": {}})
        return self.model_copy(
            update={
                "repos": {},
                "retention": _apply(self.retention, override.retention),
                "limits": _apply(self.limits, override.limits),
                "context": _apply(self.context, override.context),
            }
        )


def _apply(base: Any, override: BaseModel) -> Any:
    """``base`` with every value the override set (a field it did not set stays as it was)."""
    changes = override.model_dump(exclude_unset=True)
    return base.model_copy(update=changes)


def _cross_checks(config: Config, prefix: str) -> None:
    """``live_per_repo`` at most ``total``; ``adds_per_window`` at most the store-wide figure."""
    limits = config.limits
    pairs = (
        ("live_per_repo", limits.live_per_repo, limits.total),
        ("adds_per_window", limits.adds_per_window, limits.adds_per_window_total),
    )
    for key, low, high in pairs:
        if low > high:
            raise ConfigError(f"{prefix}limits.{key}")


def _field_of(exc: ValidationError, fallback: str) -> str:
    """The dotted key of the first problem, built only from key names that look like names."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    parts: list[str] = []
    for item in errors[0]["loc"] if errors else ():
        if not isinstance(item, str) or SAFE_KEY.fullmatch(item) is None:
            break
        parts.append(item)
    return ".".join(parts) or fallback


def _validate(raw: dict[str, Any], where: str) -> Config:
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_field_of(exc, where)) from None
    _cross_checks(config, "")
    for repo in config.repos:
        _cross_checks(config.for_repo(repo), f"repos.{repo}.")
    return config


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def resolve_home(gateway: Gateway) -> Path:
    """The password-database home, which must be absolute and an existing directory.

    Raises ``ConfigError("home")`` otherwise (lookup failure, empty, relative, missing, a file).
    """
    try:
        home = gateway.home()
    except GatewayError as exc:
        raise ConfigError("home") from exc
    if not home.is_absolute():
        raise ConfigError("home")
    try:
        info = gateway.stat_path(home, follow=True)
    except OSError as exc:
        raise ConfigError("home") from exc
    if info is None or info.kind != "dir":
        raise ConfigError("home")
    return home


def _check_owner_and_mode(gateway: Gateway, uid: int, info: Any, where: str) -> None:
    if info.uid != uid or info.mode & 0o022:
        raise ConfigError(where)


def _read_file(gateway: Gateway, uid: int, path: Path) -> FileRead | None:
    where = str(path)
    try:
        got = gateway.open_file(path, MAX_FILE_BYTES)
    except (OSError, GatewayError) as exc:
        raise ConfigError(where) from exc
    if got is None:
        return None
    if got.info.kind != "file" or len(got.data) > MAX_FILE_BYTES:
        raise ConfigError(where)
    _check_owner_and_mode(gateway, uid, got.info, where)
    return got


def _check_dir(gateway: Gateway, uid: int, path: Path) -> bool:
    """True when ``path`` is a directory fit to read; False when it is missing."""
    try:
        info = gateway.stat_path(path, follow=False)
    except OSError as exc:
        raise ConfigError(str(path)) from exc
    if info is None:
        return False
    if info.kind != "dir":  # a symlink is refused too
        raise ConfigError(str(path))
    _check_owner_and_mode(gateway, uid, info, str(path))
    return True


def load_config(gateway: Gateway) -> Config:
    """Read ``config.yaml`` and ``config.d/*.yaml`` from the home and validate the result.

    A missing config file means defaults. Raises :class:`ConfigError` for anything wrong.
    """
    home = resolve_home(gateway)
    uid = gateway.uid()
    directory = home / CONFIG_DIR
    merged: dict[str, Any] = {}
    if not _check_dir(gateway, uid, directory):
        return _validate(merged, str(directory))
    main = directory / "config.yaml"
    got = _read_file(gateway, uid, main)
    if got is not None:
        merged = _parse_yaml(got.data, str(main))
    drop_dir = directory / "config.d"
    if _check_dir(gateway, uid, drop_dir):
        try:
            names = gateway.list_dir(drop_dir)
        except OSError as exc:
            raise ConfigError(str(drop_dir)) from exc
        wanted = sorted(n for n in names if DROP_IN_NAME.fullmatch(n))
        if len(wanted) > MAX_DROP_INS:
            raise ConfigError(str(drop_dir))
        for name in wanted:
            path = drop_dir / name
            got = _read_file(gateway, uid, path)
            if got is None:  # removed since the listing
                raise ConfigError(str(path))
            merged = _merge(merged, _parse_yaml(got.data, str(path)))
    return _validate(merged, str(directory))
