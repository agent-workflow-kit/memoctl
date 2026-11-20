"""Gateway: the single boundary between memoctl and the outside world.

This is the only module that reads the clock, the password-database home, the terminal, the
standard descriptors, standard input, randomness or files, and the only one that starts a
process (including ``crontab``) or, later, makes an HTTP call. Tests substitute a fake
(``tests/fake_gateway.py``) or fake binaries (``tests/fakebin/``); ``tests/test_import_boundary.py``
fails if another module reaches outside directly.

Commands are always run as argv lists, never through a shell, and untrusted text is passed via
stdin or files, never in argv or the environment. Process output is decoded as UTF-8 with
``surrogateescape`` so no byte sequence raises or is lost.

A command reads the clock once with ``now()`` and hands the value down as a parameter.
"""

from __future__ import annotations

import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

ENCODING = "utf-8"
ERRORS = "surrogateescape"
DEFAULT_TERMINAL_WIDTH = 80


class GatewayError(RuntimeError):
    """An outside call failed; the message names what was attempted and how it failed."""


class Gateway(Protocol):
    """Everything memoctl needs from outside the process."""

    def now(self) -> datetime:
        """The current time, timezone-aware (UTC)."""
        ...

    def home(self) -> Path:
        """The user's home from the password database (never the ``HOME`` variable)."""
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


def run(
    argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell; output is decoded as UTF-8 with ``surrogateescape``."""
    raw = subprocess.run(
        argv,
        input=_to_bytes(stdin),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode(ENCODING, ERRORS),
        raw.stderr.decode(ENCODING, ERRORS),
    )


class RealGateway:
    """The production gateway."""

    def __init__(self, *, crontab_bin: str = "crontab") -> None:
        self._crontab_bin = crontab_bin

    def now(self) -> datetime:
        return datetime.fromtimestamp(time.time(), UTC)

    def home(self) -> Path:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)

    def stdin_read(self) -> str:
        return sys.stdin.read()

    def write_out(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def write_err(self, text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

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
        return Path(path).read_bytes()

    def write_bytes(self, path: Path, data: bytes) -> None:
        Path(path).write_bytes(data)

    def run(
        self, argv: list[str], *, stdin: str | bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run(argv, stdin=stdin, timeout=timeout)

    def crontab_read(self) -> bytes | None:
        result = self._crontab(["-l"], None)
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and b"no crontab" in result.stderr:
            return None
        raise self._failure("-l", result)

    def crontab_write(self, data: bytes) -> None:
        result = self._crontab(["-"], data)
        if result.returncode != 0:
            raise self._failure("-", result)

    def _crontab(self, args: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self._crontab_bin, *args],
            input=stdin,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def _failure(self, arg: str, result: subprocess.CompletedProcess[bytes]) -> GatewayError:
        detail = result.stderr.decode(ENCODING, ERRORS).strip()
        return GatewayError(f"crontab {arg} failed (exit {result.returncode}): {detail}")
