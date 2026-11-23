"""The table of subcommands. One line per command; an issue that adds a command adds its
line here and nothing else, so sibling branches conflict on one line at most."""

from __future__ import annotations

from memoctl import cmd_add, cmd_list, cmd_show
from memoctl.cli_args import Command

COMMANDS: tuple[Command, ...] = (
    cmd_add.COMMAND,
    cmd_list.COMMAND,
    cmd_show.COMMAND,
    # todo(#23): search
    # todo(#27): context
    # todo(#30): delete
    # todo(#31): prune
    # todo(#32): validate
    # todo(#33): cron
)
