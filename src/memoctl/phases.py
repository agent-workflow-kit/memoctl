"""``--verbose`` phase lines (interface.md, Output).

A command calls :func:`mark` after each phase it finishes; with ``--verbose`` the elapsed
milliseconds since the previous mark are recorded under that phase's name, and without it the
call does nothing. The lines are written to stderr in one write by :meth:`Phases.flush`, after
the command returned and so after its disarm point, its stdout and its warnings: nothing is
printed while the deadline can still fire, and a command that fails (raises) prints none (its
documented error line stays the only thing on stderr).

A line is ``verbose (<phase>): <n> ms``. The phase is a fixed identifier chosen in this package
(checked against ``[a-z][a-z_]*``, so no value of the call, note text or environment can reach
a line), ``<n>`` an integer: nothing here can carry a body, a secret or terminal control bytes.
Time comes from the gateway's monotonic clock.
"""

from __future__ import annotations

import re
from types import TracebackType

from memoctl.gateway import Gateway

__all__ = ["Phases", "current", "mark"]

_PHASE = re.compile(r"[a-z][a-z_]*")
_MAX_PHASES = 64  # a command has a handful; a runaway loop cannot grow the buffer


class Phases:
    def __init__(self, gateway: Gateway, *, enabled: bool) -> None:
        self._gateway = gateway
        self._enabled = enabled
        self._last = 0.0
        self._lines: list[str] = []
        self._installed = False
        self._previous: Phases | None = None

    def __enter__(self) -> Phases:
        global _CURRENT
        # The clock first: when it raises, no recorder is left installed.
        self._last = self._gateway.monotonic() if self._enabled else 0.0
        self._previous = _CURRENT
        _CURRENT = self
        self._installed = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        global _CURRENT
        if self._installed:
            self._installed = False
            _CURRENT = self._previous
            self._previous = None

    def mark(self, phase: str) -> None:
        """End ``phase``: record the milliseconds since the previous mark (or the start)."""
        if not self._enabled:
            return
        if _PHASE.fullmatch(phase) is None:  # a bug in this package, never input
            raise ValueError("a phase name is a fixed lowercase identifier")
        now = self._gateway.monotonic()
        elapsed = max(0, int((now - self._last) * 1000))
        self._last = now
        if len(self._lines) < _MAX_PHASES:
            self._lines.append(f"verbose ({phase}): {elapsed} ms\n")

    def flush(self) -> None:
        """Write the recorded lines to stderr in one write (never raises: the result was fixed
        before this, so a failure to write changes nothing)."""
        if not self._lines:
            return
        text, self._lines = "".join(self._lines), []
        try:
            self._gateway.write_err(text)
        except Exception:  # noqa: BLE001 - as every write after the result: ignored
            pass


class _Inert:
    def mark(self, phase: str) -> None:
        pass


_CURRENT: Phases | None = None
_INERT = _Inert()


def current() -> Phases | _Inert:
    """The installed recorder, or an inert one (a command run alone in a unit test)."""
    return _CURRENT if _CURRENT is not None else _INERT


def mark(phase: str) -> None:
    current().mark(phase)
