"""Argument parsing for the command line (interface.md, Argument parsing).

``argparse`` is used only to split the arguments. Its own output and exit are neutralized:
help and version text is collected and printed by the caller through the gateway, and every
parse error becomes a :class:`UsageError` with field ``argv`` and no text from the arguments
(argparse's messages echo them).

Rules enforced here:

- global flags (``--json``, ``--dry-run``, ``--actor``, ``--verbose``, ``--quiet``) exist only
  on the subcommand parsers, so before the subcommand only ``--help`` or ``--version``,
  alone, is accepted; any other leading flag, or ``--``, is a usage error;
- option and subcommand names are exact (no abbreviations), and ``-h`` is not an option;
- the ``--json`` pre-scan is :func:`prescan_json`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NoReturn

from memoctl import __version__
from memoctl.cli_errors import UsageError
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError

HELP_WIDTH = 80  # fixed: help must not depend on COLUMNS or the terminal

Run = Callable[[argparse.Namespace, Gateway], int]


@dataclass(frozen=True)
class Command:
    """One subcommand. A command module exports one ``Command``; ``cli_commands.py`` lists it.

    ``configure`` adds the command's own arguments (or nested subparsers) to its parser.
    ``dry_run`` says whether ``--dry-run`` is accepted. ``globals_on_command`` is false for a
    command whose global flags belong after a nested subcommand (``cron install --json``): its
    ``configure`` then calls :func:`add_global_flags` on each leaf parser itself.
    """

    name: str
    summary: str
    run: Run
    configure: Callable[[argparse.ArgumentParser], None] = lambda parser: None
    dry_run: bool = False
    globals_on_command: bool = True


class ParserExit(Exception):
    """Help or version text was requested; the parser did not print or exit."""

    def __init__(self, text: str) -> None:
        super().__init__("parser exit")
        self.text = text


# The members of ``ArgumentParser`` that print or exit, which ``_Parser`` overrides.
SEAM_MEMBERS = ("error", "exit", "_print_message")


def check_argparse_seam(parser_class: type) -> None:
    """Fail loudly when ``parser_class`` lacks a member that :class:`_Parser` overrides.

    A Python that renames one (they are private in part) would let argparse print and exit
    on its own, so the module refuses to import instead.
    """
    for name in SEAM_MEMBERS:
        if not hasattr(parser_class, name):
            raise ImportError(f"argparse.ArgumentParser has no {name}(): memoctl cannot mute it")


check_argparse_seam(argparse.ArgumentParser)


class _Formatter(argparse.HelpFormatter):
    """The default help layout at a fixed width (the default reads the terminal)."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, width=HELP_WIDTH)


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that never prints and never exits."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("formatter_class", _Formatter)
        super().__init__(*args, add_help=False, allow_abbrev=False, **kwargs)  # type: ignore[arg-type]
        self._printed: list[str] = []
        self.add_argument("--help", action="help", help="show this help and exit")

    def error(self, message: str) -> NoReturn:
        # argparse wraps an exception of a ``type=`` converter in an ``ArgumentError`` (the
        # converter's exception is its ``__context__``): keep ours, which carries the field.
        cause = sys.exc_info()[1]
        while cause is not None:
            if isinstance(cause, (GrammarError, UsageError)):
                raise cause
            cause = cause.__context__
        raise UsageError("argv")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        # Only help and version reach here with status 0 (their text was collected by
        # ``_print_message``); anything else is a parse failure.
        if status != 0:
            raise UsageError("argv")
        raise ParserExit("".join(self._printed))

    def _print_message(self, message: str, file: object = None) -> None:
        self._printed.append(message)


def add_global_flags(parser: argparse.ArgumentParser, *, dry_run: bool) -> None:
    """The flags every command accepts after its name (interface.md, Global flags)."""
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    parser.add_argument("--actor", default=None, help="who is acting (recorded in the log)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--verbose", action="store_true", help="one line per phase on stderr")
    group.add_argument("--quiet", action="store_true", help="suppress status output")
    if dry_run:
        parser.add_argument("--dry-run", action="store_true", help="change nothing")


def build_parser(commands: Sequence[Command]) -> _Parser:
    root = _Parser(
        prog="memoctl",
        description="Uncommitted shared knowledge that agents leave for each other.",
    )
    root.add_argument("--version", action="version", version=f"memoctl {__version__}")
    sub = root.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    for command in commands:
        parser = sub.add_parser(command.name, help=command.summary, description=command.summary)
        if command.globals_on_command:
            add_global_flags(parser, dry_run=command.dry_run)
        command.configure(parser)
    return root


def prescan_json(argv: Sequence[str]) -> bool:
    """Whether ``--json`` appears in ``argv``, looked up before anything is parsed.

    The scan stops at ``--``; ``--json`` must be an exact token. ``--help`` and ``--version``
    are exempt: with either, ``--json`` is ignored and the answer is false.
    """
    found = False
    for token in argv:
        if token == "--":
            break
        if token in ("--help", "--version"):
            return False
        if token == "--json":
            found = True
    return found


def leading_flag_is_refused(argv: Sequence[str]) -> bool:
    """True for a flag (or ``--``) before the subcommand, except ``--help``/``--version`` alone."""
    if not argv or not argv[0].startswith("-") or argv[0] == "-":
        return False
    return not (len(argv) == 1 and argv[0] in ("--help", "--version"))
