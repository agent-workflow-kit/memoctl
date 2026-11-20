"""Gateway: the single boundary between memoctl and the outside world.

This is the only module that reads the clock, the password-database home, the terminal, the
standard descriptors, standard input, randomness or files, and the only one that starts a
process (including ``crontab``). An HTTP method is added by the issue that first needs it.
Tests substitute a fake (``tests/fake_gateway.py``) or fake binaries (``tests/fakebin/``);
``tests/test_import_boundary.py`` fails if another module reaches outside directly.

Commands are always run as argv lists, never through a shell, and untrusted text is passed via
stdin or files, never in argv or the environment. Process output is decoded as UTF-8 with
``surrogateescape`` so no byte sequence raises or is lost.

Body sources (``read_file_bounded``, ``read_stdin_bounded``) return raw bytes, never text.

A command reads the clock once with ``now()`` and hands the value down as a parameter.
todo(#16): enforce one clock read per command.

todo(#5): env accessor (kind_env, XDG) - ``os.environ`` is banned outside the gateway.
"""

from __future__ import annotations

import errno
import os
import pwd
import secrets
import select
import shutil
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

ENCODING = "utf-8"
ERRORS = "surrogateescape"
DEFAULT_TERMINAL_WIDTH = 80


class GatewayError(RuntimeError):
    """An outside call failed; the message names what was attempted and how it failed."""


# Why a body source was refused; fixed strings, never the path or the content.
SOURCE_REASONS = ("symlink", "not_regular", "terminal", "unreadable")
READ_CHUNK = 65536


class BodySourceError(GatewayError):
    """A file or standard input cannot be used as a body source. ``reason`` is one of
    ``SOURCE_REASONS``; the message never contains the path or any content."""

    def __init__(self, reason: str) -> None:
        if reason not in SOURCE_REASONS:
            raise ValueError(f"unknown body source reason {reason!r}")
        super().__init__(f"body source refused ({reason})")
        self.reason = reason


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

    def read_file_bounded(self, path: Path, nbytes: int) -> bytes:
        """Read at most ``nbytes`` bytes of the regular file at ``path`` without following a
        symlink in its last component. Raises ``BodySourceError``."""
        ...

    def read_stdin_bounded(self, nbytes: int) -> bytes:
        """Read at most ``nbytes`` bytes from standard input (fewer at end of file). Raises
        ``BodySourceError`` for a terminal, a directory or a descriptor that cannot be read."""
        ...

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


def _read_fd(fd: int, nbytes: int) -> bytes:
    """Read until end of file or ``nbytes`` bytes, whichever is first; never asks for more
    than is still wanted, so a growing file or an endless source cannot exceed the bound."""
    chunks: list[bytes] = []
    remaining = nbytes
    while remaining > 0:
        try:
            chunk = os.read(fd, min(READ_CHUNK, remaining))
        except BlockingIOError:
            # A non-blocking descriptor (the caller set O_NONBLOCK on the open file
            # description) with no data yet: wait until it is readable, then retry.
            # todo(#10): the deadline of the command interrupts this wait.
            select.select([fd], [], [])
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return False


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

    def read_file_bounded(self, path: Path, nbytes: int) -> bytes:
        """Open with ``O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC | O_NOCTTY`` (a FIFO or device opens
        at once instead of blocking), require a regular file on ``fstat`` of the open
        descriptor (so there is no check-then-open race) and read at most ``nbytes``.

        An ``lstat`` runs first so that a socket, FIFO or device is classified without
        opening it (opening some special files has side effects, and a socket cannot be
        opened at all). It is not a security boundary: the ``fstat`` after the open is the
        authority.
        """
        try:
            before = os.lstat(path)
        except (TimeoutError, InterruptedError):
            raise
        except (OSError, ValueError):  # missing, denied, a loop, an embedded NUL
            raise BodySourceError("unreadable") from None
        if stat.S_ISLNK(before.st_mode):
            raise BodySourceError("symlink")
        if not stat.S_ISREG(before.st_mode):
            raise BodySourceError("not_regular")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
        try:
            fd = os.open(path, flags)
        except (TimeoutError, InterruptedError):
            raise
        except OSError as exc:
            # ELOOP (Linux, macOS) or EMLINK (BSD) also come from a loop in a parent
            # component: it is a symlink refusal only when the last component is one.
            if exc.errno in (errno.ELOOP, errno.EMLINK) and _is_symlink(path):
                raise BodySourceError("symlink") from None
            raise BodySourceError("unreadable") from None
        except ValueError:  # an embedded NUL
            raise BodySourceError("unreadable") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BodySourceError("not_regular")
            return _read_fd(fd, nbytes)
        except (TimeoutError, InterruptedError):
            raise
        except OSError:
            raise BodySourceError("unreadable") from None
        finally:
            os.close(fd)

    def read_stdin_bounded(self, nbytes: int) -> bytes:
        """Read descriptor 0 directly (no text layer, no locale). A terminal and a directory
        are refused; a pipe, socket, file or device (``/dev/zero``) is read up to ``nbytes``.
        The read blocks while a pipe stays open and silent: the deadline of the command is
        what ends it.
        todo(#10): the deadline timer that interrupts this read.
        """
        try:
            mode = os.fstat(0).st_mode
            if os.isatty(0):
                raise BodySourceError("terminal")
            if stat.S_ISDIR(mode):
                raise BodySourceError("not_regular")
            return _read_fd(0, nbytes)
        except (TimeoutError, InterruptedError):
            raise  # the deadline (todo(#10)) or a signal handler, not an unreadable source
        except OSError:
            raise BodySourceError("unreadable") from None

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
