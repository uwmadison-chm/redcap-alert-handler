# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""`rah process`: read the mailbox, run handlers, write outcomes back.

The thing you actually run. A bare `rah process` makes one pass over the base
folder and exits, which is what makes it cron-able; `--watch` repeats the pass
on a timer until a signal stops it. Either way the real work belongs to
`processor.run_pass` (list, decide, dispatch, apply) and `dispatch` (the state
machine); what lives here is the outer shell -- loading config and secrets,
keeping a token warm, resolving the folder layout, sizing the worker pool, and
turning trouble into the right exit code. Config or handler problems stop the
command in both modes; Graph and auth trouble stop a single pass but only slow
a watching one.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Annotated

import humanfriendly
import typer

from redcap_alert_handler.auth import AuthError, TokenProvider
from redcap_alert_handler.cli.conventions import (
    ConfigOption,
    NoColorOption,
    QuietOption,
    RequiredSecretsOption,
    VerboseOption,
    setup_logging,
)
from redcap_alert_handler.config import Config, ConfigError, Secrets, load_config, load_secrets
from redcap_alert_handler.graph import GraphClient, GraphError
from redcap_alert_handler.handlers.contract import Handler
from redcap_alert_handler.handlers.loader import HandlerResolutionError, load_handlers
from redcap_alert_handler.logs import get_logger
from redcap_alert_handler.mailbox import resolve_base_folder, resolve_layout
from redcap_alert_handler.pool import HandlerPool
from redcap_alert_handler.processor import PassStats, run_pass

logger = get_logger(__name__)

# What signal.signal accepts and returns for a handler slot -- a callable, one
# of the SIG_* ints, or None. Named so saving and restoring stay type-clean.
SignalHandler = Callable[[int, FrameType | None], object] | int | None

WatchOption = Annotated[
    bool,
    typer.Option(
        "--watch", help="Keep polling the mailbox instead of making one pass and exiting."
    ),
]

PollIntervalOption = Annotated[
    str,
    typer.Option(
        "--poll-interval",
        help='How long to wait between passes in --watch mode, e.g. "5s" or "1m".',
    ),
]


def process(
    ctx: typer.Context,
    config: ConfigOption,
    secrets: RequiredSecretsOption,
    watch: WatchOption = False,
    poll_interval: PollIntervalOption = "5s",
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Process the mailbox once, or keep polling it with --watch.

    Loads the config, secrets, and handlers up front and stops if any of them
    won't load. A single pass exits 0 once it finishes -- even if every
    handler failed, since the mailbox records those outcomes -- and nonzero
    only on infrastructure trouble (auth, Graph, an unprovisioned mailbox).
    --watch runs the same pass on a timer and rides out that trouble instead,
    exiting 0 when a signal asks it to stop.
    """
    # The root callback already configured logging; only reconfigure when one
    # of process's own flags was set, so the flag closest to the command wins.
    if verbose or quiet or no_color:
        setup_logging(verbose, quiet, no_color)

    interval_seconds = _parse_poll_interval(poll_interval)
    source = ctx.get_parameter_source("poll_interval")
    if source is not None and source.name != "DEFAULT" and not watch:
        logger.warning("--poll-interval does nothing without --watch; making a single pass")

    loaded_config, loaded_secrets, handlers = _load_startup(config, secrets)
    provider = TokenProvider(loaded_secrets, loaded_config.global_config.token_cache_path)

    stop = threading.Event()
    previous = _install_signal_handlers(stop)
    try:
        if watch:
            _watch(loaded_config, handlers, provider, interval_seconds, stop)
        else:
            _one_shot(loaded_config, handlers, provider, stop)
    finally:
        _restore_signal_handlers(previous)


def _parse_poll_interval(value: str) -> float:
    """The poll interval in seconds, or a usage error if it won't parse."""
    try:
        seconds = humanfriendly.parse_timespan(value)
    except humanfriendly.InvalidTimespan as e:
        raise typer.BadParameter(
            f'{value!r} isn\'t a duration I understand; try something like "5s", "1m", "30s"'
        ) from e
    if seconds <= 0:
        raise typer.BadParameter(f"the poll interval has to be positive, not {value!r}")
    return seconds


def _load_startup(
    config_path: Path, secrets_path: Path
) -> tuple[Config, Secrets, dict[str, Handler]]:
    """Load config, secrets, and handlers, or report and exit.

    A config or handler that won't load is a human's job, not something to
    retry, so both modes stop here. Each problem gets its own line.
    """
    try:
        config = load_config(config_path)
    except ConfigError as e:
        for problem in e.problems:
            logger.error("💥 config: %s", problem)
        raise typer.Exit(1) from e

    try:
        secrets = load_secrets(secrets_path)
    except ConfigError as e:
        for problem in e.problems:
            logger.error("💥 secrets: %s", problem)
        raise typer.Exit(1) from e

    try:
        handlers = load_handlers(config)
    except HandlerResolutionError as e:
        for problem in e.problems:
            logger.error("💥 handler: %s", problem)
        raise typer.Exit(1) from e

    return config, secrets, handlers


def _one_shot(
    config: Config, handlers: Mapping[str, Handler], provider: TokenProvider, stop: threading.Event
) -> None:
    global_config = config.global_config
    try:
        # GraphClient is looked up as a module global on purpose: the tests
        # monkeypatch redcap_alert_handler.cli.process.GraphClient.
        with GraphClient(global_config.mailbox, get_token=provider.get_token) as client:
            layout = _resolve_layout(client, config)
            if layout is None:
                logger.error(
                    "💥 the mailbox isn't provisioned; run `rah init` to create its folders"
                )
                raise typer.Exit(1)
            base_id, folder_ids = layout
            _run_one_pass(client, config, handlers, base_id, folder_ids, stop)
    except AuthError as e:
        logger.error("💥 %s", e)
        raise typer.Exit(1) from e
    except GraphError as e:
        logger.error("💥 %s", e)
        raise typer.Exit(1) from e


def _watch(
    config: Config,
    handlers: Mapping[str, Handler],
    provider: TokenProvider,
    interval_seconds: float,
    stop: threading.Event,
) -> None:
    global_config = config.global_config
    logger.info(
        "watching %s, polling every %s",
        global_config.base_folder,
        humanfriendly.format_timespan(interval_seconds),
    )
    with GraphClient(global_config.mailbox, get_token=provider.get_token) as client:
        # Resolved once and reused; a GraphError drops it so a folder deleted
        # out from under us gets re-resolved on the next cycle.
        layout: tuple[str, Mapping[str, str]] | None = None
        while not stop.is_set():
            try:
                provider.refresh_if_stale()
                if layout is None:
                    layout = _resolve_layout(client, config)
                if layout is None:
                    logger.error(
                        "💥 the mailbox isn't provisioned; run `rah init` "
                        "and I'll pick it up next cycle"
                    )
                else:
                    base_id, folder_ids = layout
                    _run_one_pass(client, config, handlers, base_id, folder_ids, stop)
            except AuthError as e:
                # Recovery is a human re-running `rah auth`; no restart needed.
                logger.error("💥 %s", e)
            except GraphError as e:
                logger.error("💥 %s", e)
                layout = None
                if e.status == 401:
                    # Azure revoked the token mid-life; a near-expiry check
                    # would never notice, so force the next cycle to refresh.
                    provider.invalidate()
            _wait_for_next_cycle(stop, interval_seconds)
    logger.info("✅ shutting down cleanly")


def _resolve_layout(client: GraphClient, config: Config) -> tuple[str, Mapping[str, str]] | None:
    """The base folder id and the rah folder ids under it, or None if incomplete.

    None means either the base folder or one of its rah subfolders is missing
    -- an unprovisioned mailbox the operator has to fix with `rah init`.
    """
    global_config = config.global_config
    base = resolve_base_folder(client, global_config.base_folder)
    if base is None:
        return None
    report = resolve_layout(client, base["id"], list(config.routes), create=False)
    if report.missing:
        return None
    return base["id"], report.folder_ids


def _run_one_pass(
    client: GraphClient,
    config: Config,
    handlers: Mapping[str, Handler],
    base_id: str,
    folder_ids: Mapping[str, str],
    stop: threading.Event,
) -> None:
    pool = HandlerPool(config.global_config.max_workers)
    try:
        stats = run_pass(
            client,
            config,
            handlers,
            base_id,
            folder_ids,
            pool,
            now=lambda: datetime.now(UTC),
            stop=stop,
        )
    finally:
        pool.close()
    _log_summary(stats)


def _log_summary(stats: PassStats) -> None:
    # run_pass already logged each message; this is the one-line tally. An
    # empty folder has nothing worth an INFO line, so it drops to DEBUG.
    fields = (
        "dispatched",
        "completed",
        "transient_failures",
        "permanent_failures",
        "abandoned",
        "expired",
        "dead_lettered",
        "finished",
        "waiting",
        "skipped",
    )
    parts = [
        f"{name.replace('_', ' ')} {value}" for name in fields if (value := getattr(stats, name))
    ]
    if parts:
        logger.info("pass done: %s", ", ".join(parts))
    else:
        logger.debug("pass done: nothing to do")


def _wait_for_next_cycle(stop: threading.Event, seconds: float) -> None:
    """Wait out the poll interval, cut short the moment a shutdown is asked for.

    A plain stop.wait, but its own function so the watch tests can replace it
    to bound the loop without any real sleeping.
    """
    stop.wait(seconds)


def _install_signal_handlers(stop: threading.Event) -> dict[int, SignalHandler]:
    """Point SIGTERM/SIGINT at the stop event; a second one aborts hard."""

    def handle(signum: int, frame: FrameType | None) -> None:
        if stop.is_set():
            # Already shutting down and they signalled again -- give up on the
            # in-flight handlers. main.py turns this into exit 130.
            raise KeyboardInterrupt
        stop.set()

    previous: dict[int, SignalHandler] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, handle)
    return previous


def _restore_signal_handlers(previous: Mapping[int, SignalHandler]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)
