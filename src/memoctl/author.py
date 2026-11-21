"""Author kind and actor resolution (interface.md, Author kind and Global flags).

The kind is a claim, not an identity. It comes from ``--kind``, then from the environment
variable named by config ``kind_env`` (read through the gateway, never ``os.environ``), else
it is ``unknown``. A variable value that fails the kind grammar becomes ``unknown`` without
being echoed, stored, logged or printed: this module never puts a value into a message or a
result that did not already pass its grammar.

TODO(#16): ``add`` calls :func:`resolve_kind` at step 4 and :func:`kind_is_shown` for the
``kind_not_shown`` warning. TODO(#13): the secret check of the kind (exit 20 secret_detected,
details.field kind, from --kind or the variable) runs on the resolved kind in add step 6.
TODO(#6): map GrammarError to the exit 2 usage envelope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from memoctl.config import Config
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError, matches

__all__ = [
    "UNKNOWN_KIND",
    "ActorResult",
    "ActorSource",
    "KindResult",
    "KindSource",
    "kind_is_shown",
    "resolve_actor",
    "resolve_kind",
]

UNKNOWN_KIND = "unknown"
_MAX_KIND_CHARS = 64  # a longer value cannot match; skip the regex for a huge one
_LOGIN_UNSAFE = re.compile(r"[^A-Za-z0-9._@-]", re.ASCII)
_MAX_LOGIN_CHARS = 64


class KindSource(Enum):
    FLAG = "flag"
    ENV = "env"
    DEFAULT = "default"


class ActorSource(Enum):
    FLAG = "flag"
    TTY = "tty"
    DEFAULT = "default"


@dataclass(frozen=True)
class KindResult:
    kind: str
    source: KindSource


@dataclass(frozen=True)
class ActorResult:
    actor: str
    source: ActorSource


def resolve_kind(gateway: Gateway, config: Config, flag: str | None) -> KindResult:
    """The claimed author kind for ``add``.

    ``config`` is the effective config (``kind_env`` is global-only, so it is the same either
    way). A ``flag`` that fails the grammar raises :class:`GrammarError` (field ``kind``) and
    is never rescued by the variable. An unset, empty or malformed variable gives ``unknown``.
    """
    if flag is not None:
        if len(flag) > _MAX_KIND_CHARS or not matches("kind", flag):
            raise GrammarError("kind")
        return KindResult(flag, KindSource.FLAG)
    value = gateway.env(config.kind_env)
    if value is not None and len(value) <= _MAX_KIND_CHARS and matches("kind", value):
        return KindResult(value, KindSource.ENV)
    return KindResult(UNKNOWN_KIND, KindSource.DEFAULT)


def kind_is_shown(kind: str, config: Config) -> bool:
    """True when the effective ``context.author_kinds`` lists ``kind`` (else warn
    ``kind_not_shown``). Pass the repo's effective config (``Config.for_repo``)."""
    return kind in config.context.author_kinds


def resolve_actor(gateway: Gateway, flag: str | None) -> ActorResult:
    """The actor: ``--actor`` (checked), else ``human:<user>`` on a terminal, else
    ``auto:unknown``. The login name is sanitized to the actor grammar."""
    if flag is not None:
        if not matches("actor", flag):
            raise GrammarError("actor")
        return ActorResult(flag, ActorSource.FLAG)
    if not gateway.isatty(0):
        return ActorResult("auto:unknown", ActorSource.DEFAULT)
    try:
        login = gateway.login_name()
    except OSError:
        login = None
    name = _LOGIN_UNSAFE.sub("_", login)[:_MAX_LOGIN_CHARS] if login else ""
    return ActorResult(f"human:{name or 'unknown'}", ActorSource.TTY)
