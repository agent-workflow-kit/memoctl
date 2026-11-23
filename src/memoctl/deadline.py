"""The command deadline (interface.md, Deadlines).

A command runs inside ``with Deadline(gateway, json_mode=...) as deadline:`` and arms it with
:meth:`Deadline.arm`. When the wall-clock timer runs out the handler writes the pre-built
timeout line to stderr and, with ``--json``, the pre-built envelope to stdout, then leaves the
process with exit 12 through :meth:`Gateway.exit_now` (``os._exit``): it never raises, so
nothing a ``finally`` or ``except`` clause does can turn a timeout into another outcome.

Every ``signal`` and ``threading`` use is in the gateway (the import-boundary test bans both
elsewhere); this module only keeps the policy:

- one deadline per process: entering a second :class:`Deadline` while one is installed, or
  arming one that is already armed, is a :class:`DeadlineError` (a bug in the caller, exit 1);
- :meth:`Deadline.disarm` is a disarm point: it stops the timer first, and when the timer had
  already run out (a deadline that passed during a commit) the command ends with exit 12
  ``timeout`` at once, so a timeout is never reported after the output started and never
  skipped when the time had passed before it;
- :meth:`Deadline.expired` is the SQLite progress callback for long statements and
  :meth:`Deadline.remaining_ms` caps every lock wait: the Python handler cannot run while SQLite
  works;
- the handler's writes are bounded (:data:`REPORT_BOUND_SECONDS` per stream), so a full pipe or a
  stopped terminal cannot keep the process from leaving.

Commands reach the installed deadline through :func:`current` (an inert one when none is
installed, as in a unit test of a command alone).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING

from memoctl.cli_errors import FAILED, Failure
from memoctl.gateway import MAX_ALARM_SECONDS, AlarmHandle, Gateway

if TYPE_CHECKING:
    from memoctl.store import Store

__all__ = [
    "COMMAND_SECONDS",
    "CONTEXT_PROBE_SECONDS",
    "TIMEOUT_EXIT",
    "Deadline",
    "DeadlineError",
    "current",
    "initial_seconds",
]

TIMEOUT_EXIT = 12
COMMAND_SECONDS = 30.0  # every command but ``context``
CONTEXT_PROBE_SECONDS = 2.0  # ``context`` until its config is loaded, then ``context.deadline``
TIMEOUT_REASON = "timeout"
REPORT_BOUND_SECONDS = (
    0.1  # per stream: the handler never waits longer on a reader that is not reading
)
BUSY_MARGIN_MS = 10  # a lock wait outlasts the deadline by this much, so the timer wins the race
# The one timeout failure: the handler's stderr line and envelope are built from it, so they
# equal what the main path prints for ``StoreError("timeout")`` (``failure_for``).
_FAILURE = Failure(FAILED, "failed", "deadline passed", TIMEOUT_REASON)
STATUS_LINE = _FAILURE.status_line()


class DeadlineError(RuntimeError):
    """The deadline was used wrongly (nested, double-armed, bad duration). A bug, not input."""


def initial_seconds(command: str) -> float:
    """The duration armed after argument parsing (``context`` re-arms after loading config)."""
    return CONTEXT_PROBE_SECONDS if command == "context" else COMMAND_SECONDS


def _checked(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise DeadlineError("a deadline is a number of seconds")
    # The range test alone also rejects nan and inf, and compares a huge int without float().
    if not 0 < seconds <= MAX_ALARM_SECONDS:
        raise DeadlineError("a deadline must be above 0 and at most an hour")
    return float(seconds)


_CURRENT: Deadline | None = None


class Deadline:
    def __init__(self, gateway: Gateway, *, json_mode: bool = False) -> None:
        self._gateway = gateway
        self._stdout = _FAILURE.envelope() if json_mode else ""
        self._stderr = STATUS_LINE
        self._handle: AlarmHandle | None = None
        self._started = 0.0
        self._at = math.inf
        self._firing = False
        self._installed = False

    # -- installation
    def __enter__(self) -> Deadline:
        global _CURRENT
        if _CURRENT is not None:
            raise DeadlineError("a deadline is already installed")
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
        try:
            self.disarm()
        finally:
            if self._installed:
                self._installed = False
                _CURRENT = None

    # -- arming
    @property
    def armed(self) -> bool:
        return self._handle is not None

    def arm(self, seconds: float) -> None:
        """Fire ``seconds`` from now."""
        seconds = _checked(seconds)
        if self._handle is not None:
            raise DeadlineError("the deadline is already armed")
        self._started = self._gateway.monotonic()
        self._at = self._started + seconds
        self._handle = self._gateway.alarm_arm(seconds, self.fire)

    def rearm(self, seconds: float) -> None:
        """Fire ``seconds`` after the moment of :meth:`arm` (``context`` loads its config under
        a short deadline, then runs under ``context.deadline`` counted from the start); at once
        when that moment has passed."""
        seconds = _checked(seconds)
        if self._handle is None:
            raise DeadlineError("the deadline is not armed")
        self._at = self._started + seconds
        left = self._at - self._gateway.monotonic()
        if left <= 0:
            self.fire()
        self._handle.reset(left)

    def disarm(self) -> None:
        """A disarm point: stop the timer; when it had already run out, exit 12 ``timeout``.
        Does nothing when not armed."""
        handle, self._handle = self._handle, None
        self._at = math.inf
        if handle is not None and handle.release():
            self.fire()

    @contextmanager
    def paused(self, then_seconds: float = COMMAND_SECONDS) -> Iterator[None]:
        """Disarmed inside the block (``delete`` waiting for an answer), armed again for
        ``then_seconds`` after it. A block that raises leaves it disarmed."""
        was_armed = self.armed
        self.disarm()
        yield
        if was_armed:
            self.arm(then_seconds)

    # -- firing
    def expired(self) -> bool:
        """The SQLite progress callback: true once the deadline passed (interrupts the statement,
        which raises ``StoreError("timeout")``)."""
        return self._handle is not None and self._gateway.monotonic() >= self._at

    def remaining_ms(self) -> int | None:
        """Milliseconds until the deadline (0 once it passed, a small margin added so that a lock
        wait capped to it ends after the timer fired); ``None`` when not armed. The store caps
        every busy timeout to this: a Python handler cannot run during a lock wait."""
        if self._handle is None:
            return None
        left = self._at - self._gateway.monotonic()
        return 0 if left <= 0 else math.ceil(left * 1000) + BUSY_MARGIN_MS

    def fire(self) -> None:
        """Write the timeout report and leave with exit 12. Writes cannot stop the exit: it is
        in a ``finally``, so a failing write or a second signal still ends in exit 12."""
        if self._firing:
            return
        self._firing = True
        try:
            if self._stdout:
                self._gateway.write_out(self._stdout, bound=REPORT_BOUND_SECONDS)
            self._gateway.write_err(self._stderr, bound=REPORT_BOUND_SECONDS)
        finally:
            self._gateway.exit_now(TIMEOUT_EXIT)

    def configure_store(self, store: Store) -> None:
        """The ``configure`` hook of ``open_store``: interrupts statements after the deadline."""
        store.set_progress_handler(self.expired)
        store.set_busy_cap(self.remaining_ms)


class _Inert:
    """What :func:`current` returns when nothing is installed: every call does nothing."""

    armed = False

    def arm(self, seconds: float) -> None:
        pass

    def rearm(self, seconds: float) -> None:
        pass

    def disarm(self) -> None:
        pass

    @contextmanager
    def paused(self, then_seconds: float = COMMAND_SECONDS) -> Iterator[None]:
        yield

    def expired(self) -> bool:
        return False

    def configure_store(self, store: Store) -> None:
        pass


_INERT = _Inert()


def current() -> Deadline | _Inert:
    """The installed deadline, or an inert one."""
    return _CURRENT if _CURRENT is not None else _INERT
