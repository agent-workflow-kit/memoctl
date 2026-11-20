"""Command-line entry point for memoctl."""

from __future__ import annotations

import argparse

from memoctl import __version__
from memoctl.gateway import Gateway, RealGateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoctl",
        description="Uncommitted shared knowledge that agents leave for each other.",
    )
    parser.add_argument("--version", action="version", version=f"memoctl {__version__}")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: list[str] | None = None, gateway: Gateway | None = None) -> int:
    gateway = gateway or RealGateway()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        gateway.write_err(parser.format_help())
        return 2
    return 0
