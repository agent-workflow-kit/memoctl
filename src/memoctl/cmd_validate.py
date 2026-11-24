"""``memoctl validate`` (interface.md, ``validate``).

Order: the four checks run through :func:`memoctl.validation.validate_store` (read-only; the
deadline's progress handler is installed on the one store it opens), the deadline is disarmed,
then the output is written. Nothing is printed before the disarm point, so a deadline never
leaves partial output; a deadline that fires inside a check is ``StoreError("timeout")``, which
propagates to the error envelope (exit 12). ``validate`` takes no arguments of its own and no
``--dry-run``: anything else is a usage error from the parser.

A failed check prints the document (``--json``) or the check lines on stdout and one status
line on stderr, an error line that ``--quiet`` does not suppress. Passing runs print nothing on
stderr. ``--quiet`` never suppresses the data.
"""

from __future__ import annotations

import argparse
import json

from memoctl import deadline, phases
from memoctl.cli_args import Command
from memoctl.gateway import Gateway
from memoctl.render import emit, format_validate
from memoctl.validation import ValidateResult, validate_store


def run(args: argparse.Namespace, gateway: Gateway) -> int:
    result = validate_store(gateway, configure=deadline.current().configure_store)
    phases.mark("validate")
    deadline.current().disarm()
    print_result(args, gateway, result)
    phases.mark("output")
    return result.exit_code


def status_line(result: ValidateResult) -> str:
    """The one stderr line of a failed check (interface.md, Output on failure), built from the
    check's reason and its fixed message; empty when every check passed."""
    for check in result.checks:
        if check.ok:
            continue
        # The shapes of ``Failure.status_line``; a Failure is not built here because its
        # envelope rules (details of ``schema_newer``) describe an envelope this never prints.
        if check.reason == "config_invalid":
            return f"config_invalid: {check.message}\n"
        if check.reason == "unsafe_state":
            return f"refused (unsafe_state): {check.message}\n"
        return f"failed ({check.reason}): {check.message}\n"
    return ""


def print_result(args: argparse.Namespace, gateway: Gateway, result: ValidateResult) -> None:
    """The document (one compact JSON line) or the check lines on stdout, then the status line
    of a failed check on stderr. ``--quiet`` drops neither: the data is never suppressed and the
    status line is an error line."""
    document = result.document()
    if args.json:
        gateway.write_out(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
    else:
        emit(gateway, format_validate(document))
    line = status_line(result)
    if line:
        gateway.write_err(line)


COMMAND = Command("validate", "check the store (read-only)", run)
