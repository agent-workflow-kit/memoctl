"""``memoctl show <id>`` (interface.md, ``show``).

One note in full, whatever its status. An unknown id (or a missing store) exits 10; a row that
fails the row checks exits 12 ``db_corrupt`` (raised by the store layer). The body is printed
through the renderer, so it cannot drive a terminal.
"""

from __future__ import annotations

import argparse
import json

from memoctl import deadline, phases
from memoctl.cli_args import Command
from memoctl.cli_errors import NoteNotFoundError
from memoctl.gateway import Gateway
from memoctl.grammar import GrammarError, matches
from memoctl.notes_read import fetch_note, open_read_store
from memoctl.render import Rendered, emit, format_show


def _id(text: str) -> int:
    if not matches("id", text):
        raise GrammarError("id")
    return int(text)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", type=_id, help="the note id")


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    now = int(gateway.now().timestamp())
    store = open_read_store(gateway)
    phases.mark("open_store")
    document = None
    if store is not None:
        with store:
            document = fetch_note(store, args.id, now)
    phases.mark("read")
    deadline.current().disarm()
    if document is None:
        raise NoteNotFoundError(args.id)
    if args.json:
        gateway.write_out(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
        phases.mark("output")
        return 0
    rendered: Rendered = format_show(document)
    emit(gateway, rendered)
    phases.mark("output")
    return 0


COMMAND = Command("show", "print one note in full", run, configure)
