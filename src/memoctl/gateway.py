"""Gateway: the single boundary between memoctl and the outside world.

This is the only module that reads the clock, the password-database home, the terminal, the
standard descriptors, standard input, randomness or files, and the only one that starts a
process (including ``crontab``). An HTTP method is added by the issue that first needs it.
Tests substitute a fake (``tests/fake_gateway.py``) or fake binaries (``tests/fakebin/``);
``tests/test_import_boundary.py`` fails if another module reaches outside directly.

Commands are always run as argv lists, never through a shell, and untrusted text is passed via
stdin or files, never in argv or the environment. Process output is decoded as UTF-8 with
``surrogateescape`` so no byte sequence raises or is lost.

Signals are process-wide host state, so the wall-clock alarm (``alarm_arm``) and the immediate
exit (``exit_now``) live here too; ``memoctl.deadline`` builds the command deadline on them.

Body sources (``read_file_bounded``, ``read_stdin_bounded``) return raw bytes, never text.

A command reads the clock once with ``now()`` and hands the value down as a parameter.
todo(#16): enforce one clock read per command.

``env`` is the only way to read an environment variable (``os.environ`` is banned elsewhere).
The config loader never calls it: HOME and XDG_* are not read (interface.md, Config).
"""

from __future__ import annotations

import errno
import fcntl
import math
import os
import pwd
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, Protocol

ENCODING = "utf-8"
ERRORS = "surrogateescape"
DEFAULT_TERMINAL_WIDTH = 80
TEMP_PREFIX = ".tmp."


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
        anything, even a dangling symlink, is there. The directory gets exactly ``mode``
        whatever the umask (``mkdirat``, then ``fchmodat`` by name; the process umask is never
        touched)."""
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

    def open_file_at(self, name: str, mode: int, *, create: bool) -> FileHandle | None:
        """Open the file ``name`` for reading and appending: ``O_RDWR | O_APPEND | O_NOFOLLOW |
        O_NONBLOCK | O_CLOEXEC`` (a final symlink is refused, a FIFO does not block). With
        ``create`` a missing file is made with ``mode`` (the umask is not compensated: callers
        call :meth:`FileHandle.set_mode`); without it ``None`` is returned for a missing file.
        Any other failure raises ``OSError``. The kind and owner are the caller's to check
        through :meth:`FileHandle.stat`."""
        ...

    def repair_file_mode(self, name: str, uid: int, mode: int) -> bool:
        """Set ``name`` to exactly ``mode`` when it is a regular file owned by ``uid``, for a
        file whose current mode denies the owner access (so :meth:`open_file_at` got ``EACCES``).
        No symlink is followed and a FIFO is never opened. ``False`` when the file is missing or
        is not an own regular file (nothing is changed); ``OSError`` as the filesystem reports."""
        ...

    def size_of(self, name: str) -> int | None:
        """Size in bytes of the regular file ``name`` (no symlink followed); ``None`` when it is
        missing, ``-1`` when it is not a regular file."""
        ...

    def rename(self, old: str, new: str) -> None:
        """``renameat``: atomically move ``old`` onto ``new`` (replacing a file there) inside this
        directory. Never follows a symlink; ``OSError`` as the filesystem reports."""
        ...

    def close(self) -> None: ...


class FileHandle(Protocol):
    """An open regular-file descriptor opened by :meth:`DirHandle.open_file_at`. ``close`` is
    idempotent; any other call on a closed handle raises ``OSError``."""

    def stat(self) -> PathInfo:
        """Kind, owner and mode of the opened file (``fstat``)."""
        ...

    def size(self) -> int:
        """Size in bytes (``fstat``)."""
        ...

    def set_mode(self, mode: int) -> None:
        """Set the permission bits exactly (``fchmod``; the umask does not apply)."""
        ...

    def read_at(self, offset: int, count: int) -> bytes:
        """``pread``: up to ``count`` bytes at ``offset`` (empty past the end)."""
        ...

    def append(self, data: bytes) -> int:
        """Exactly one ``write`` call; returns the number of bytes it wrote, which may be fewer
        than ``len(data)`` (the caller decides). ``OSError`` as the filesystem reports."""
        ...

    def lock_exclusive(self, timeout: float) -> bool:
        """Take an exclusive ``flock``, polling a non-blocking attempt for at most ``timeout``
        seconds; ``False`` when it was not got in time. The lock is released by ``close``."""
        ...

    def close(self) -> None: ...


LOCK_POLL_SECONDS = 0.01


class _RealFileHandle:
    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def _live(self) -> int:
        if self._fd is None:
            raise OSError(errno.EBADF, "file handle is closed")
        return self._fd

    def stat(self) -> PathInfo:
        return _info(os.fstat(self._live()))

    def size(self) -> int:
        return os.fstat(self._live()).st_size

    def set_mode(self, mode: int) -> None:
        os.fchmod(self._live(), mode)

    def read_at(self, offset: int, count: int) -> bytes:
        return os.pread(self._live(), count, offset)

    def append(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError(f"append needs bytes, got {type(data).__name__}")
        return os.write(self._live(), data)

    def lock_exclusive(self, timeout: float) -> bool:
        fd = self._live()
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SECONDS)

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


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
        """Make ``name`` with exactly ``mode`` (the umask does not apply, and is never touched).

        mkdir applies the umask, so the mode is set again afterwards, by name and without
        following a symlink: ``chmod(..., dir_fd=, follow_symlinks=False)``. A hostile umask such
        as 0o777 leaves a directory its owner cannot open yet, so no descriptor is opened for
        this. Where that call is unavailable (``NotImplementedError``, ``ValueError`` or
        ENOTSUP/ENOSYS, e.g. no /proc for the glibc emulation) the name is opened with
        O_NOFOLLOW and ``fchmod`` is used instead, which needs the owner read bit. If the mode
        cannot be set, the just-made directory is removed again (best effort, so a mode-0
        leftover never blocks later runs) and the original error propagates."""
        fd = self._live()
        os.mkdir(_component(name), mode, dir_fd=fd)
        try:
            try:
                os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)
            except (NotImplementedError, ValueError):
                self._fchmod_nofollow(fd, name, mode)
            except OSError as exc:
                if exc.errno not in (errno.ENOTSUP, errno.ENOSYS):
                    raise
                self._fchmod_nofollow(fd, name, mode)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _fchmod_nofollow(fd: int, name: str, mode: int) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        sub = os.open(name, flags, dir_fd=fd)
        try:
            os.fchmod(sub, mode)
        finally:
            os.close(sub)

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

    def open_file_at(self, name: str, mode: int, *, create: bool) -> FileHandle | None:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        try:
            fd = os.open(_component(name), flags, mode, dir_fd=self._live())
        except FileNotFoundError:
            if create:
                raise  # the directory itself is gone
            return None
        return _RealFileHandle(fd)

    def repair_file_mode(self, name: str, uid: int, mode: int) -> bool:
        fd = self._live()
        component = _component(name)
        info = self.lstat(name)
        if info is None or info.kind != "file" or info.uid != uid:
            return False
        # Open it read-only, then write-only, to fchmod the verified inode. A mode that denies
        # both (0000, 0100) leaves chmod by name, which never follows a symlink.
        for access in (os.O_RDONLY, os.O_WRONLY):
            flags = access | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                ro = os.open(component, flags, dir_fd=fd)
            except PermissionError:
                continue
            try:
                st = os.fstat(ro)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                    return False
                os.fchmod(ro, mode)
                return True
            finally:
                os.close(ro)
        try:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            os.chmod(component, mode, dir_fd=fd, follow_symlinks=False)
            after = os.stat(component, dir_fd=fd, follow_symlinks=False)
        except (ValueError, NotImplementedError):
            # A symlink swapped in after the lstat makes chmod refuse the dir_fd and
            # follow_symlinks combination (ValueError / NotImplementedError).
            return False
        # Post-condition: the name still is the same regular file of ours. The lstat -> chmod
        # step is not atomic: a same-uid process that swaps the name in that window can still
        # get one chmod applied to an inode it chose before this check notices (residual,
        # accepted: it already runs as the same user).
        return (
            stat.S_ISREG(before.st_mode)
            and stat.S_ISREG(after.st_mode)
            and after.st_uid == uid
            and (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
        )

    def size_of(self, name: str) -> int | None:
        try:
            st = os.stat(_component(name), dir_fd=self._live(), follow_symlinks=False)
        except FileNotFoundError:
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else -1

    def rename(self, old: str, new: str) -> None:
        fd = self._live()
        os.rename(_component(old), _component(new), src_dir_fd=fd, dst_dir_fd=fd)

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


MAX_ALARM_SECONDS = 3600.0
_MIN_RESTORED_TIMER = 1e-6  # a host timer that ran out while ours was armed fires at once


class AlarmHandle(Protocol):
    """One armed alarm. ``on_alarm`` runs in the main thread between two bytecodes (never while
    SQLite or another C call works) and only while the handle is active."""

    def reset(self, seconds: float) -> None:
        """Restart the one-shot timer with ``seconds`` (same limits as ``alarm_arm``)."""
        ...

    def release(self) -> bool:
        """Stop the timer, discard a signal that is already pending, restore the previous
        handler, the host's timer (its remaining time less what passed) and the signal mask.
        True when the timer had already run out (the alarm fired or is pending). Idempotent:
        later calls do nothing and return False."""
        ...


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

    def access(self, path: Path, *, write: bool, search: bool) -> bool:
        """``os.access`` for the real user id (``W_OK`` when ``write``, ``X_OK`` when
        ``search``; both must hold). ``False`` when the path is missing or on any error."""
        ...

    def resolve_path(self, path: Path) -> Path:
        """``path`` made absolute with every symlink resolved (a missing tail is kept as is)."""
        ...

    def read_mountinfo(self, limit: int) -> bytes | None:
        """The first ``limit`` bytes of ``/proc/self/mountinfo``; ``None`` when it cannot be
        read (not Linux, not mounted)."""
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

    def open_dir(self, path: Path, *, follow: bool) -> DirHandle | None:
        """Open the directory ``path`` (``O_DIRECTORY``; a final symlink is followed only when
        ``follow``; symlinks in parent components are always followed). ``None`` when it or a
        parent is missing. A final symlink (not following), a non-directory (also as a parent)
        and a refusal raise ``OSError``."""
        ...

    def read_file_bounded(self, path: Path, nbytes: int) -> bytes:
        """Read at most ``nbytes`` bytes of the regular file at ``path`` without following a
        symlink in its last component. Raises ``BodySourceError``."""
        ...

    def read_stdin_bounded(self, nbytes: int) -> bytes:
        """Read at most ``nbytes`` bytes from standard input (fewer at end of file). Raises
        ``BodySourceError`` for a terminal, a directory or a descriptor that cannot be read."""
        ...

    def monotonic(self) -> float:
        """A monotonic clock in seconds, for measuring the deadline (never for a timestamp)."""
        ...

    def alarm_arm(self, seconds: float, on_alarm: Callable[[], None]) -> AlarmHandle:
        """Install a ``SIGALRM`` handler that calls ``on_alarm`` and start a one-shot
        ``ITIMER_REAL`` timer of ``seconds``. The previous handler, the signal mask and any timer
        the host had set are kept for :meth:`AlarmHandle.release`. Main thread only
        (``GatewayError`` elsewhere); ``ValueError`` for a ``seconds`` that is not a finite
        number in ``(0, MAX_ALARM_SECONDS]``."""
        ...

    def exit_now(self, code: int) -> NoReturn:
        """Leave the process at once (``os._exit``: no cleanup, no ``finally``, no flush), after
        killing the direct child processes this gateway started and has not yet reaped (not their
        own children)."""
        ...

    def write_out(self, text: str, *, bound: float | None = None) -> None:
        """Write ``text`` to file descriptor 1. With ``bound`` (seconds) the write gives up once
        the descriptor has not become writable for that long (a full pipe, a stopped terminal),
        so the caller never waits on a reader that is not reading."""
        ...

    def write_err(self, text: str, *, bound: float | None = None) -> None:
        """As :meth:`write_out`, for file descriptor 2."""
        ...

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
        through. The parent is not created (``FileNotFoundError``). A hard exit
        (``os._exit``, SIGKILL) between creating the temporary file and the rename can leave
        an orphan ``.tmp.<hex>`` file behind; nothing cleans it up."""
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
    """Run ``argv`` without a shell; any start failure or timeout becomes a ``GatewayError``.

    SIGALRM is blocked from before the child starts until it is in ``_CHILDREN``, so a deadline
    that runs out in that window is delivered right after the registration and ``exit_now``
    kills the child (otherwise it would be orphaned). The child itself starts with the caller's
    mask restored, so it does not inherit a blocked SIGALRM."""
    proc: subprocess.Popen[bytes] | None = None
    caller_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        with subprocess.Popen(
            argv,
            stdin=None if stdin is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask),
        ) as proc:
            _CHILDREN.add(proc)  # so exit_now can kill it when the deadline ends the process
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask)  # a pending alarm runs now
                out, err = proc.communicate(stdin, timeout=timeout)
            except BaseException:  # the same cleanup as subprocess.run
                proc.kill()
                raise
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    except subprocess.TimeoutExpired as exc:
        raise GatewayError(f"{argv!r} timed out after {timeout}s") from exc
    except (OSError, ValueError) as exc:
        raise GatewayError(f"{argv!r} could not run: {exc}") from exc
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask)  # also when Popen failed
        if proc is not None:
            _CHILDREN.discard(proc)  # after the with block reaped it (exit_now never misses one)


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


_CHILDREN: set[subprocess.Popen[bytes]] = set()  # started by _exec, not yet reaped


def _check_alarm_seconds(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("alarm seconds must be a number")
    # The range test alone also rejects nan and inf, and compares a huge int without float().
    if not 0 < seconds <= MAX_ALARM_SECONDS:
        raise ValueError("alarm seconds out of range")
    return float(seconds)


class _RealAlarm:
    """``SIGALRM`` plus ``ITIMER_REAL``, set up and torn down with the signal blocked so a
    handler never runs half-installed or half-restored."""

    def __init__(self, seconds: float, on_alarm: Callable[[], None]) -> None:
        seconds = _check_alarm_seconds(seconds)
        if threading.current_thread() is not threading.main_thread():
            raise GatewayError("the alarm can only be armed from the main thread")
        self._on_alarm = on_alarm
        self._active = False
        self._released = False
        self._previous: signal.Handlers | int | Callable[..., object] | None = signal.SIG_DFL
        self._host_left, self._host_interval, self._host_at = 0.0, 0.0, 0.0
        self._mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            handler = signal.getsignal(signal.SIGALRM)
            # None: installed outside Python and not restorable; the default is the best left.
            self._previous = signal.SIG_DFL if handler is None else handler
            # A SIGALRM left pending while blocked (inherited across exec, or sent early)
            # would hit our handler the moment it is unblocked: ignoring it discards it.
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
            self._host_left, self._host_interval = signal.setitimer(signal.ITIMER_REAL, 0)
            self._host_at = time.monotonic()
            signal.signal(signal.SIGALRM, self._handle)
            self._active = True
            signal.setitimer(signal.ITIMER_REAL, seconds)
        except BaseException:
            self._teardown()
            raise
        # A parent that blocked SIGALRM would silence the deadline: unblock it while armed.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})

    def _handle(self, signum: int, frame: object) -> None:
        if self._active:
            self._on_alarm()

    def reset(self, seconds: float) -> None:
        seconds = _check_alarm_seconds(seconds)
        if self._released:
            raise GatewayError("the alarm was released")
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def release(self) -> bool:
        if self._released:
            return False
        return self._teardown()

    def _teardown(self) -> bool:
        self._released = True
        self._active = False
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            left, _interval = signal.setitimer(signal.ITIMER_REAL, 0)
            ran_out = left == 0.0
            # Ignoring a signal discards one that is pending, so it cannot reach the restored
            # handler (the default action would end the process).
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
            signal.signal(signal.SIGALRM, self._previous)
            if self._host_left > 0:
                left_now = self._host_left - (time.monotonic() - self._host_at)
                signal.setitimer(
                    signal.ITIMER_REAL, max(left_now, _MIN_RESTORED_TIMER), self._host_interval
                )
            return ran_out
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, self._mask)


_BOUNDED_CHUNK = 512  # below PIPE_BUF: a writable pipe takes it whole, without blocking


def _writable_before(fd: int, end: float) -> bool:
    """Wait until ``fd`` can take a write or ``end`` (a ``time.monotonic`` value) passes. A
    closed or broken descriptor counts as ready: the write then fails and is ignored."""
    left_ms = math.ceil((end - time.monotonic()) * 1000)
    if left_ms <= 0:
        return False
    poller = select.poll()
    poller.register(fd, select.POLLOUT)
    return bool(poller.poll(left_ms))


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


# Bound on the wait for a blocked output pipe, per unbounded ``_write_fd`` call (the deadline
# restarts on each call); ``emit`` makes at most three (stdout, stderr, warnings), so the worst
# case is ~9 s. A call with ``bound`` (the signal handler) uses its own, shorter bound.
WRITE_WAIT_SECONDS = 3.0


def _write_fd(fd: int, text: str, bound: float | None = None) -> None:
    """Write ``text`` to a standard descriptor as UTF-8 with errors replaced, never through the
    locale. On a non-blocking descriptor that is full (EAGAIN) it waits for writability, at most
    ``WRITE_WAIT_SECONDS`` per call (the bound restarts on each call), and retries; partial
    writes continue. Any other failure (closed descriptor, EPIPE) and the bound running out are
    ignored, as the interface says ("a failure to write after the result was fixed is ignored").

    With ``bound`` the write is for a signal handler that must not hang: it waits for the
    descriptor to be writable for at most ``bound`` seconds in all (``poll``, EINTR retried
    with the time left) and writes in small chunks, then gives up silently. The descriptor is
    left blocking (its open file description may be shared with other processes)."""
    view = memoryview(text.encode(ENCODING, "replace"))
    end = None if bound is None else time.monotonic() + bound
    wait_end = time.monotonic() + WRITE_WAIT_SECONDS
    try:
        while view:
            if end is None:
                chunk = view
            elif _writable_before(fd, end):
                chunk = view[:_BOUNDED_CHUNK]
            else:
                return
            try:
                view = view[os.write(fd, chunk) :]
            except BlockingIOError:
                if end is not None:
                    continue  # the loop polls again, up to ``end``
                remaining = wait_end - time.monotonic()
                if remaining <= 0:
                    return
                select.select([], [fd], [], remaining)
    except (OSError, ValueError):
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

    def access(self, path: Path, *, write: bool, search: bool) -> bool:
        mode = (os.W_OK if write else 0) | (os.X_OK if search else 0)
        try:
            return os.access(path, mode or os.F_OK)
        except (OSError, ValueError):
            return False

    def resolve_path(self, path: Path) -> Path:
        return Path(os.path.realpath(path))

    def read_mountinfo(self, limit: int) -> bytes | None:
        try:
            fd = os.open("/proc/self/mountinfo", os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            chunks: list[bytes] = []
            remaining = limit
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

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

    def open_dir(self, path: Path, *, follow: bool) -> DirHandle | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if not follow:
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        return _RealDirHandle(fd)

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

    def monotonic(self) -> float:
        return time.monotonic()

    def alarm_arm(self, seconds: float, on_alarm: Callable[[], None]) -> AlarmHandle:
        return _RealAlarm(seconds, on_alarm)

    def exit_now(self, code: int) -> NoReturn:
        for child in list(_CHILDREN):
            try:
                child.kill()  # only a process this gateway started; Popen skips a reaped one
            except Exception:  # noqa: BLE001 - leaving matters more than a failed kill
                pass
        os._exit(code)

    def write_out(self, text: str, *, bound: float | None = None) -> None:
        _write_fd(1, text, bound)

    def write_err(self, text: str, *, bound: float | None = None) -> None:
        _write_fd(2, text, bound)

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
        # A fixed-length name: one derived from the target would exceed NAME_MAX for a long one.
        temp = os.path.join(parent, f"{TEMP_PREFIX}{secrets.token_hex(16)}")
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
