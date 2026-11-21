"""Gateway: the single boundary between memoctl and the outside world.

This is the only module that reads the clock, the password-database home, the terminal, the
standard descriptors, standard input, randomness or files, and the only one that starts a
process (including ``crontab``). An HTTP method is added by the issue that first needs it.
Tests substitute a fake (``tests/fake_gateway.py``) or fake binaries (``tests/fakebin/``);
``tests/test_import_boundary.py`` fails if another module reaches outside directly.

Commands are always run as argv lists, never through a shell, and untrusted text is passed via
stdin or files, never in argv or the environment. Process output is decoded as UTF-8 with
``surrogateescape`` so no byte sequence raises or is lost.

A command reads the clock once with ``now()`` and hands the value down as a parameter.
todo(#16): enforce one clock read per command.

``env`` is the only way to read an environment variable (``os.environ`` is banned elsewhere).
The config loader never calls it: HOME and XDG_* are not read (interface.md, Config).
"""

from __future__ import annotations

import errno
import os
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

ENCODING = "utf-8"
ERRORS = "surrogateescape"
DEFAULT_TERMINAL_WIDTH = 80


class GatewayError(RuntimeError):
    """An outside call failed; the message names what was attempted and how it failed."""


@dataclass(frozen=True)
class PathInfo:
    """What the config checks need to know about a path.

    ``kind`` is ``file`` (regular), ``dir``, ``symlink`` or ``other`` (FIFO, device, socket).
    ``mode`` holds the permission bits only.
    """

    kind: str
    uid: int
    mode: int


@dataclass(frozen=True)
class FileRead:
    """The result of ``open_file``: ``info`` describes the opened descriptor; ``data`` is the
    first ``limit + 1`` bytes for a regular file and empty for every other kind."""

    info: PathInfo
    data: bytes


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _info(st: os.stat_result) -> PathInfo:
    return PathInfo(_kind(st.st_mode), st.st_uid, stat.S_IMODE(st.st_mode))


def _component(name: str) -> str:
    """``name`` when it is one plain path component; ``ValueError`` otherwise, so a handle
    can never be steered out of its directory."""
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"not a single path component: {name!r}")
    return name


class DirHandle(Protocol):
    """An open directory descriptor: every name is relative to it and one component long.

    The callers check the directory once, through :meth:`stat` (``fstat`` on the descriptor),
    and everything after that goes through the same descriptor, so a swapped path cannot
    redirect it. ``close`` is idempotent; any other call on a closed handle raises ``OSError``.
    """

    def stat(self) -> PathInfo:
        """Kind, owner and mode of the directory itself (``fstat``)."""
        ...

    def open_dir(self, name: str, *, follow: bool) -> DirHandle | None:
        """Open the subdirectory ``name`` (a final symlink is followed only when ``follow``).
        ``None`` when it is missing. A symlink (not following), a non-directory and a refusal
        raise ``OSError``."""
        ...

    def make_dir(self, name: str, mode: int) -> None:
        """Create ``name`` (``mkdirat``; never follows a symlink). ``FileExistsError`` when
        anything, even a dangling symlink, is there. The directory gets exactly
        ``mode``: the umask is cleared for the call, so the mode is exact."""
        ...

    def set_mode(self, mode: int) -> None:
        """Set the directory's permission bits exactly (``fchmod``; the umask does not apply)."""
        ...

    def lstat(self, name: str) -> PathInfo | None:
        """Kind, owner and mode of ``name`` without following a symlink; ``None`` when missing."""
        ...

    def create_file(self, name: str, mode: int) -> bool:
        """Create the regular file ``name`` empty, exclusively and without following a symlink,
        with exactly ``mode`` (the umask does not apply). ``False`` when anything is there."""
        ...

    def close(self) -> None: ...


_UMASK_LOCK = threading.Lock()


class _RealDirHandle:
    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def _live(self) -> int:
        if self._fd is None:
            raise OSError(errno.EBADF, "directory handle is closed")
        return self._fd

    def stat(self) -> PathInfo:
        return _info(os.fstat(self._live()))

    def open_dir(self, name: str, *, follow: bool) -> DirHandle | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if not follow:
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(_component(name), flags, dir_fd=self._live())
        except FileNotFoundError:
            return None
        return _RealDirHandle(fd)

    def make_dir(self, name: str, mode: int) -> None:
        # The umask is cleared for this one call so the directory is created with exactly
        # ``mode``: a hostile umask (say 0o277) would otherwise leave a directory its owner
        # cannot open. The umask is process-wide; the lock only orders our own callers, and
        # every other file this tool creates sets its mode explicitly.
        with _UMASK_LOCK:
            old = os.umask(0)
            try:
                os.mkdir(_component(name), mode, dir_fd=self._live())
            finally:
                os.umask(old)

    def set_mode(self, mode: int) -> None:
        os.fchmod(self._live(), mode)

    def lstat(self, name: str) -> PathInfo | None:
        try:
            return _info(os.stat(_component(name), dir_fd=self._live(), follow_symlinks=False))
        except FileNotFoundError:
            return None

    def create_file(self, name: str, mode: int) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(_component(name), flags, mode, dir_fd=self._live())
        except FileExistsError:
            return False
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        return True

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


class Gateway(Protocol):
    """Everything memoctl needs from outside the process."""

    def now(self) -> datetime:
        """The current time, timezone-aware (UTC)."""
        ...

    def home(self) -> Path:
        """The user's home from the password database (never the ``HOME`` variable)."""
        ...

    def login_name(self) -> str | None:
        """The login name from the password database, or ``None`` when the lookup fails."""
        ...

    def uid(self) -> int:
        """The real user id of the process."""
        ...

    def env(self, name: str) -> str | None:
        """The value of environment variable ``name``, or ``None`` when it is unset."""
        ...

    def stat_path(self, path: Path, *, follow: bool) -> PathInfo | None:
        """Kind, owner and mode of ``path`` (``lstat`` unless ``follow``); ``None`` when it or
        a parent is missing. Other errors raise ``OSError``."""
        ...

    def list_dir(self, path: Path) -> list[str]:
        """The entry names of a directory, unordered. Raises ``OSError`` as the filesystem does."""
        ...

    def open_file(self, path: Path, limit: int) -> FileRead | None:
        """Open ``path`` without following a symlink and without blocking, check the opened
        descriptor and read at most ``limit + 1`` bytes of a regular file. ``None`` when the
        file is missing; a symlink gives kind ``symlink`` (nothing read). Other errors raise
        ``OSError``."""
        ...

    def open_dir(self, path: Path, *, follow: bool) -> DirHandle | None:
        """Open the directory ``path`` (``O_DIRECTORY``; a final symlink is followed only when
        ``follow``; symlinks in parent components are always followed). ``None`` when it or a
        parent is missing. A final symlink (not following), a non-directory (also as a parent)
        and a refusal raise ``OSError``."""
        ...

    def stdin_read(self) -> str: ...
    def write_out(self, text: str) -> None: ...
    def write_err(self, text: str) -> None: ...
    def isatty(self, fd: int) -> bool: ...
    def terminal_width(self) -> int: ...

    def random_hex(self, nbytes: int) -> str:
        """``nbytes`` random bytes as ``2 * nbytes`` hex characters."""
        ...

    def read_bytes(self, path: Path) -> bytes: ...
    def write_bytes(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        """Replace ``path`` atomically with ``data`` at exactly ``mode`` (default 0600): a
        temporary file beside it is written, synced and renamed over it, so a reader sees the
        old or the new bytes, never a mix, and a symlink at ``path`` is replaced, not written
        through. The parent is not created (``FileNotFoundError``)."""
        ...

    def run(
        self, argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...

    def crontab_read(self) -> bytes | None:
        """The current crontab, or ``None`` when the user has none."""
        ...

    def crontab_write(self, data: bytes) -> None:
        """Install ``data`` (exact bytes) as the crontab through ``crontab -``."""
        ...


def _to_bytes(text: str | bytes | None) -> bytes | None:
    if isinstance(text, str):
        return text.encode(ENCODING, ERRORS)
    return text


def _exec(
    argv: list[str],
    stdin: bytes | None,
    timeout: float | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv`` without a shell; any start failure or timeout becomes a ``GatewayError``."""
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GatewayError(f"{argv!r} timed out after {timeout}s") from exc
    except (OSError, ValueError) as exc:
        raise GatewayError(f"{argv!r} could not run: {exc}") from exc


def _run(
    argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell; output is decoded as UTF-8 with ``surrogateescape``."""
    raw = _exec(argv, _to_bytes(stdin), timeout)
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode(ENCODING, ERRORS),
        raw.stderr.decode(ENCODING, ERRORS),
    )


def _write_fd(fd: int, text: str) -> None:
    """Write ``text`` to a standard descriptor as UTF-8 with errors replaced, never through the
    locale. A failure to write (closed descriptor, EPIPE) is ignored, as the interface says."""
    view = memoryview(text.encode(ENCODING, "replace"))
    try:
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        pass


class RealGateway:
    """The production gateway."""

    def __init__(self, *, crontab_bin: str = "crontab", crontab_timeout: float = 30) -> None:
        self._crontab_bin = crontab_bin
        self._crontab_timeout = crontab_timeout

    def now(self) -> datetime:
        return datetime.fromtimestamp(time.time(), UTC)

    def home(self) -> Path:
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError as exc:
            raise GatewayError("the user is not in the password database") from exc

    def login_name(self) -> str | None:
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return None

    def uid(self) -> int:
        return os.getuid()

    def env(self, name: str) -> str | None:
        return os.environ.get(name)

    def stat_path(self, path: Path, *, follow: bool) -> PathInfo | None:
        try:
            st = Path(path).stat() if follow else Path(path).lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        return _info(st)

    def list_dir(self, path: Path) -> list[str]:
        return os.listdir(path)

    def open_file(self, path: Path, limit: int) -> FileRead | None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
                return FileRead(PathInfo("symlink", -1, 0), b"")
            raise
        try:
            info = _info(os.fstat(fd))
            chunks: list[bytes] = []
            remaining = limit + 1
            while info.kind == "file" and remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return FileRead(info, b"".join(chunks))
        finally:
            os.close(fd)

    def open_dir(self, path: Path, *, follow: bool) -> DirHandle | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if not follow:
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        return _RealDirHandle(fd)

    def stdin_read(self) -> str:
        return sys.stdin.read()

    def write_out(self, text: str) -> None:
        _write_fd(1, text)

    def write_err(self, text: str) -> None:
        _write_fd(2, text)

    def isatty(self, fd: int) -> bool:
        try:
            return os.isatty(fd)
        except OSError:
            return False

    def terminal_width(self) -> int:
        return shutil.get_terminal_size(fallback=(DEFAULT_TERMINAL_WIDTH, 24)).columns

    def random_hex(self, nbytes: int) -> str:
        if nbytes < 1:
            raise ValueError("nbytes must be positive")
        return secrets.token_hex(nbytes)

    def read_bytes(self, path: Path) -> bytes:
        """Read the whole file. Raises ``OSError`` as the filesystem does."""
        return Path(path).read_bytes()

    def write_bytes(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        if not isinstance(data, bytes):
            raise TypeError(f"write_bytes needs bytes, got {type(data).__name__}")
        target = Path(path)
        parent = os.fspath(target.parent)
        temp = os.path.join(parent, f".{target.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp, flags, mode)
        try:
            try:
                os.fchmod(fd, mode)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, target)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def run(
        self, argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _run(argv, stdin=stdin, timeout=timeout)

    def crontab_read(self) -> bytes | None:
        result = self._crontab(["-l"], None)
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and result.stderr.startswith(b"no crontab for"):
            return None
        raise self._failure("-l", result)

    def crontab_write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError(f"crontab_write needs bytes, got {type(data).__name__}")
        result = self._crontab(["-"], data)
        if result.returncode != 0:
            raise self._failure("-", result)

    def _crontab(self, args: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        # An explicit copy: the message language is fixed (the "no crontab for" check) and
        # CRONTAB_NOHEADER (which changes what ``-l`` prints) never reaches the child.
        env = {k: v for k, v in os.environ.items() if k != "CRONTAB_NOHEADER"}
        env["LC_ALL"] = "C"
        return _exec([self._crontab_bin, *args], stdin, self._crontab_timeout, env)

    def _failure(self, arg: str, result: subprocess.CompletedProcess[bytes]) -> GatewayError:
        detail = result.stderr.decode(ENCODING, ERRORS).strip()
        return GatewayError(f"crontab {arg} failed (exit {result.returncode}): {detail}")
