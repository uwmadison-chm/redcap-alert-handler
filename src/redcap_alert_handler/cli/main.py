"""The rah command line entry point."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as pkg_version
from typing import Annotated

import typer

from redcap_alert_handler.cli.conventions import (
    configure_logging,
    get_logger,
    resolve_log_level,
    resolve_use_color,
)

app = typer.Typer(name="rah")
logger = get_logger(__name__)


def _print_version(value: bool) -> None:
    if not value:
        return
    typer.echo(f"rah {pkg_version('redcap-alert-handler')}")
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Set log level to DEBUG.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Set log level to ERROR.")] = False,
    no_color: Annotated[
        bool, typer.Option("--no-color", help="Turn off color in logging.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the rah version and exit.",
            is_eager=True,
            callback=_print_version,
        ),
    ] = False,
) -> None:
    """Process REDCap emails from an O365 mailbox."""
    level, warning = resolve_log_level(verbose, quiet)
    use_color = resolve_use_color(no_color, sys.stderr)
    configure_logging(level, use_color)
    if warning:
        logger.warning(warning)

    # No subcommands exist yet (step 0). Typer's no_args_is_help exits 2 on a
    # bare invocation, which reads as a usage error rather than plain help,
    # so we print help and exit clean ourselves.
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        logger.debug("interrupted")
        raise SystemExit(130) from None
    except BrokenPipeError:
        # Python flushes stdout at exit; redirect it to devnull first so that
        # flush doesn't raise a second BrokenPipeError on the way out.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        raise SystemExit(1) from None
