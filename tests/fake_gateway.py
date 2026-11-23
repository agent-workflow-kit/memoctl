"""Test fakes for ``memoctl.gateway``; nothing under ``src/`` imports this module."""

from __future__ import annotations

import os
import random
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memoctl.gateway import BodySourceError, FileRead, GatewayError, PathInfo, _info

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


@dataclass
class FakeTerminal:
    """Scripted standard input and recorded output; not a tty unless the test says so."""

    stdin: str | bytes = ""
    stdout: str = ""
    stderr: str = ""
    width: int = 80
    tty: dict[int, bool] = field(default_factory=dict)
    # A ``BodySourceError`` reason raised by ``read_stdin_bounded`` (terminal, not_regular,
    # unreadable), to model a stdin the real gateway refuses.
    stdin_error: str | None = None


@dataclass
class FakeCrontab:
    """A recording crontab: every ``crontab -`` write is kept, byte for byte."""

    table: bytes | None = None
    writes: list[bytes] = field(default_factory=list)
    fail_with: str | None = None


def _stdin_bytes(data: str | bytes) -> bytes:
    """UTF-8 with ``surrogateescape`` (the bytes a real stdin would carry); a lone surrogate
    outside U+DC80 to U+DCFF is written as its three-byte form instead of raising."""
    if isinstance(data, bytes):
        return data
    try:
        return data.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return b"".join(
            bytes([ord(c) - 0xDC00])
            if 0xDC80 <= ord(c) <= 0xDCFF
            else c.encode("utf-8", "surrogatepass")
            for c in data
        )


class FakeGateway:
    """Implements ``memoctl.gateway.Gateway`` with no real home, clock, terminal or crontab.

    Files live under ``root``: relative paths resolve against ``root`` (never the working
    directory) and every path is confined to it (symlinks resolved).
    ``run`` never starts a process: results are scripted with ``script_run``.

    Known deviations of ``read_file_bounded`` from the real gateway: it is hermetic, so a
    path that resolves outside ``root`` (an absolute path to a real file, say) answers
    ``unreadable`` where the real gateway reads it; the type checks (symlink in the last
    component, then not a regular file) run first, in the real order.

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
        self.runs: list[tuple[list[str], str | bytes | None, float | None]] = []
        self._scripted: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self._random = random.Random(seed)

    def now(self) -> datetime:
        return self.clock.now()

    def home(self) -> Path:
        return self._home

    def login_name(self) -> str | None:
        return self.login

    def read_stdin_bounded(self, nbytes: int) -> bytes:
        if self.terminal.stdin_error is not None:
            raise BodySourceError(self.terminal.stdin_error)
        if self.terminal.tty.get(0, False):
            raise BodySourceError("terminal")
        return _stdin_bytes(self.terminal.stdin)[:nbytes]

    def read_file_bounded(self, path: Path, nbytes: int) -> bytes:
        if "\0" in str(path):
            raise BodySourceError("unreadable")
        try:
            mode = (self.root / path).lstat().st_mode
        except OSError:
            raise BodySourceError("unreadable") from None
        if stat.S_ISLNK(mode):
            raise BodySourceError("symlink")
        if not stat.S_ISREG(mode):
            raise BodySourceError("not_regular")
        try:
            target = self._confined(path)
        except GatewayError:
            raise BodySourceError("unreadable") from None
        try:
            with target.open("rb") as f:
                return f.read(nbytes)
        except OSError:
            raise BodySourceError("unreadable") from None

    def write_out(self, text: str) -> None:
        self.terminal.stdout += text

    def write_err(self, text: str) -> None:
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

    def read_bytes(self, path: Path) -> bytes:
        return self._confined(path).read_bytes()

    def write_bytes(self, path: Path, data: bytes) -> None:
        self._confined(path).write_bytes(data)

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
