"""``memoctl search`` (interface.md, ``search``).

The text comes from a positional argument or ``--text-file`` (a path, or ``-`` for standard
input), at most 1024 bytes either way, and goes through Body rules items 2 to 4 and the term
rules of :mod:`memoctl.searchmatch` before the store is opened. The scan is ``list``'s (one
read transaction, one row at a time, memory bounded by ``--limit``) with the text match as one
more filter; every note in the ``--repo`` filter is tested whatever its status so that
``hidden`` counts. Output goes through the same printer as ``list``, after the deadline is
disarmed: stored text never reaches the terminal except through the renderer.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from memoctl import deadline, phases
from memoctl.body import check_text
from memoctl.bodyread import STDIN, BodyError, read_body
from memoctl.cli_args import Command
from memoctl.cli_errors import UsageError
from memoctl.cmd_list import DEFAULT_LIMIT, empty_result, grammar_arg, limit_arg, print_result
from memoctl.gateway import Gateway
from memoctl.notes_read import Filters, open_read_store, scan
from memoctl.searchmatch import Matcher, TermError, parse_terms

MAX_TEXT_BYTES = 1024


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", nargs="?", default=None, help="the search text (or --text-file)")
    parser.add_argument("--text-file", default=None, help="read the search text from a file or -")
    parser.add_argument("--repo", type=grammar_arg("repo"), default=None, help="only this repo id")
    parser.add_argument("--all", action="store_true", help="include superseded and expired notes")
    parser.add_argument(
        "--limit", type=limit_arg, default=DEFAULT_LIMIT, help="at most this many notes (1 to 500)"
    )


def _text_bytes(args: argparse.Namespace, gateway: Gateway) -> bytes:
    if (args.text is None) == (args.text_file is None):  # none, or both
        raise UsageError("argv")
    if args.text_file is not None:
        source: Path | str = STDIN if args.text_file == STDIN else Path(args.text_file)
        return read_body(gateway, source, MAX_TEXT_BYTES, field="text")
    try:  # argv carries undecodable bytes as U+DC80 to U+DCFF
        data = args.text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # a lone surrogate no byte stream holds: invalid UTF-8 below
        data = args.text.encode("utf-8", "surrogatepass")
    if len(data) > MAX_TEXT_BYTES:
        raise BodyError("too_long", MAX_TEXT_BYTES, "text")
    return data


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    text = check_text(_text_bytes(args, gateway), field="text")
    try:
        terms = parse_terms(text)
    except TermError:
        raise UsageError("text") from None
    matcher = Matcher(terms)
    phases.mark("parse")
    filters = Filters(repo=args.repo, include_all=args.all, limit=args.limit)
    now = int(gateway.now().timestamp())
    store = open_read_store(gateway)
    phases.mark("open_store")
    if store is None:
        result = empty_result()
    else:
        with store:
            result = scan(store, filters, now, lambda row: matcher.matches(row.topic, row.body))
            store.bad_rows += matcher.skipped
    phases.mark("scan")
    deadline.current().disarm()
    print_result(args, gateway, result)
    phases.mark("output")
    return 0


COMMAND = Command("search", "find notes whose topic or body contain every term", run, configure)
