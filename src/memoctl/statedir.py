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
translated into a refusal; an error from creating a level (read-only file system, no
permission on a parent, I/O error) propagates as the ``OSError`` it is.

todo(#6): the CLI maps :class:`UnsafeStateError` to exit 20 ``unsafe_state`` and an ``OSError``
from a failed creation (read-only parent, no permission on a parent, I/O error) to its exit.
todo(#32): ``validate``'s check that the nearest existing ancestor of the state directory is
writable and searchable is owned by #32, not done here.
The store (``store.py``) opens ``state.db`` by :attr:`StateDir.db_path` after
:meth:`check_db` and :meth:`check_sidecars`.
todo(#15): ``log.jsonl`` and ``log.lock`` are opened relative to :attr:`StateDir.handle`.
"""

from __future__ import annotations

import errno
from pathlib import Path
from types import TracebackType
from typing import Literal, overload

from memoctl.config import ConfigError, resolve_home
from memoctl.gateway import DirHandle, Gateway, PathInfo

__all__ = ["DB_NAME", "STATE_PARTS", "StateDir", "UnsafeStateError", "open_state_dir"]

STATE_PARTS = (".local", "state", "memoctl")
DB_NAME = "state.db"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DIR_MODE = 0o700
FILE_MODE = 0o600
OWNER_USE = 0o500  # read and search, which opening a directory needs
FORBIDDEN_BITS = 0o077
ATTEMPTS = 3  # a rival may create or remove a level between our open and our mkdir

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
    def db_path(self) -> Path:
        return self.path / DB_NAME

    def check_db(self) -> bool:
        """``False`` when ``state.db`` is missing; ``True`` when it is a regular file owned by
        the user with no group or other permission; :class:`UnsafeStateError` otherwise (a
        symlink included)."""
        info = self.handle.lstat(DB_NAME)
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
            info = self.handle.lstat(DB_NAME + suffix)
            if info is not None:
                _check(info, uid, "file", "not_a_file")

    def create_db(self) -> bool:
        """Create an empty ``state.db`` at 0600, exclusively, without following a symlink.
        ``False`` when something is there already (another process created it): the caller
        then runs :meth:`check_db` and opens it. Only ``add`` calls this."""
        return self.handle.create_file(DB_NAME, FILE_MODE)

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


def _descend(cur: DirHandle, name: str, *, final: bool, create: bool, uid: int) -> DirHandle | None:
    """Open ``name`` under ``cur`` (a parent follows a symlink, the final component does not);
    with ``create`` make it at 0700 when missing. ``None`` only when it is missing and not
    creating. Errors from making it propagate unchanged."""
    for _ in range(ATTEMPTS):
        nxt = _open_final(cur, name, uid) if final else cur.open_dir(name, follow=True)
        if nxt is not None:
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
    :class:`memoctl.config.ConfigError` (``home``) and :class:`UnsafeStateError`."""
    home = resolve_home(gateway)
    root = gateway.open_dir(home, follow=True)
    if root is None:
        raise ConfigError("home")
    cur = root
    uid = gateway.uid()
    opened = False
    try:
        for name in STATE_PARTS[:-1]:
            try:
                nxt = _descend(cur, name, final=False, create=create, uid=uid)
            except NotADirectoryError as exc:
                raise UnsafeStateError("parent") from exc
            if nxt is None:
                return None
            cur.close()
            cur = nxt
        nxt = _descend(cur, STATE_PARTS[-1], final=True, create=create, uid=uid)
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
