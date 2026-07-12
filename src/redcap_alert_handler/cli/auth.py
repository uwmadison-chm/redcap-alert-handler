# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""`rah auth`: interactive sign-in for the service account.

Single-purpose by design -- it gets a token into the cache and says who it
belongs to. Health questions (does the mailbox exist, do the folders look
right) belong to `rah doctor`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from redcap_alert_handler.auth import (
    AuthError,
    build_app,
    describe_account,
    load_token_cache,
    redeem_auth_response,
    refresh_silently,
    save_token_cache,
    start_auth_flow,
    token_expiry,
)
from redcap_alert_handler.cli.conventions import (
    ConfigOption,
    NoColorOption,
    QuietOption,
    RequiredSecretsOption,
    VerboseOption,
    setup_logging,
)
from redcap_alert_handler.config import Config, ConfigError, Secrets, load_config, load_secrets
from redcap_alert_handler.logs import get_logger

logger = get_logger(__name__)


def auth(
    config: ConfigOption,
    secrets: RequiredSecretsOption,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Sign in as the service account and save the token cache.

    Prints a sign-in URL to open in any browser (on any machine), then asks
    for the URL the browser lands on afterward. When the cached token still
    refreshes, there's nothing to do and no browser is needed.
    """
    # The root callback already configured logging; only reconfigure when one
    # of auth's own flags was set, so the flag closest to the command wins.
    if verbose or quiet or no_color:
        setup_logging(verbose, quiet, no_color)

    loaded_config, loaded_secrets = _load_or_exit(config, secrets)

    cache_path = loaded_config.global_config.token_cache_path
    cache = load_token_cache(cache_path)
    app = build_app(loaded_secrets, cache)

    result = refresh_silently(app)
    if result is not None:
        save_token_cache(cache, cache_path)
        logger.info(
            "✅ cached token still works; signed in as %s, access token good until %s",
            describe_account(app, result),
            token_expiry(result).isoformat(timespec="seconds"),
        )
        return

    flow = start_auth_flow(app)
    # Plain echo, not logging: the URL is the product here, and it has to
    # survive -q and piping to a pager.
    typer.echo("Open this URL in a browser and sign in as the service account:")
    typer.echo("")
    typer.echo(flow["auth_uri"])
    typer.echo("")
    typer.echo(
        "The browser will end up on a localhost page that fails to load. "
        "That's expected -- copy its full URL from the address bar."
    )
    redirect_url = typer.prompt("Redirect URL")

    try:
        result = redeem_auth_response(app, flow, redirect_url)
    except AuthError as e:
        if e.description:
            logger.error("💥 sign-in failed: %s: %s", e.error, e.description)
        else:
            logger.error("💥 sign-in failed: %s", e.error)
        raise typer.Exit(1) from e

    save_token_cache(cache, cache_path)
    logger.info(
        "✅ signed in as %s; access token good until %s",
        describe_account(app, result),
        token_expiry(result).isoformat(timespec="seconds"),
    )


def _load_or_exit(config_path: Path, secrets_path: Path) -> tuple[Config, Secrets]:
    """Load both files, reporting every problem before exiting on any."""
    problems_found = False
    loaded_config = None
    loaded_secrets = None
    try:
        loaded_config = load_config(config_path)
    except ConfigError as e:
        for problem in e.problems:
            logger.error("❌ config: %s", problem)
        problems_found = True
    try:
        loaded_secrets = load_secrets(secrets_path)
    except ConfigError as e:
        for problem in e.problems:
            logger.error("❌ secrets: %s", problem)
        problems_found = True
    if problems_found:
        raise typer.Exit(1)
    assert loaded_config is not None
    assert loaded_secrets is not None
    return loaded_config, loaded_secrets
