# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""How rah logs: the package logger, the handler that writes it, and color.

Every layer logs -- the CLI, the engine modules, and handler packages -- so
the logging system lives above all of them. What stays in `cli.conventions`
is only the flag handling: turning -v, -q, and --no-color into a
`configure_logging` call.

`configure_logging` installs rah's handler on the `redcap_alert_handler`
logger and turns propagation off, so a logger outside that hierarchy never
reaches rah's output. `get_logger` keeps everyone inside it: a rah module
passing `__name__` gets its own logger back, and a handler package's name is
namespaced under `redcap_alert_handler.handlers.<name>`, sharing rah's
output and level. That's why `get_logger` is part of the handler contract,
re-exported at the package root next to `Message` and the error types.
"""

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
    """Return name's logger, namespaced into rah's hierarchy if it isn't already."""
    if name == LOGGER_NAME or name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.handlers.{name}")


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
