"""``memoctl validate``: the four checks as a pure, read-only function (interface.md, validate).

:func:`validate_store` runs ``config``, ``state_dir``, ``database`` and ``schema`` in that
order and returns a :class:`ValidateResult` whose :meth:`~ValidateResult.document` is the
``--json`` document (``schemas/cli/validate.json``). A failed check ends the run: later checks
are omitted (the spec's rule), so the checks are independent in that none reads another's
result, and a hostile file yields a reported failure, never a crash.

Strictly read-only: nothing is created, migrated or repaired. The state directory is opened
without ``create``, the database in ``Mode.READ``. Opening a WAL database read-only may create
``-shm`` and an empty ``-wal`` beside it, with the database's mode (SQLite, interface.md
Database); that is the only trace and the database file itself is never written.

Messages are fixed strings: no path, row, value or sqlite text. The rows are not counted
(the spec gives a fixed message); a failure names the reason only.

``log.jsonl`` is not read: the spec's checks do not include it, so blank and partial lines in
it cannot affect validate.

A deadline (``StoreError("timeout")``, raised when the progress handler installed through
``configure`` fires) is not a check failure: it propagates so the CLI prints the error
envelope (exit 12 ``timeout``).

todo(#6): register ``validate`` (no ``--dry-run``) with one line in ``COMMANDS`` in
``cli_commands.py`` (PR #56): a ``Command`` whose ``run(args, gateway)`` calls
:func:`validate_store`, prints ``json.dumps(result.document())`` with ``--json`` and returns
:attr:`ValidateResult.exit_code`; a ``StoreError`` with reason ``timeout`` goes to the
envelope path. First branch that merges #56 and #60 adds it.
todo(#21): human output through ``format_validate`` (``render.py``, PR #60) and ``emit``.
todo(#10): pass the deadline's ``configure`` hook (PR #59) as ``configure=``.
todo(#35): the live WSL/network-mount check of ``unsafe_filesystem`` is a human step.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memoctl.config import ConfigError, load_config, resolve_home
from memoctl.gateway import Gateway, GatewayError
from memoctl.statedir import STATE_PARTS, StateAccessError, UnsafeStateError, open_state_dir
from memoctl.store import (
    DB_CORRUPT,
    DB_LOCKED,
    DB_UNREADABLE,
    TIMEOUT,
    Mode,
    SchemaNewerError,
    Store,
    StoreError,
    open_store,
)

__all__ = [
    "CHECK_NAMES",
    "UNSAFE_FS_TYPES",
    "CheckResult",
    "ValidateResult",
    "filesystem_warning",
    "validate_store",
]

CHECK_NAMES = ("config", "state_dir", "database", "schema")
UNSAFE_FS_TYPES = frozenset(
    {"drvfs", "9p", "v9fs", "nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs"}
)
MOUNTINFO_LIMIT = 4 * 1024 * 1024
UNSAFE_FILESYSTEM = "unsafe_filesystem"

_EXIT = {"config_invalid": 2, "unsafe_state": 20}
_FAILED_EXIT = 12  # every other reason

_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    reason: str | None
    message: str

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "reason": self.reason, "message": self.message}


@dataclass(frozen=True)
class ValidateResult:
    checks: tuple[CheckResult, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def exit_code(self) -> int:
        for check in self.checks:
            if not check.ok:
                return _EXIT.get(check.reason or "", _FAILED_EXIT)
        return 0

    def document(self) -> dict[str, Any]:
        """The ``--json`` document: ``{ok, checks, warnings}``."""
        return {
            "ok": self.ok,
            "checks": [c.document() for c in self.checks],
            "warnings": list(self.warnings),
        }


def _pass(name: str, message: str) -> CheckResult:
    return CheckResult(name, True, None, message)


def _fail(name: str, reason: str, message: str) -> CheckResult:
    return CheckResult(name, False, reason, message)


# --- unsafe_filesystem ----------------------------------------------------------------------


def _unescape(field: str) -> str:
    """mountinfo writes space, tab, newline and backslash as three-digit octal escapes."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def _contains(mount: str, path: str) -> bool:
    return mount == "/" or path == mount or path.startswith(mount + "/")


def filesystem_type(mountinfo: bytes, path: Path) -> str | None:
    """The file system type of the longest mount point in ``mountinfo`` containing ``path``
    (the last such entry when a mount is covered by another at the same point). Lines that do
    not parse are skipped. ``None`` when no line matches."""
    text = mountinfo.decode("utf-8", errors="surrogateescape")
    target = str(path)
    best_len = -1
    best: str | None = None
    for line in text.split("\n"):
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        head = left.split(" ")
        tail = right.split(" ")
        if len(head) < 5 or not tail[0]:
            continue
        mount = _unescape(head[4])
        if _contains(mount, target) and len(mount) >= best_len:
            best_len = len(mount)
            best = tail[0]
    return best


def filesystem_warning(gateway: Gateway, path: Path) -> bool:
    """True when ``path`` (an existing directory, symlinks resolved) is on a file system type
    where WAL is unreliable. No readable ``/proc/self/mountinfo``: no warning."""
    try:
        data = gateway.read_mountinfo(MOUNTINFO_LIMIT)
        if data is None:
            return False
        if len(data) >= MOUNTINFO_LIMIT:
            # the read stopped at the cap: the last line may be cut mid-field (``nfsd`` read
            # as ``nfs``), so only whole lines are used
            data = data[: data.rfind(b"\n") + 1]
        fs = filesystem_type(data, gateway.resolve_path(path))
    except (OSError, GatewayError):
        return False
    return fs in UNSAFE_FS_TYPES


# --- the checks -----------------------------------------------------------------------------


def _check_config(gateway: Gateway) -> CheckResult:
    try:
        load_config(gateway)
    except ConfigError:
        return _fail("config", "config_invalid", "the configuration is invalid")
    return _pass("config", "configuration is valid")


def _nearest_existing(gateway: Gateway, path: Path) -> Path | None:
    """The nearest ancestor of ``path`` that exists (symlinks followed); ``None`` when the
    walk reaches the root without finding one."""
    current = path
    while True:
        parent = current.parent
        if parent == current:
            return None
        current = parent
        info = gateway.stat_path(current, follow=True)
        if info is not None:
            return current


def _warning_base(gateway: Gateway, home: Path) -> Path | None:
    """The directory the file system warning is judged on when ``state_dir`` fails: the state
    directory when it exists, else its nearest existing ancestor; ``None`` when that cannot be
    told."""
    expected = home.joinpath(*STATE_PARTS)
    try:
        if gateway.stat_path(expected, follow=True) is not None:
            return expected
        return _nearest_existing(gateway, expected)
    except (OSError, GatewayError):
        return None


def _unfollowable_parent(gateway: Gateway, home: Path) -> bool:
    """True when ``.local`` or ``.local/state`` is a symlink that cannot be followed (dangling
    or looping): ``add`` refuses it (``UnsafeStateError("parent")``), so it is not "missing"."""
    level = home
    for name in STATE_PARTS[:-1]:
        level = level / name
        info = gateway.stat_path(level, follow=False)
        if info is None:
            return False
        if info.kind == "symlink" and gateway.stat_path(level, follow=True) is None:
            return True
    return False


def _check_state_dir(gateway: Gateway) -> tuple[CheckResult, Path | None]:
    """The result and the existing directory the file system warning is judged on (also when
    the check fails)."""
    try:
        home = resolve_home(gateway)
    except ConfigError:
        return _fail("state_dir", "config_invalid", "the home directory is not usable"), None
    try:
        state = open_state_dir(gateway, create=False)
    except ConfigError:
        return _fail("state_dir", "config_invalid", "the home directory is not usable"), None
    except UnsafeStateError:
        return (
            _fail("state_dir", "unsafe_state", "the state directory is not safe to use"),
            _warning_base(gateway, home),
        )
    except (OSError, StateAccessError):
        # an existing level that cannot be opened (no search permission, I/O error)
        return (
            _fail("state_dir", "unsafe_state", "the state directory cannot be opened"),
            _warning_base(gateway, home),
        )
    if state is not None:
        with state:
            return _pass("state_dir", "state directory is private"), state.path
    expected = home.joinpath(*STATE_PARTS)
    ancestor: Path | None = None
    try:
        ancestor = _nearest_existing(gateway, expected)
        info = None if ancestor is None else gateway.stat_path(ancestor, follow=True)
        usable = (
            ancestor is not None
            and info is not None
            and info.kind == "dir"
            and gateway.access(ancestor, write=True, search=True)
            and not _unfollowable_parent(gateway, home)
        )
    except OSError:
        return (
            _fail("state_dir", "unsafe_state", "the state directory cannot be created"),
            _warning_base(gateway, home),
        )
    if not usable:
        return (
            _fail("state_dir", "unsafe_state", "the state directory cannot be created"),
            ancestor,
        )
    return _pass("state_dir", "not created yet"), ancestor


def _check_store(gateway: Gateway, configure: Callable[[Store], None] | None) -> list[CheckResult]:
    """``database`` and ``schema``, from one read-only open. Only ``database`` when it fails."""
    try:
        state = open_state_dir(gateway, create=False)
    except (ConfigError, UnsafeStateError, StateAccessError, OSError):
        # the state_dir check passed a moment ago; whatever changed since is not safe either
        return [_fail("database", "unsafe_state", "the state directory is not safe to use")]
    if state is None:
        return [_pass("database", "no database yet"), _pass("schema", "no database yet")]
    with state:
        return _check_open(state, configure)


def _check_open(state: Any, configure: Callable[[Store], None] | None) -> list[CheckResult]:
    try:
        store = open_store(state, Mode.READ, configure=configure)
    except (UnsafeStateError, StateAccessError, OSError):
        # a refused file, or one whose lstat failed (I/O error): not safe to use either
        return [_fail("database", "unsafe_state", "the database file is not safe to use")]
    except SchemaNewerError:
        # application_id and identity were accepted before the version was refused; the rows
        # of a newer layout cannot be read, so they are not checked
        return [
            _pass("database", "identity ok; newer schema, rows not checked"),
            _fail("schema", "schema_newer", "the schema is newer than this version supports"),
        ]
    except StoreError as exc:
        return [_database_failure(exc)]
    if store is None:
        return [_pass("database", "no database yet"), _pass("schema", "no database yet")]
    try:
        with store:
            with store.read():
                store.check_rows()
                if not store.quick_check():
                    raise StoreError(DB_CORRUPT)
    except StoreError as exc:
        return [_database_failure(exc)]
    return [
        _pass("database", "database is consistent"),
        _pass("schema", "schema version is supported"),
    ]


_DB_MESSAGES = {
    DB_UNREADABLE: "the database cannot be read",
    DB_LOCKED: "the database is locked",
    DB_CORRUPT: "the database is damaged or not memoctl's",
}


def _database_failure(exc: StoreError) -> CheckResult:
    if exc.reason == TIMEOUT:
        raise exc  # the deadline: the caller prints the error envelope
    reason = exc.reason if exc.reason in _DB_MESSAGES else DB_CORRUPT
    return _fail("database", reason, _DB_MESSAGES[reason])


def validate_store(
    gateway: Gateway, *, configure: Callable[[Store], None] | None = None
) -> ValidateResult:
    """Run the four checks, read-only. ``configure`` is handed to ``open_store`` (the
    deadline's progress handler). Raises ``StoreError("timeout")`` when it fires."""
    checks: list[CheckResult] = []
    warnings: list[str] = []

    config = _check_config(gateway)
    checks.append(config)
    if not config.ok:
        return ValidateResult(tuple(checks), ())

    state_check, existing = _check_state_dir(gateway)
    checks.append(state_check)
    if existing is not None and filesystem_warning(gateway, existing):
        warnings.append(UNSAFE_FILESYSTEM)
    if not state_check.ok:
        return ValidateResult(tuple(checks), tuple(warnings))

    for check in _check_store(gateway, configure):
        checks.append(check)
        if not check.ok:
            break
    return ValidateResult(tuple(checks), tuple(warnings))
