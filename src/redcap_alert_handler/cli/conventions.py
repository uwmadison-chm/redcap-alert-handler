"""Logging, color, and stream conventions shared by every CLI endpoint."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

from rich.console import Console

LOGGER_NAME = "redcap_alert_handler"

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
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        style = _LEVEL_STYLES.get(record.levelno)
        self._console.print(message, style=style, highlight=False, soft_wrap=True)


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
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
