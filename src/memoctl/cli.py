"""Command-line entry point for memoctl.

The order in :func:`main` is fixed by interface.md (Output): the standard descriptors are
checked first, then the ``--json`` pre-scan, then parsing. Everything printed goes through the
gateway as UTF-8; no ``print``, no traceback, no exception text ever reaches the user.
"""

from __future__ import annotations

from collections.abc import Sequence

from memoctl.cli_args import (
    Command,
    ParserExit,
    build_parser,
    leading_flag_is_refused,
    prescan_json,
)
from memoctl.cli_commands import COMMANDS
from memoctl.cli_errors import (
    OK,
    SIGINT_EXIT,
    Failure,
    UsageError,
    failure_for,
)
from memoctl.gateway import Gateway, RealGateway

STANDARD_FDS = ((0, False), (1, True), (2, True))  # (descriptor, open for writing)


def ensure_standard_descriptors(gateway: Gateway) -> None:
    """Open ``/dev/null`` on each of descriptors 0, 1 and 2 that is closed, in that order.

    Each open must return exactly the descriptor it fills; when it returns another one the
    descriptor is moved with ``dup2`` and the extra closed. Best effort: when ``/dev/null``
    cannot be opened there is nowhere to report it, so the run continues.
    """
    for fd, write in STANDARD_FDS:
        if gateway.fd_is_open(fd):
            continue
        try:
            got = gateway.open_devnull(write=write)
            if got != fd:
                gateway.dup_onto(got, fd)
                gateway.release_fd(got)
        except OSError:
            continue


def report(gateway: Gateway, failure: Failure, *, json_mode: bool) -> int:
    """Print ``failure``: the envelope on stdout with ``--json``, the status line on stderr."""
    if json_mode:
        gateway.write_out(failure.envelope())
    gateway.write_err(failure.status_line())
    return failure.exit_code


def main(
    argv: Sequence[str] | None = None,
    gateway: Gateway | None = None,
    commands: Sequence[Command] = COMMANDS,
) -> int:
    gateway = gateway or RealGateway()
    ensure_standard_descriptors(gateway)
    args = list(gateway.argv() if argv is None else argv)
    json_mode = prescan_json(args)
    try:
        return _run(args, gateway, commands)
    except ParserExit as done:
        gateway.write_out(done.text)
        return OK
    except KeyboardInterrupt:
        return SIGINT_EXIT  # 128 + SIGINT: no output
    except Exception as exc:  # noqa: BLE001 - the last line of defence; nothing is rendered
        failure = failure_for(exc)
        # Drop the traceback (its frames hold locals such as a body) before anything else.
        exc.__traceback__ = None
        return report(gateway, failure, json_mode=json_mode)


def _run(args: list[str], gateway: Gateway, commands: Sequence[Command]) -> int:
    if leading_flag_is_refused(args):
        raise UsageError("argv")
    parser = build_parser(commands)
    namespace = parser.parse_args(args)
    if namespace.command is None:
        raise UsageError("argv")
    by_name = {command.name: command for command in commands}
    return by_name[namespace.command].run(namespace, gateway)
