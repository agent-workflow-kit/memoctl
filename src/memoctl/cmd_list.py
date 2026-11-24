"""``memoctl list`` (interface.md, ``list``).

Order: arguments are checked by the parser (grammars, ``--limit``), the store is opened
read-only (a missing store is an empty result; no state directory, ``state.db`` or row is
created or modified, though SQLite may add ``-wal``/``-shm`` sidecars on a read-only WAL
open), one pass reads and counts (``notes_read.scan``), the deadline is disarmed, then the
output is written. Nothing is printed before the disarm point, so a deadline never leaves
partial output.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable

from memoctl import deadline
from memoctl.cli_args import Command
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError, matches, parse_label
from memoctl.notes_read import Filters, ListResult, open_read_store, scan
from memoctl.render import Rendered, emit, format_list

MAX_LIMIT = 500
DEFAULT_LIMIT = 50
_LIMIT = re.compile(r"[1-9][0-9]{0,2}")
HIDDEN_LINE = "{} superseded or expired notes hidden; use --all\n"


def grammar_arg(field: str) -> Callable[[str], str]:
    def convert(text: str) -> str:
        if not matches(field, text):
            raise GrammarError(field)
        return text

    return convert


def limit_arg(text: str) -> int:
    if _LIMIT.fullmatch(text) is None or int(text) > MAX_LIMIT:
        raise GrammarError("limit")
    return int(text)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=grammar_arg("repo"), default=None, help="only this repo id")
    parser.add_argument("--topic", type=grammar_arg("topic"), default=None, help="only this topic")
    parser.add_argument("--kind", type=grammar_arg("kind"), default=None, help="only this kind")
    parser.add_argument(
        "--label",
        type=parse_label,
        action="append",
        default=[],
        help="only notes with this key=value (repeatable; every one must match)",
    )
    parser.add_argument("--all", action="store_true", help="include superseded and expired notes")
    parser.add_argument(
        "--limit", type=limit_arg, default=DEFAULT_LIMIT, help="at most this many notes (1 to 500)"
    )


def empty_result() -> ListResult:
    return ListResult([], 0, 0)


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    filters = Filters(
        repo=args.repo,
        topic=args.topic,
        kind=args.kind,
        labels=frozenset(args.label),
        include_all=args.all,
        limit=args.limit,
    )
    now = int(gateway.now().timestamp())
    store = open_read_store(gateway)
    if store is None:
        result = empty_result()
    else:
        with store:
            result = scan(store, filters, now)
    deadline.current().disarm()
    print_result(args, gateway, result)
    return 0


def print_result(args: argparse.Namespace, gateway: Gateway, result: ListResult) -> None:
    """The output of ``list`` and ``search``: one compact JSON line, or the rendered lines.
    ``--quiet`` drops the status lines on stderr, never the data."""
    if args.json:
        document = result.document()
        gateway.write_out(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
        if result.hidden > 0 and not args.quiet:
            gateway.write_err(HIDDEN_LINE.format(result.hidden))
        return
    rendered = format_list(result.notes, result.total, len(result.notes), result.hidden)
    if args.quiet:
        rendered = Rendered(rendered.out, "", rendered.warnings)
    emit(gateway, rendered)


COMMAND = Command("list", "list notes, newest first", run, configure)
