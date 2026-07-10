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

import typer
from rich.pretty import pretty_repr

from redcap_alert_handler.cli.conventions import (
    ConfigOption,
    NoColorOption,
    QuietOption,
    SecretsOption,
    VerboseOption,
    get_logger,
    setup_logging,
)
from redcap_alert_handler.config import Config, ConfigError, load_config, load_secrets

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


def doctor(
    config: ConfigOption,
    secrets: SecretsOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Validate the config file and, if given, the secrets file.

    Exits 0 if every check that ran passed, 1 if any failed.
    """
    # The root callback already configured logging; only reconfigure when one
    # of doctor's own flags was set, so the flag closest to the command wins.
    if verbose or quiet or no_color:
        setup_logging(verbose, quiet, no_color)

    config_result, loaded_config = _check_config(config)
    _report(config_result)

    secrets_result = _check_secrets(secrets)
    _report(secrets_result)

    if loaded_config is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("loaded config:\n%s", pretty_repr(loaded_config))

    if any(result.passed is False for result in (config_result, secrets_result)):
        raise typer.Exit(1)


def _check_config(path: Path) -> tuple[CheckResult, Config | None]:
    try:
        config = load_config(path)
    except ConfigError as e:
        return CheckResult(name="config", passed=False, messages=tuple(e.problems)), None
    return CheckResult(name="config", passed=True), config


def _check_secrets(path: Path | None) -> CheckResult:
    if path is None:
        return CheckResult(
            name="secrets",
            passed=None,
            messages=("secrets not checked: no --secrets or $RAH_SECRETS given",),
        )
    try:
        secrets = load_secrets(path)
    except ConfigError as e:
        return CheckResult(name="secrets", passed=False, messages=tuple(e.problems))

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
            )
        if days_left <= _SECRET_EXPIRY_WARNING_WINDOW.days:
            when = "today" if days_left == 0 else f"in {days_left} days ({expires.isoformat()})"
            return CheckResult(
                name="secrets",
                passed=True,
                warnings=(f"client secret expires {when}; make a new one soon",),
            )
    return CheckResult(name="secrets", passed=True)


def _report(result: CheckResult) -> None:
    if result.passed is True:
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
