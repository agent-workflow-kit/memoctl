"""``memoctl cron install|uninstall [--dry-run]`` (interface.md, ``cron``).

Order:

1. parse (``cron install --json``: the global flags belong to the leaf, never to ``cron``);
2. the home from the password database must be an absolute existing directory, like for every
   command (else exit 2 ``config_invalid`` on ``home``); ``install`` also needs its characters
   to be safe to write into a crontab. Both are checked before anything is read;
3. read the crontab through the gateway (a failure is exit 12 ``crontab_failed``);
4. find the block (:mod:`memoctl.cron_block`; a malformed one is exit 20, nothing changed);
5. unless the outcome is ``unchanged`` or ``--dry-run``: one ``crontab -`` write of the whole
   new table;
6. the deadline is disarmed, then the output.

The gateway has no lock: a ``crontab -e`` (or another ``memoctl cron``) between step 3 and 5
is lost, as the interface documents. The real ``crontab`` replaces the table atomically, so a
kill leaves the old table or the new one.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from memoctl import deadline
from memoctl.cli_args import Command, add_global_flags
from memoctl.config import ConfigError, resolve_home
from memoctl.cron_block import (
    MAX_BLOCK_LINE,
    CrontabFailedError,
    home_is_safe,
    plan_install,
    plan_uninstall,
)
from memoctl.gateway import Gateway, GatewayError
from memoctl.render import Rendered, emit, format_cron

__all__ = ["COMMAND", "run"]


def configure(parser: argparse.ArgumentParser) -> None:
    nested = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    install = nested.add_parser(
        "install", help="add or update the nightly prune entry", description="Install the block."
    )
    uninstall = nested.add_parser(
        "uninstall", help="remove the nightly prune entry", description="Remove the block."
    )
    add_global_flags(install, dry_run=True)
    add_global_flags(uninstall, dry_run=True)


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    if args.action == "install":
        home = _install_home(gateway)
        table = _read(gateway)
        outcome, new, block = plan_install(table, home)
    else:
        resolve_home(gateway)
        table = _read(gateway)
        outcome, new, block = plan_uninstall(table)
    if new is not None and not args.dry_run:
        try:
            gateway.crontab_write(new)
        except GatewayError as exc:
            raise CrontabFailedError from exc
    deadline.current().disarm()
    shown = [_text(line) for line in block]
    document: dict[str, Any] = {"outcome": outcome, "dry_run": bool(args.dry_run), "block": shown}
    if args.json:
        emit(
            gateway, Rendered(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
        )
    else:
        rendered = format_cron(outcome, dry_run=args.dry_run)
        emit(gateway, Rendered("" if args.quiet else rendered.out))
    return 0


def _install_home(gateway: Gateway) -> str:
    home = str(resolve_home(gateway))
    if not home_is_safe(home):
        raise ConfigError("home")
    return home


def _read(gateway: Gateway) -> bytes | None:
    try:
        return gateway.crontab_read()
    except GatewayError as exc:
        raise CrontabFailedError from exc


def _text(line: bytes) -> str:
    """A block line for JSON: hand-edited text may hold any bytes, so decode with replacement
    and cap at the schema's length."""
    return line.decode("utf-8", "replace")[:MAX_BLOCK_LINE]


COMMAND = Command(
    "cron",
    "install or remove the nightly prune entry in the crontab",
    run,
    configure,
    dry_run=True,
    globals_on_command=False,
)
