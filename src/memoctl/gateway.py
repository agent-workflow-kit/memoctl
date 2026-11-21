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

    def argv(self) -> list[str]:
        """The command-line arguments after the program name, undecoded bytes kept as
        ``surrogateescape`` characters."""
        ...

    def fd_is_open(self, fd: int) -> bool:
        """``fstat`` on ``fd``: false only when the descriptor is closed (``EBADF``)."""
        ...

    def open_devnull(self, *, write: bool) -> int:
        """Open ``/dev/null`` (write-only when ``write``, else read-only); the lowest free
        descriptor is returned, inheritable. Raises ``OSError``."""
        ...

    def dup_onto(self, src: int, dst: int) -> None:
        """``dup2(src, dst)``; ``dst`` is inheritable. Raises ``OSError``."""
        ...

    def release_fd(self, fd: int) -> None:
        """Close ``fd``; an ``OSError`` is ignored."""
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
    def write_bytes(self, path: Path, data: bytes) -> None: ...

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

    def argv(self) -> list[str]:
        return list(sys.argv[1:])

    def fd_is_open(self, fd: int) -> bool:
        try:
            os.fstat(fd)
        except OSError as exc:
            return exc.errno != errno.EBADF
        return True

    def open_devnull(self, *, write: bool) -> int:
        fd = os.open(os.devnull, (os.O_WRONLY if write else os.O_RDONLY) | os.O_CLOEXEC)
        os.set_inheritable(fd, True)
        return fd

    def dup_onto(self, src: int, dst: int) -> None:
        os.dup2(src, dst, inheritable=True)

    def release_fd(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

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

    def write_bytes(self, path: Path, data: bytes) -> None:
        """Write ``data`` to ``path``.

        Not atomic (a crash can leave a truncated file), does not set a file mode, does not
        create parent directories and does not fsync.
        todo(#8): file modes and atomic replace.
        """
        Path(path).write_bytes(data)

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
