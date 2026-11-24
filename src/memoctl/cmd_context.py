"""``memoctl context`` (interface.md, ``context``): the notes a session starts with.

Its output is injected into an AI model's context, so every path either prints a block that
:mod:`memoctl.context_render` has checked, or prints nothing on stdout (exit 10, or a failure
whose status line goes to stderr). The order follows the interface:

1. arguments are parsed (exit 2); no ``--repo``, or a repo id over 255 characters, is exit 10
   before any config is read;
2. the deadline is armed for 2 s by the CLI; the config is loaded (exit 2 ``config_invalid``)
   and the timer is re-armed to ``context.deadline`` counted from the first arm;
3. the state directory and the store are opened read-only without creating anything; a missing
   or uninitialized store is exit 10;
4. selection runs in one read transaction (one snapshot), with the real sanitizer;
5. the block is rendered and checked, the deadline is disarmed, and only then is stdout written
   (a deadline never reports a timeout after output has started).

Nothing here prints note text on stderr: failures are fixed messages built by the CLI. Output
is the block bytes exactly, never terminal-escaped (it is model context, not terminal text).
"""

from __future__ import annotations

import argparse
from typing import Final

from memoctl import deadline, phases
from memoctl.cli_args import Command
from memoctl.config import load_config
from memoctl.context_render import render_json, render_text
from memoctl.gateway import Gateway
from memoctl.grammar import ADD_MAX_REPO_ID_LENGTH, GrammarError, matches, parse_label
from memoctl.notes_read import open_read_store
from memoctl.sanitize import sanitize
from memoctl.selection import Selection, SelectionConfig, select_context

NOTHING_TO_SHOW: Final = 10
NONCE_BYTES: Final = 8
MIN_BUDGET: Final = 1000  # the config floor; used only for an empty selection


def repo_arg(text: str) -> str:
    """The ``--repo`` value. A repo over 255 characters is not an error: the command exits 10
    ("nothing to show") without reading config, whatever the text holds, so it is passed on
    unchecked (and never printed). Anything else must match the repo grammar (exit 2)."""
    if len(text) > ADD_MAX_REPO_ID_LENGTH:
        return text
    if not matches("repo", text):
        raise GrammarError("repo")
    return text


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=repo_arg, default=None, help="the session's repo id")
    parser.add_argument(
        "--label",
        type=parse_label,
        action="append",
        default=[],
        help="a session label key=value (repeatable)",
    )
    parser.add_argument(
        "--fence", action="store_true", help="wrap the block in an untrusted-data fence"
    )


def _print(
    args: argparse.Namespace,
    gateway: Gateway,
    selection: Selection,
    *,
    max_chars: int,
    max_bytes: int,
) -> int:
    """Render (and check) ``selection``, disarm the deadline, then write stdout. Exit 0 when a
    block was shown, else 10. A render failure raises before anything is written."""
    nonce = gateway.random_hex(NONCE_BYTES) if args.fence else None
    render = render_json if args.json else render_text
    text = render(
        selection, fence=args.fence, nonce=nonce, max_chars=max_chars, max_bytes=max_bytes
    )
    # The gateway writes UTF-8 with errors replaced, the same bytes as encode_output(text).
    phases.mark("render")
    deadline.current().disarm()
    if text:
        gateway.write_out(text, complete=True)  # never cut off by a slow reader
    phases.mark("output")
    return 0 if selection.shown > 0 else NOTHING_TO_SHOW


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    repo: str | None = args.repo
    if repo is None or len(repo) > ADD_MAX_REPO_ID_LENGTH:
        # Nothing to select: no config is read, so the budgets are the defaults' floor; an
        # empty selection prints no block and checks none.
        return _print(
            args, gateway, Selection("", 0, (), 0), max_chars=MIN_BUDGET, max_bytes=MIN_BUDGET
        )
    config = load_config(gateway).for_repo(repo)
    deadline.current().rearm(config.context.deadline)
    phases.mark("config")
    max_chars, max_bytes = config.context.max_chars, config.context.max_bytes
    selection_config = SelectionConfig.from_config(config.context)
    store = open_read_store(gateway)
    phases.mark("open_store")
    if store is None:
        return _print(
            args, gateway, Selection(repo, 0, (), 0), max_chars=max_chars, max_bytes=max_bytes
        )
    now = int(gateway.now().timestamp())  # read only once there is a store to select from
    with store, store.read():
        selection = select_context(
            store,
            repo=repo,
            session_labels=frozenset(args.label),
            config=selection_config,
            now=now,
            sanitize=sanitize,
        )
    phases.mark("select")
    return _print(args, gateway, selection, max_chars=max_chars, max_bytes=max_bytes)


COMMAND = Command("context", "print the notes for a session start", run, configure)
