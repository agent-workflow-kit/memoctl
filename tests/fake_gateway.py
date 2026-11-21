"""Test fakes for ``memoctl.gateway``; nothing under ``src/`` imports this module."""

from __future__ import annotations

import errno
import os
import random
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from memoctl.gateway import (
    TEMP_PREFIX,
    DirHandle,
    FileRead,
    GatewayError,
    PathInfo,
    _info,
    _RealDirHandle,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """A clock that moves only when the test moves it, forward or backward."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self._now = self._checked(start)

    @staticmethod
    def _checked(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("FakeClock needs timezone-aware times")
        return value.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = self._checked(value)

    def advance(self, delta: timedelta) -> None:
        """Move by ``delta``; a negative ``delta`` moves backward."""
        self._now += delta


class FakeExit(BaseException):
    """``exit_now`` on the fake: a ``BaseException`` so no ``except Exception`` swallows it."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class FakeAlarm:
    """A one-shot alarm on the fake's monotonic clock; it fires when the test advances time."""

    def __init__(self, gw: FakeGateway, seconds: float, on_alarm: Callable[[], None]) -> None:
        self._gw = gw
        self.on_alarm = on_alarm
        self.at = gw.mono + seconds
        self.released = False
        self.fired = False

    def reset(self, seconds: float) -> None:
        self.at = self._gw.mono + seconds

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        self._gw.alarms.remove(self)
        return self.fired or self._gw.mono >= self.at


@dataclass
class FakeTerminal:
    """Scripted standard input and recorded output; not a tty unless the test says so."""

    stdin: str = ""
    stdout: str = ""
    stderr: str = ""
    width: int = 80
    tty: dict[int, bool] = field(default_factory=dict)


@dataclass
class FakeCrontab:
    """A recording crontab: every ``crontab -`` write is kept, byte for byte."""

    table: bytes | None = None
    writes: list[bytes] = field(default_factory=list)
    fail_with: str | None = None


class FakeGateway:
    """Implements ``memoctl.gateway.Gateway`` with no real home, clock, terminal or crontab.

    Files live under ``root``: relative paths resolve against ``root`` (never the working
    directory) and every path is confined to it (symlinks resolved).
    ``run`` never starts a process: results are scripted with ``script_run``.

    Known deviations of the crontab fakes (this class and ``tests/fakebin/crontab``) from a
    real crontab: unknown or extra arguments exit 64 (real: 1 with a usage message); no
    "crontab: installing new crontab" message on stderr; no syntax validation of the table;
    input without a trailing newline, or empty input, is accepted as is.
    """

    def __init__(self, root: Path, *, seed: int = 0, start: datetime = EPOCH) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._home = self.root / "home"
        self._home.mkdir(exist_ok=True)
        self.clock = FakeClock(start)
        self.terminal = FakeTerminal()
        self.crontab = FakeCrontab()
        self.login: str | None = "tester"
        self.uid_value = os.getuid()  # tests set another value to simulate a foreign owner
        self.environ: dict[str, str] = {}
        self.argv_value: list[str] = []
        # Open descriptors by name ("r" or "w"); a test closes one with ``del gw.fds[1]``.
        self.fds: dict[int, str] = {0: "r", 1: "w", 2: "w"}
        self.next_free_fd = 0
        self.devnull_fails = False
        self.fd_calls: list[tuple[str, int] | tuple[str, int, int]] = []
        self.runs: list[tuple[list[str], str | bytes | None, float | None]] = []
        self._scripted: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self._random = random.Random(seed)
        self.mono = 1000.0
        self.alarms: list[FakeAlarm] = []
        self.exits: list[int] = []

    def now(self) -> datetime:
        return self.clock.now()

    def home(self) -> Path:
        return self._home

    def login_name(self) -> str | None:
        return self.login

    def argv(self) -> list[str]:
        return list(self.argv_value)

    def fd_is_open(self, fd: int) -> bool:
        self.fd_calls.append(("fstat", fd))
        return fd in self.fds

    def open_devnull(self, *, write: bool) -> int:
        if self.devnull_fails:
            raise OSError(errno.EMFILE, "too many open files")
        fd = self.next_free_fd
        while fd in self.fds:
            fd += 1
        self.fds[fd] = "w" if write else "r"
        self.fd_calls.append(("open", fd))
        return fd

    def dup_onto(self, src: int, dst: int) -> None:
        self.fds[dst] = self.fds[src]
        self.fd_calls.append(("dup2", src, dst))

    def release_fd(self, fd: int) -> None:
        self.fds.pop(fd, None)
        self.fd_calls.append(("close", fd))

    def monotonic(self) -> float:
        return self.mono

    def alarm_arm(self, seconds: float, on_alarm: Callable[[], None]) -> FakeAlarm:
        if not 0 < seconds <= 3600:
            raise ValueError("alarm seconds out of range")
        alarm = FakeAlarm(self, seconds, on_alarm)
        self.alarms.append(alarm)
        return alarm

    def advance_mono(self, seconds: float) -> None:
        """Move the monotonic clock; an armed alarm that runs out calls its handler (as the
        signal would, between two bytecodes)."""
        self.mono += seconds
        for alarm in list(self.alarms):
            if not alarm.fired and self.mono >= alarm.at:
                alarm.fired = True
                alarm.on_alarm()

    def exit_now(self, code: int) -> NoReturn:
        self.exits.append(code)
        raise FakeExit(code)

    def stdin_read(self) -> str:
        return self.terminal.stdin

    def write_out(self, text: str, *, bound: float | None = None) -> None:
        self.terminal.stdout += text

    def write_err(self, text: str, *, bound: float | None = None) -> None:
        self.terminal.stderr += text

    def isatty(self, fd: int) -> bool:
        return self.terminal.tty.get(fd, False)

    def terminal_width(self) -> int:
        return self.terminal.width

    def random_hex(self, nbytes: int) -> str:
        if nbytes < 1:
            raise ValueError("nbytes must be positive")
        return self._random.randbytes(nbytes).hex()

    def _confined(self, path: Path) -> Path:
        if "\0" in str(path):
            raise GatewayError(f"fake gateway: embedded NUL in path {str(path)!r}")
        resolved = (self.root / path).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise GatewayError(f"fake gateway: {path} is outside {self.root}")
        return resolved

    def _confined_nofollow(self, path: Path) -> Path:
        """Like ``_confined`` but the last component is not resolved (for ``lstat``)."""
        if "\0" in str(path):
            raise GatewayError(f"fake gateway: embedded NUL in path {str(path)!r}")
        full = self.root / path
        parent = self._confined(full.parent)
        return parent / full.name

    def uid(self) -> int:
        return self.uid_value

    def env(self, name: str) -> str | None:
        return self.environ.get(name)

    def stat_path(self, path: Path, *, follow: bool) -> PathInfo | None:
        target = self._confined(path) if follow else self._confined_nofollow(path)
        try:
            return _info(target.stat() if follow else target.lstat())
        except (FileNotFoundError, NotADirectoryError):
            return None

    def list_dir(self, path: Path) -> list[str]:
        return os.listdir(self._confined(path))

    def open_file(self, path: Path, limit: int) -> FileRead | None:
        target = self._confined_nofollow(path)
        try:
            info = _info(target.lstat())
        except (FileNotFoundError, NotADirectoryError):
            return None
        if info.kind != "file":
            return FileRead(PathInfo(info.kind, info.uid, info.mode), b"")
        with target.open("rb") as handle:
            return FileRead(info, handle.read(limit + 1))

    def open_dir(self, path: Path, *, follow: bool) -> DirHandle | None:
        # The path is confined to ``root`` when opened. The handle is a real descriptor and, as
        # with the real gateway, its own ``open_dir(follow=True)`` can follow an absolute
        # symlink out of ``root``; the fake does not prevent that.
        target = self._confined(path) if follow else self._confined_nofollow(path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if not follow:
            flags |= os.O_NOFOLLOW
        try:
            return _RealDirHandle(os.open(target, flags))
        except FileNotFoundError:
            return None

    def read_bytes(self, path: Path) -> bytes:
        return self._confined(path).read_bytes()

    def write_bytes(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        # Same steps as the real gateway (temporary file, exact mode, replace), minus fsync.
        if not isinstance(data, bytes):
            raise TypeError(f"write_bytes needs bytes, got {type(data).__name__}")
        target = self._confined_nofollow(path)
        temp = target.with_name(f"{TEMP_PREFIX}{self._random.randbytes(16).hex()}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            try:
                os.fchmod(fd, mode)
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def script_run(
        self, argv: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        self._scripted[tuple(argv)] = subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def run(
        self, argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        if key not in self._scripted:
            raise AssertionError(f"unscripted gateway run: {argv!r}")
        self.runs.append((list(argv), stdin, timeout))
        return self._scripted[key]

    def crontab_read(self) -> bytes | None:
        self._check_crontab()
        return self.crontab.table

    def crontab_write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError(f"crontab_write needs bytes, got {type(data).__name__}")
        self._check_crontab()
        self.crontab.writes.append(data)
        self.crontab.table = data

    def _check_crontab(self) -> None:
        if self.crontab.fail_with is not None:
            raise GatewayError(f"crontab failed: {self.crontab.fail_with}")
