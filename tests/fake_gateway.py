"""Test fakes for ``memoctl.gateway``; nothing under ``src/`` imports this module."""

from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memoctl.gateway import GatewayError

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
        self.runs: list[tuple[list[str], str | bytes | None, float | None]] = []
        self._scripted: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self._random = random.Random(seed)

    def now(self) -> datetime:
        return self.clock.now()

    def home(self) -> Path:
        return self._home

    def stdin_read(self) -> str:
        return self.terminal.stdin

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
