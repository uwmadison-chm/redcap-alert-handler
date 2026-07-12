# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Option and flag conventions shared by every CLI endpoint.

The logging system itself lives in `logs`, one level up, since handler
packages and engine modules use it too. What's here is the CLI's own layer:
the typer options every endpoint repeats, and the glue that turns -v, -q,
and --no-color into a `configure_logging` call.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from redcap_alert_handler.logs import (
    LOGGER_NAME,
    configure_logging,
    get_logger,
    resolve_use_color,
)

# Shared by every endpoint that needs config or secrets (doctor now; auth and
# process later) so the flags, envvars, and help text can't drift apart.
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


def resolve_log_level(verbose: bool, quiet: bool) -> tuple[int, str | None]:
    """Turn -v/-q into a log level, plus a warning to emit if both were given."""
    if verbose and quiet:
        return logging.DEBUG, "both --verbose and --quiet given; using DEBUG"
    if verbose:
        return logging.DEBUG, None
    if quiet:
        return logging.ERROR, None
    return logging.INFO, None


def setup_logging(verbose: bool, quiet: bool, no_color: bool) -> None:
    """Resolve the logging flags and install the handler, warning on -v -q."""
    level, warning = resolve_log_level(verbose, quiet)
    use_color = resolve_use_color(no_color, sys.stderr)
    configure_logging(level, use_color)
    if warning:
        get_logger(LOGGER_NAME).warning(warning)
