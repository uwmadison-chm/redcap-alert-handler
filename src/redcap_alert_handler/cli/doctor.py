# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""`rah doctor`: local diagnostics, standing in for a /health endpoint.

A small ordered list of checks, each returning pass/fail/skipped plus
messages, so later steps (auth status, Graph reachability, folder layout)
can append checks without reshaping this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import humanfriendly
import typer
from rich.pretty import pretty_repr

from redcap_alert_handler import auth
from redcap_alert_handler.cli.conventions import (
    ConfigOption,
    NoColorOption,
    QuietOption,
    SecretsOption,
    VerboseOption,
    get_logger,
    setup_logging,
)
from redcap_alert_handler.config import Config, ConfigError, Secrets, load_config, load_secrets
from redcap_alert_handler.graph import GraphClient, GraphError

logger = get_logger(__name__)

# How close a self-reported client-secret expiry gets before doctor starts
# warning about it.
_SECRET_EXPIRY_WARNING_WINDOW = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one doctor check.

    passed is None for a check that didn't run at all (e.g. secrets when no
    secrets path was given) -- distinct from failure, and doesn't affect the
    exit code.
    """

    name: str
    passed: bool | None
    messages: tuple[str, ...] = field(default_factory=tuple)
    # Heads-up conditions on a check that still passed
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # A short qualifier appended to a passing check's okay line
    detail: str | None = None


def doctor(
    config: ConfigOption,
    secrets: SecretsOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Check the config, the secrets if given, the token cache, and the mailbox.

    The Graph check only runs when config, secrets, and a fresh token are all
    in hand; otherwise it's skipped rather than failed. Exits 0 if every check
    that ran passed, 1 if any failed.
    """
    # The root callback already configured logging; only reconfigure when one
    # of doctor's own flags was set, so the flag closest to the command wins.
    if verbose or quiet or no_color:
        setup_logging(verbose, quiet, no_color)

    config_result, loaded_config = _check_config(config)
    _report(config_result)

    secrets_result, loaded_secrets = _check_secrets(secrets)
    _report(secrets_result)

    cache_result, token_result = _check_token_cache(loaded_config, loaded_secrets)
    _report(cache_result)

    graph_result = _check_graph(loaded_config, loaded_secrets, token_result)
    _report(graph_result)

    if loaded_config is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("loaded config:\n%s", pretty_repr(loaded_config))

    results = (config_result, secrets_result, cache_result, graph_result)
    if any(result.passed is False for result in results):
        raise typer.Exit(1)


def _check_config(path: Path) -> tuple[CheckResult, Config | None]:
    try:
        config = load_config(path)
    except ConfigError as e:
        return CheckResult(name="config", passed=False, messages=tuple(e.problems)), None
    return CheckResult(name="config", passed=True), config


def _check_secrets(path: Path | None) -> tuple[CheckResult, Secrets | None]:
    if path is None:
        return CheckResult(
            name="secrets",
            passed=None,
            messages=("secrets not checked: no --secrets or $RAH_SECRETS given",),
        ), None
    try:
        secrets = load_secrets(path)
    except ConfigError as e:
        return CheckResult(name="secrets", passed=False, messages=tuple(e.problems)), None

    # The expiry is self-reported (see the Secrets model), so this is a
    # health judgment, not validation: a past date fails, a near one warns.
    expires = secrets.client_secret_expires
    if expires is not None:
        # Azure expires secrets at a UTC instant, so judge against UTC's today
        days_left = (expires - datetime.now(UTC).date()).days
        if days_left < 0:
            return CheckResult(
                name="secrets",
                passed=False,
                messages=(
                    f"client secret expired on {expires.isoformat()}; make a new one in "
                    "the Azure portal and update the secrets file",
                ),
            ), None
        if days_left <= _SECRET_EXPIRY_WARNING_WINDOW.days:
            when = "today" if days_left == 0 else f"in {days_left} days ({expires.isoformat()})"
            return CheckResult(
                name="secrets",
                passed=True,
                warnings=(f"client secret expires {when}; make a new one soon",),
            ), secrets
    return CheckResult(name="secrets", passed=True), secrets


def _check_token_cache(
    config: Config | None, secrets: Secrets | None
) -> tuple[CheckResult, dict | None]:
    # Returns the refreshed msal result alongside the check so the Graph check
    # can reuse the same access token instead of refreshing a second time.
    name = "token cache"
    if config is None:
        return CheckResult(
            name=name,
            passed=None,
            messages=("token cache not checked: the config didn't load",),
        ), None

    path = config.global_config.token_cache_path
    if not path.exists():
        return CheckResult(
            name=name,
            passed=False,
            messages=(f"no token cache at {path}; run rah auth to sign in",),
        ), None

    cache = auth.load_token_cache(path)
    username = auth.cached_username(cache)
    if username is None:
        return CheckResult(
            name=name,
            passed=False,
            messages=(f"token cache at {path} has no signed-in account; run rah auth",),
        ), None

    if secrets is None:
        # Refreshing needs the client secret, so without usable secrets the
        # judgment stops at "someone has signed in".
        return CheckResult(
            name=name,
            passed=True,
            detail=f"signed in as {username}, but refresh not tried without secrets",
        ), None

    # Diagnosis only: the refreshed token is deliberately not written back.
    # doctor may run as a user who can read the cache but shouldn't own it.
    app = auth.build_app(secrets, cache)
    result = auth.refresh_silently(app)
    if result is None:
        return CheckResult(
            name=name,
            passed=False,
            messages=("cached token wouldn't refresh; run rah auth to sign in again",),
        ), None
    expiry = auth.token_expiry(result).isoformat(timespec="seconds")
    logger.debug("token cache: token valid for %s, good until %s", _rough_timespan(result), expiry)
    return CheckResult(
        name=name,
        passed=True,
        detail=f"authenticated as {auth.describe_account(app, result)}",
    ), result


def _check_graph(
    config: Config | None, secrets: Secrets | None, token_result: dict | None
) -> CheckResult:
    name = "graph"
    if config is None:
        return _graph_skip("the config didn't load")
    if secrets is None:
        return _graph_skip("no secrets to authenticate with")
    if token_result is None:
        return _graph_skip("the token cache didn't refresh")
    access_token = token_result.get("access_token")
    if not access_token:
        return _graph_skip("the refreshed token had no access token")

    global_config = config.global_config
    base_folder = global_config.base_folder
    try:
        with GraphClient(global_config.mailbox, get_token=lambda: access_token) as client:
            folder = _resolve_base_folder(client, base_folder)
            if folder is None:
                return CheckResult(
                    name=name,
                    passed=False,
                    messages=(
                        f"no folder named {base_folder!r} under the mailbox root; "
                        "create it, or point base_folder at one that exists",
                    ),
                )
            messages = client.list_messages(folder["id"])
    except GraphError as e:
        return CheckResult(name=name, passed=False, messages=(_graph_advice(e),))

    count = len(messages)
    noun = "message" if count == 1 else "messages"
    return CheckResult(
        name=name,
        passed=True,
        detail=f"{count} {noun} in {base_folder} for {global_config.mailbox}",
    )


def _resolve_base_folder(client: GraphClient, base_folder: str) -> dict | None:
    # "inbox" means the real Inbox (a well-known folder); anything else is a
    # named child of the mailbox root, matching how config validates it.
    if base_folder == "inbox":
        return client.get_well_known_folder("inbox")
    root = client.get_well_known_folder("msgFolderRoot")
    return client.find_child_folder(root["id"], base_folder)


def _graph_skip(reason: str) -> CheckResult:
    return CheckResult(name="graph", passed=None, messages=(f"graph not checked: {reason}",))


def _graph_advice(error: GraphError) -> str:
    if error.status is None:
        return f"couldn't reach the Graph API: {error}"
    return f"Graph rejected the request (HTTP {error.status}): {error}"


def _rough_timespan(result: dict) -> str:
    """The token result's expires_in as words, e.g. "59 minutes".

    msal reports the cached token's *remaining* life, so ragged values like
    3541 are the norm; stray seconds get trimmed rather than spelled out.
    """
    seconds = int(result.get("expires_in", 0))
    if seconds >= 60:
        seconds -= seconds % 60
    return humanfriendly.format_timespan(seconds)


def _report(result: CheckResult) -> None:
    if result.passed is True:
        if result.detail:
            logger.info("✅ %s okay: %s", result.name, result.detail)
        else:
            logger.info("✅ %s okay", result.name)
        for warning in result.warnings:
            logger.warning("⚠️ %s: %s", result.name, warning)
        return
    if result.passed is False:
        for message in result.messages:
            logger.error("❌ %s: %s", result.name, message)
        return
    for message in result.messages:
        logger.info(message)
