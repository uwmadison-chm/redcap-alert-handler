# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""`rah doctor` and `rah init`: local diagnostics and mailbox provisioning.

The checks themselves live in `checks`, where `rah process` can run the same
list at startup. What's here is the command around them: the flags, the
verbose config dump, the machine-readable and file copies of the report, and
the exit code. Both commands run the same core, so a check can never mean one
thing to one command and something else to the other; `init` is the one that
creates what's missing, and `doctor` only ever reports.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.pretty import pretty_repr

from redcap_alert_handler.checks import CheckResult, json_report, render_check, report, run_checks
from redcap_alert_handler.cli.conventions import (
    ConfigOption,
    NoColorOption,
    QuietOption,
    SecretsOption,
    VerboseOption,
    setup_logging,
)
from redcap_alert_handler.logs import get_logger

logger = get_logger(__name__)

JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Write a machine-readable report to stdout."),
]

OutputOption = Annotated[
    Path | None,
    typer.Option("--output", "-o", help="Write the report to a file instead of the terminal."),
]


def doctor(
    config: ConfigOption,
    secrets: SecretsOption = None,
    json_output: JsonOption = False,
    output: OutputOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Check the config, the secrets if given, the token cache, and the mailbox.

    Runs the full check list and reports each result, changing nothing.
    Graph-side checks only run when config, secrets, and a fresh token are all
    in hand; otherwise they're skipped rather than failed. Missing folders and
    categories are reported, and `rah init` is what creates them. Exits 0 if
    every check that ran passed, 1 if any failed.
    """
    _run_doctor(config, secrets, False, json_output, output, verbose, quiet, no_color)


def init(
    config: ConfigOption,
    secrets: SecretsOption = None,
    json_output: JsonOption = False,
    output: OutputOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Provision the mailbox: create the rah folders and categories it needs.

    The documented first-run step after `rah auth`. It runs the same checks
    `doctor` does and creates whatever's missing, so running it against an
    already-provisioned mailbox is a no-op with a report.
    """
    _run_doctor(config, secrets, True, json_output, output, verbose, quiet, no_color)


def _run_doctor(
    config: Path,
    secrets: Path | None,
    fix: bool,
    json_output: bool,
    output: Path | None,
    verbose: bool,
    quiet: bool,
    no_color: bool,
) -> None:
    # The root callback already configured logging; only reconfigure when one
    # of the subcommand's own flags was set, so the flag closest to the
    # command wins.
    if verbose or quiet or no_color:
        setup_logging(verbose, quiet, no_color)

    check_report = run_checks(config, secrets, fix)
    report(check_report.results)

    if check_report.config is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("loaded config:\n%s", pretty_repr(check_report.config))

    _emit_report(check_report.results, json_output, output)

    if not check_report.ok:
        raise typer.Exit(1)


def _emit_report(results: tuple[CheckResult, ...], json_output: bool, output: Path | None) -> None:
    # Human stderr logging already happened; this is the extra machine or file
    # copy. --json to stdout (or the file); -o without --json writes the plain
    # check lines to the file.
    if json_output:
        payload = json.dumps(json_report(results))
        if output is None:
            typer.echo(payload)
            return
        _write_file(output, payload)
        return
    if output is not None:
        text = "\n".join(text for result in results for _, text in render_check(result))
        _write_file(output, text)


def _write_file(output: Path, text: str) -> None:
    try:
        output.write_text(text + "\n")
    except OSError as e:
        logger.error("💥 couldn't write the report to %s: %s", output, e.strerror)
        raise typer.Exit(1) from e
