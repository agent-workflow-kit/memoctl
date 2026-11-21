"""The state directory: check it, create it, hold it by descriptor (interface.md, State).

The directory is ``.local/state/memoctl`` under the password-database home
(:func:`memoctl.config.resolve_home`; ``HOME`` and ``XDG_STATE_HOME`` are never read). It is
opened with ``O_DIRECTORY`` and ``O_NOFOLLOW`` and checked on that descriptor (owned by the
user, no group or other permission); everything later goes through the same descriptor.

Missing parents ``.local`` and ``.local/state`` are created at 0700; existing parents are not
checked and a symlinked parent is followed (the spec checks only the directory itself). The
modes are set with ``fchmod`` on the opened descriptor, so the umask cannot weaken or break
them; a directory that exists with the wrong mode is refused, never fixed. A refusal is an
:class:`UnsafeStateError` whose message is fixed text.

todo(#6): the CLI maps :class:`UnsafeStateError` to exit 20 ``unsafe_state`` and an ``OSError``
from a failed creation (read-only parent, no permission on a parent) to its exit.
todo(#9): the store opens ``state.db`` by :attr:`StateDir.db_path` after :meth:`check_db`.
todo(#15): ``log.jsonl`` and ``log.lock`` are opened relative to :attr:`StateDir.handle`.
"""

from __future__ import annotations

import errno
from pathlib import Path
from types import TracebackType

from memoctl.config import ConfigError, resolve_home
from memoctl.gateway import DirHandle, Gateway, PathInfo

__all__ = ["DB_NAME", "STATE_PARTS", "StateDir", "UnsafeStateError", "open_state_dir"]

STATE_PARTS = (".local", "state", "memoctl")
DB_NAME = "state.db"
DIR_MODE = 0o700
FILE_MODE = 0o600
FORBIDDEN_BITS = 0o077
ATTEMPTS = 3  # a rival may create or remove a level between our open and our mkdir

# Errors that on the final component mean "there is something here we may not use"; a
# foreign owner's 0700 directory shows up as EACCES.
_REFUSED = {errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM, getattr(errno, "EMLINK", -1)}


class UnsafeStateError(Exception):
    """The state directory or ``state.db`` is not fit to use (exit 20 ``unsafe_state``).

    ``reason`` is one of ``not_a_directory``, ``owner``, ``mode``, ``parent``, ``race``,
    ``not_a_file``; the message never has a path or a value.
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


def _descend(
    cur: DirHandle, name: str, *, follow: bool, create: bool
) -> tuple[DirHandle | None, bool]:
    """Open ``name`` under ``cur``; with ``create`` make it at 0700 when missing. Returns the
    handle (``None`` only when missing and not creating) and whether this call created it."""
    for _ in range(ATTEMPTS):
        nxt = cur.open_dir(name, follow=follow)
        if nxt is not None:
            return nxt, False
        if not create:
            return None, False
        try:
            cur.mkdir(name, DIR_MODE)
        except FileExistsError:
            continue  # a rival created it (or a dangling symlink sits there): look again
        made = cur.open_dir(name, follow=False)
        if made is None:
            continue
        try:
            made.set_mode(DIR_MODE)
        except BaseException:
            made.close()
            raise
        return made, True
    raise UnsafeStateError("race")


def open_state_dir(gateway: Gateway, *, create: bool) -> StateDir | None:
    """Open and check the state directory; ``None`` when it does not exist (and ``create`` is
    false). With ``create`` the missing levels are made at 0700. Raises
    :class:`memoctl.config.ConfigError` (``home``) and :class:`UnsafeStateError`."""
    home = resolve_home(gateway)
    root = gateway.open_dir(home, follow=True)
    if root is None:
        raise ConfigError("home")
    cur = root
    opened = False
    try:
        for name in STATE_PARTS[:-1]:
            try:
                nxt, _ = _descend(cur, name, follow=True, create=create)
            except NotADirectoryError as exc:
                raise UnsafeStateError("parent") from exc
            if nxt is None:
                return None
            cur.close()
            cur = nxt
        try:
            nxt, _ = _descend(cur, STATE_PARTS[-1], follow=False, create=create)
        except OSError as exc:
            if exc.errno in _REFUSED:
                raise UnsafeStateError("not_a_directory") from exc
            raise
        if nxt is None:
            return None
        cur.close()
        cur = nxt
        _check(cur.stat(), gateway.uid(), "dir", "not_a_directory")
        result = StateDir(gateway, cur, home.joinpath(*STATE_PARTS))
        opened = True
        return result
    finally:
        if not opened:
            cur.close()
