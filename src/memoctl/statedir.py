"""The state directory: check it, create it, hold it by descriptor (interface.md, State).

The directory is ``.local/state/memoctl`` under the password-database home
(:func:`memoctl.config.resolve_home`; ``HOME`` and ``XDG_STATE_HOME`` are never read). It is
opened with ``O_DIRECTORY`` and ``O_NOFOLLOW`` and checked on that descriptor (owned by the
user, no group or other permission); everything later goes through the same descriptor.

Missing parents ``.local`` and ``.local/state`` are created at 0700; existing parents are not
checked and a symlinked parent is followed (the spec checks only the directory itself). The
modes are set explicitly after creation (``mkdir`` then ``chmod`` by name, ``fchmod`` on the
opened descriptor), so the umask cannot weaken or break them and is never changed; a directory
that exists with the wrong mode is refused, never fixed. A refusal is an
:class:`UnsafeStateError` whose message is fixed text. Only an existing final component is
translated into a refusal; an error from reaching or creating a level (read-only file system,
no permission on a parent, I/O error) is a :class:`StateAccessError` (the ``OSError`` is its
cause). The CLI maps :class:`UnsafeStateError` to exit 20 ``unsafe_state`` and
:class:`StateAccessError` to exit 12 ``db_unreadable``.
``validate``'s check that the nearest existing ancestor of the state directory is writable and
searchable lives in ``validation.py`` (#32), not here.
The store (``store.py``) opens ``state.db`` by :attr:`StateDir.db_path` after
:meth:`check_db` and :meth:`check_sidecars`. ``log.jsonl`` and ``log.lock`` are opened relative
to :attr:`StateDir.handle` by :mod:`memoctl.logfile`.
"""

from __future__ import annotations

import errno
from pathlib import Path
from types import TracebackType
from typing import Literal, overload

from memoctl.config import ConfigError, resolve_home
from memoctl.gateway import DirHandle, Gateway, PathInfo

__all__ = [
    "DB_NAME",
    "SETTLE_TRIES",
    "STATE_PARTS",
    "StateAccessError",
    "StateDir",
    "UnsafeStateError",
    "open_state_dir",
]

STATE_PARTS = (".local", "state", "memoctl")
DB_NAME = "state.db"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DIR_MODE = 0o700
FILE_MODE = 0o600
OWNER_USE = 0o500  # read and search, which opening a directory needs
FORBIDDEN_BITS = 0o077
ATTEMPTS = 3  # a rival may create or remove a level between our open and our mkdir
# A creator makes a level with ``mkdir`` (the umask applies) and sets 0700 right after, so for a
# moment the level has less than 0700. Another process that sees it waits for the creator,
# for at most SETTLE_TRIES * SETTLE_PAUSE (0.3 s) in all per command (one shared budget); a level
# that stays so is then handled as before.
SETTLE_TRIES = 30
SETTLE_PAUSE = 0.01

# Errors from opening an existing final component that mean "something here is not a directory
# we may use": a symlink (ELOOP, or EMLINK on some systems) or a non-directory (ENOTDIR).
_NOT_A_DIR = {errno.ENOTDIR, errno.ELOOP, getattr(errno, "EMLINK", -1)}
# Permission errors on the final open: the reason is found by looking at the name.
_DENIED = {errno.EACCES, errno.EPERM}


class UnsafeStateError(Exception):
    """The state directory or ``state.db`` is not fit to use (exit 20 ``unsafe_state``).

    ``reason`` is one of ``not_a_directory`` (a symlink or other non-directory as the state
    directory), ``owner``, ``mode``, ``parent`` (a non-directory or symlink where a parent
    should be), ``race``, ``not_a_file`` (``state.db``); the message never has a path or a value.
    """

    code = "unsafe_state"

    def __init__(self, reason: str) -> None:
        super().__init__("unsafe state directory")
        self.reason = reason

    def __reduce__(self) -> tuple[type[UnsafeStateError], tuple[str]]:
        return (UnsafeStateError, (self.reason,))


class StateAccessError(Exception):
    """The state directory or ``state.db`` cannot be reached (read-only home, no permission,
    I/O error): exit 12 ``db_unreadable``. Not an ``unsafe_state`` refusal. The message never
    has an errno or a path."""

    def __init__(self) -> None:
        super().__init__("state cannot be accessed")

    def __reduce__(self) -> tuple[type[StateAccessError], tuple[()]]:
        return (StateAccessError, ())


def _check(info: PathInfo, uid: int, kind: str, not_kind: str) -> None:
    if info.kind != kind:
        raise UnsafeStateError(not_kind)
    if info.uid != uid:
        raise UnsafeStateError("owner")
    if info.mode & FORBIDDEN_BITS:
        raise UnsafeStateError("mode")


class StateDir:
    """The checked state directory, held open. Close it (or use ``with``) when done."""

    def __init__(self, gateway: Gateway, handle: DirHandle, path: Path) -> None:
        self._gateway = gateway
        self.handle = handle
        self.path = path

    @property
    def uid(self) -> int:
        return self._gateway.uid()

    @property
    def db_path(self) -> Path:
        return self.path / DB_NAME

    def check_db(self) -> bool:
        """``False`` when ``state.db`` is missing; ``True`` when it is a regular file owned by
        the user with no group or other permission; :class:`UnsafeStateError` otherwise (a
        symlink included)."""
        try:
            info = self.handle.lstat(DB_NAME)
        except OSError as exc:
            raise StateAccessError from exc
        if info is None:
            return False
        _check(info, self._gateway.uid(), "file", "not_a_file")
        return True

    def check_sidecars(self) -> None:
        """SQLite's ``-wal``, ``-shm`` and ``-journal`` files, when present, must be regular
        files owned by the user with no group or other permission (a symlink or a wrong mode
        there is :class:`UnsafeStateError` like ``state.db``). SQLite opens them by path, so
        this is the same check-then-open boundary as ``state.db``."""
        uid = self._gateway.uid()
        for suffix in SIDECAR_SUFFIXES:
            try:
                info = self.handle.lstat(DB_NAME + suffix)
            except OSError as exc:
                raise StateAccessError from exc
            if info is not None:
                _check(info, uid, "file", "not_a_file")

    def create_db(self) -> bool:
        """Create an empty ``state.db`` at 0600, exclusively, without following a symlink.
        ``False`` when something is there already (another process created it): the caller
        then runs :meth:`check_db` and opens it. Only ``add`` calls this."""
        try:
            return self.handle.create_file(DB_NAME, FILE_MODE)
        except OSError as exc:
            raise StateAccessError from exc

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> StateDir:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _open_final(cur: DirHandle, name: str, uid: int) -> DirHandle | None:
    """Open the final component without following a symlink; a refusal becomes the reason
    that explains it, an unexplained permission error propagates."""
    try:
        return cur.open_dir(name, follow=False)
    except OSError as exc:
        if exc.errno in _NOT_A_DIR:
            raise UnsafeStateError("not_a_directory") from exc
        if exc.errno not in _DENIED:
            raise
        info = cur.lstat(name)
        if info is None:
            raise
        if info.kind != "dir":
            raise UnsafeStateError("not_a_directory") from exc
        if info.uid != uid:
            raise UnsafeStateError("owner") from exc
        if info.mode & FORBIDDEN_BITS or info.mode & OWNER_USE != OWNER_USE:
            raise UnsafeStateError("mode") from exc
        raise


class _Settle:
    """The pauses left for one ``open_state_dir`` call, shared by every level and both waits, so
    the whole command waits at most SETTLE_TRIES * SETTLE_PAUSE."""

    def __init__(self) -> None:
        self.left = SETTLE_TRIES


def _unsettled(kind: str, owner: int, mode: int, uid: int, create: bool) -> bool:
    """A directory of the user, no group or other permission, and below what the command needs:
    for ``create`` less than 0700 (a creator has not yet set its mode, as under a umask such as
    0277 or 0777), for a read without owner read and search (unusable as it is). A level that
    is usable already, or that is not the user's or has other bits, is not waited for."""
    if kind != "dir" or owner != uid or mode & FORBIDDEN_BITS:
        return False
    need = DIR_MODE if create else OWNER_USE
    return mode & need != need


def _wait_for_creator(
    gateway: Gateway, cur: DirHandle, name: str, uid: int, settle: _Settle, *, create: bool
) -> None:
    """Wait while ``name`` looks like a level whose creator has not yet set its mode (see
    :func:`_unsettled`). The wait is bounded by the shared budget; nothing is changed or
    fixed here and nothing is accepted because of it."""
    while settle.left > 0:
        info = cur.lstat(name)
        if info is None or not _unsettled(info.kind, info.uid, info.mode, uid, create):
            return
        settle.left -= 1
        gateway.pause(SETTLE_PAUSE)


def _wait_for_creator_of(gateway: Gateway, handle: DirHandle, uid: int, settle: _Settle) -> None:
    """:func:`_wait_for_creator` for a level already open, on its descriptor, for ``create``
    only (a 0500 level opens, but nothing can be made in it until its creator has set 0700)."""
    while settle.left > 0:
        info = handle.stat()
        if not _unsettled(info.kind, info.uid, info.mode, uid, True):
            return
        settle.left -= 1
        gateway.pause(SETTLE_PAUSE)


def _descend(
    gateway: Gateway,
    cur: DirHandle,
    name: str,
    *,
    final: bool,
    create: bool,
    uid: int,
    settle: _Settle,
) -> DirHandle | None:
    """Open ``name`` under ``cur`` (a parent follows a symlink, the final component does not);
    with ``create`` make it at 0700 when missing. ``None`` only when it is missing and not
    creating. Errors from making it propagate unchanged."""
    for _ in range(ATTEMPTS):
        _wait_for_creator(gateway, cur, name, uid, settle, create=create)
        try:
            nxt = _open_final(cur, name, uid) if final else cur.open_dir(name, follow=True)
        except (PermissionError, UnsafeStateError):
            # The creator may have made the level just after the wait above looked: wait for it
            # again and try once more; a level that is really unusable fails the same way.
            _wait_for_creator(gateway, cur, name, uid, settle, create=create)
            nxt = _open_final(cur, name, uid) if final else cur.open_dir(name, follow=True)
        if nxt is not None:
            if create:
                _wait_for_creator_of(gateway, nxt, uid, settle)
            return nxt
        if not create:
            return None
        try:
            cur.make_dir(name, DIR_MODE)
        except FileExistsError:
            # A rival created it, or a dangling symlink sits there (a symlink never goes away
            # by itself, so it is not a race).
            info = cur.lstat(name)
            if info is not None and info.kind == "symlink":
                raise UnsafeStateError("not_a_directory" if final else "parent") from None
            continue
        made = _open_final(cur, name, uid) if final else cur.open_dir(name, follow=False)
        if made is None:
            continue
        try:
            made.set_mode(DIR_MODE)
        except BaseException:
            made.close()
            raise
        return made
    raise UnsafeStateError("race")


@overload
def open_state_dir(gateway: Gateway, *, create: Literal[True]) -> StateDir: ...
@overload
def open_state_dir(gateway: Gateway, *, create: bool) -> StateDir | None: ...
def open_state_dir(gateway: Gateway, *, create: bool) -> StateDir | None:
    """Open and check the state directory; ``None`` when it does not exist (and ``create`` is
    false). With ``create`` the missing levels are made at 0700. Raises
    :class:`memoctl.config.ConfigError` (``home``), :class:`UnsafeStateError` and
    :class:`StateAccessError` (an ``OSError`` from reaching or making it)."""
    try:
        return _open_state_dir(gateway, create=create)
    except OSError as exc:
        raise StateAccessError from exc


def _open_state_dir(gateway: Gateway, *, create: bool) -> StateDir | None:
    home = resolve_home(gateway)
    root = gateway.open_dir(home, follow=True)
    if root is None:
        raise ConfigError("home")
    cur = root
    uid = gateway.uid()
    settle = _Settle()
    opened = False
    try:
        for name in STATE_PARTS[:-1]:
            try:
                nxt = _descend(
                    gateway, cur, name, final=False, create=create, uid=uid, settle=settle
                )
            except NotADirectoryError as exc:
                raise UnsafeStateError("parent") from exc
            if nxt is None:
                return None
            cur.close()
            cur = nxt
        nxt = _descend(
            gateway, cur, STATE_PARTS[-1], final=True, create=create, uid=uid, settle=settle
        )
        if nxt is None:
            return None
        cur.close()
        cur = nxt
        _check(cur.stat(), uid, "dir", "not_a_directory")
        result = StateDir(gateway, cur, home.joinpath(*STATE_PARTS))
        opened = True
        return result
    finally:
        if not opened:
            cur.close()
