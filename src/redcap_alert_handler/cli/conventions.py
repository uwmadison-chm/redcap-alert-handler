# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Logging, color, and stream conventions shared by every CLI endpoint."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Annotated, TextIO

import typer
from rich.console import Console

LOGGER_NAME = "redcap_alert_handler"

# Shared by every endpoint that needs config or secrets (doctor now; auth and
# watch later) so the flags, envvars, and help text can't drift apart.
ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        envvar="RAH_CONFIG",
        help="Path to the main configuration TOML file.",
    ),
]

SecretsOption = Annotated[
    Path | None,
    typer.Option(
        "--secrets",
        envvar="RAH_SECRETS",
        help="Path to the secrets TOML file.",
    ),
]

# auth can't do anything without credentials, so its secrets flag is
# required. Same flag, envvar, and help as SecretsOption -- only the
# optionality differs.
RequiredSecretsOption = Annotated[
    Path,
    typer.Option(
        "--secrets",
        envvar="RAH_SECRETS",
        help="Path to the secrets TOML file.",
    ),
]

# The logging flags live on the root callback *and* on each subcommand, so
# both `rah -v doctor` and `rah doctor -v` work. The root configures logging
# first; a subcommand reconfigures only when one of its own flags was set,
# so the flag closest to the command wins.
VerboseOption = Annotated[
    bool,
    typer.Option("--verbose", "-v", envvar="RAH_DEBUG", help="Set log level to DEBUG."),
]

QuietOption = Annotated[
    bool,
    typer.Option("--quiet", "-q", help="Set log level to ERROR."),
]

NoColorOption = Annotated[
    bool,
    typer.Option("--no-color", help="Turn off color in logging."),
]

_LEVEL_STYLES = {
    logging.DEBUG: "dim",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold red",
}


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the redcap_alert_handler hierarchy."""
    return logging.getLogger(name)


def resolve_log_level(verbose: bool, quiet: bool) -> tuple[int, str | None]:
    """Turn -v/-q into a log level, plus a warning to emit if both were given."""
    if verbose and quiet:
        return logging.DEBUG, "both --verbose and --quiet given; using DEBUG"
    if verbose:
        return logging.DEBUG, None
    if quiet:
        return logging.ERROR, None
    return logging.INFO, None


def resolve_use_color(no_color_flag: bool, stream: TextIO) -> bool:
    """Decide whether to color output for stream, honoring the no-color.org env vars."""
    if no_color_flag:
        return False
    # no-color.org: the variable disables color if set to any non-empty value.
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("RAH_NO_COLOR"):
        return False
    return stream.isatty()


class _LineHandler(logging.Handler):
    """Writes one styled line per record through a rich Console."""

    def __init__(self, console: Console) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        # Everything under try, like stdlib StreamHandler: a handler can
        # outlive its stream, and logging must never take the program down.
        try:
            message = self.format(record)
            style = _LEVEL_STYLES.get(record.levelno)
            self._console.print(message, style=style, highlight=False, soft_wrap=True, markup=False)
        except Exception:
            self.handleError(record)


def setup_logging(verbose: bool, quiet: bool, no_color: bool) -> None:
    """Resolve the logging flags and install the handler, warning on -v -q."""
    level, warning = resolve_log_level(verbose, quiet)
    use_color = resolve_use_color(no_color, sys.stderr)
    configure_logging(level, use_color)
    if warning:
        get_logger(LOGGER_NAME).warning(warning)


def configure_logging(level: int, use_color: bool, stream: TextIO | None = None) -> None:
    """Install rah's log handler on the package logger. Safe to call more than once."""
    if stream is None:
        stream = sys.stderr
    logger = get_logger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(level)

    console = Console(file=stream, no_color=not use_color)
    handler = _LineHandler(console)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
