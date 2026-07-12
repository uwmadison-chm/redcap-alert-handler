# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""`rah doctor` and `rah init`: local diagnostics and mailbox provisioning.

An ordered list of checks -- config, routes, handlers, secrets, token cache,
graph, folders, categories -- each returning pass/fail/skipped plus messages.
`doctor` reports; `init` (and `doctor --fix`) also creates the mailbox folders
and categories the checks look for. Both run the same core so a check can
never mean one thing to one command and something else to the other.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

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
from redcap_alert_handler.config import (
    Config,
    ConfigError,
    GlobalConfig,
    Secrets,
    load_config,
    load_secrets,
)
from redcap_alert_handler.graph import GraphClient, GraphError
from redcap_alert_handler.handlers.loader import HandlerResolutionError, load_handlers
from redcap_alert_handler.mailbox import missing_categories, resolve_layout, seed_categories

logger = get_logger(__name__)

# How close a self-reported client-secret expiry gets before doctor starts
# warning about it.
_SECRET_EXPIRY_WARNING_WINDOW = timedelta(days=30)

FixOption = Annotated[
    bool,
    typer.Option("--fix", help="Create missing mailbox folders and categories."),
]

JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Write a machine-readable report to stdout."),
]

OutputOption = Annotated[
    Path | None,
    typer.Option("--output", "-o", help="Write the report to a file instead of the terminal."),
]


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
    fix: FixOption = False,
    json_output: JsonOption = False,
    output: OutputOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Check the config, the secrets if given, the token cache, and the mailbox.

    Runs the full check list and reports each result. Graph-side checks only
    run when config, secrets, and a fresh token are all in hand; otherwise
    they're skipped rather than failed. With --fix, missing mailbox folders
    and categories are created. Exits 0 if every check that ran passed, 1 if
    any failed.
    """
    _run_doctor(config, secrets, fix, json_output, output, verbose, quiet, no_color)


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

    The documented first-run step after `rah auth`. This is `doctor --fix`
    under a friendlier name -- it runs the same checks and creates whatever's
    missing.
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

    config_result, routes_result, loaded_config = _check_config_and_routes(config)
    handlers_result = _check_handlers(loaded_config)
    secrets_result, loaded_secrets = _check_secrets(secrets)
    cache_result, token_result = _check_token_cache(loaded_config, loaded_secrets)
    graph_result, folders_result, categories_result = _run_graph_checks(
        loaded_config, loaded_secrets, token_result, fix
    )

    results = (
        config_result,
        routes_result,
        handlers_result,
        secrets_result,
        cache_result,
        graph_result,
        folders_result,
        categories_result,
    )
    for result in results:
        _report(result)

    if loaded_config is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("loaded config:\n%s", pretty_repr(loaded_config))

    _emit_report(results, json_output, output)

    if any(result.passed is False for result in results):
        raise typer.Exit(1)


# -- config and routes ----------------------------------------------------


def _check_config_and_routes(path: Path) -> tuple[CheckResult, CheckResult, Config | None]:
    try:
        config = load_config(path)
    except ConfigError as e:
        # One ConfigError carries every problem; the route ones belong to the
        # routes check, the rest to config. Config passes if the only thing
        # that broke was routes -- [global] was fine, it just couldn't finish.
        route_problems = tuple(p for p in e.problems if p.startswith("routes"))
        other_problems = tuple(p for p in e.problems if not p.startswith("routes"))
        if other_problems:
            config_result = CheckResult(name="config", passed=False, messages=other_problems)
        else:
            config_result = CheckResult(name="config", passed=True)
        if route_problems:
            routes_result = CheckResult(name="routes", passed=False, messages=route_problems)
        else:
            routes_result = CheckResult(
                name="routes",
                passed=None,
                messages=("routes not checked: the config didn't load",),
            )
        return config_result, routes_result, None

    slugs = list(config.routes)
    noun = "route" if len(slugs) == 1 else "routes"
    routes_result = CheckResult(
        name="routes",
        passed=True,
        detail=f"{len(slugs)} {noun}: {', '.join(slugs)}",
    )
    return CheckResult(name="config", passed=True), routes_result, config


# -- handlers -------------------------------------------------------------


def _check_handlers(config: Config | None) -> CheckResult:
    name = "handlers"
    if config is None:
        return CheckResult(
            name=name,
            passed=None,
            messages=("handlers not checked: the config didn't load",),
        )

    try:
        load_handlers(config)
    except HandlerResolutionError as e:
        return CheckResult(name=name, passed=False, messages=tuple(e.problems))

    # "resolved" counts unique handler references, not routes -- two routes
    # sharing one handler still report as one handler resolved.
    count = len({route.handler for route in config.routes.values()})
    noun = "handler" if count == 1 else "handlers"
    return CheckResult(name=name, passed=True, detail=f"{count} {noun} resolved")


# -- secrets --------------------------------------------------------------


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


# -- token cache ----------------------------------------------------------


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


# -- graph, folders, categories -------------------------------------------


def _run_graph_checks(
    config: Config | None, secrets: Secrets | None, token_result: dict | None, fix: bool
) -> tuple[CheckResult, CheckResult, CheckResult]:
    # These three share one GraphClient session. graph resolves the base
    # folder; folders and categories work from it, so they skip whenever graph
    # didn't pass. A GraphError mid-session fails the check in progress and
    # leaves the later ones as "not checked".
    skip_reason = _graph_prerequisite(config, secrets, token_result)
    if skip_reason is not None:
        return (
            _graph_skip(skip_reason),
            _skip("folders", "the graph check didn't run"),
            _skip("categories", "the graph check didn't run"),
        )

    assert config is not None and token_result is not None
    global_config = config.global_config
    access_token = token_result["access_token"]
    slugs = list(config.routes)

    graph_result: CheckResult | None = None
    folders_result: CheckResult | None = None
    categories_result: CheckResult | None = None
    try:
        # GraphClient is looked up as a module global on purpose: the tests
        # monkeypatch redcap_alert_handler.cli.doctor.GraphClient.
        with GraphClient(global_config.mailbox, get_token=lambda: access_token) as client:
            graph_result, base_id = _check_graph_folder(client, global_config, fix)
            if graph_result.passed is not True or base_id is None:
                folders_result = _skip("folders", "the graph check didn't pass")
                categories_result = _skip("categories", "the graph check didn't pass")
            else:
                folders_result = _check_folders(client, base_id, slugs, fix)
                categories_result = _check_categories(client, fix)
    except GraphError as e:
        advice = _graph_advice(e)
        # Whichever result is still unset is the one that was mid-flight.
        if graph_result is None:
            graph_result = CheckResult(name="graph", passed=False, messages=(advice,))
            folders_result = _skip("folders", "the graph check didn't pass")
            categories_result = _skip("categories", "the graph check didn't pass")
        elif folders_result is None:
            folders_result = CheckResult(name="folders", passed=False, messages=(advice,))
            categories_result = _skip("categories", "the folders check didn't finish")
        else:
            categories_result = CheckResult(name="categories", passed=False, messages=(advice,))

    return graph_result, folders_result, categories_result


def _graph_prerequisite(
    config: Config | None, secrets: Secrets | None, token_result: dict | None
) -> str | None:
    if config is None:
        return "the config didn't load"
    if secrets is None:
        return "no secrets to authenticate with"
    if token_result is None:
        return "the token cache didn't refresh"
    if not token_result.get("access_token"):
        return "the refreshed token had no access token"
    return None


def _check_graph_folder(
    client: GraphClient, global_config: GlobalConfig, fix: bool
) -> tuple[CheckResult, str | None]:
    base_folder = global_config.base_folder
    mailbox = global_config.mailbox
    folder = _resolve_base_folder(client, base_folder)
    if folder is None:
        # "inbox" is well-known and always exists, so a missing base folder is
        # always a named one we can create under the mailbox root.
        if fix and base_folder != "inbox":
            root = client.get_well_known_folder("msgFolderRoot")
            folder = client.create_child_folder(root["id"], base_folder)
            count = folder.get("totalItemCount", 0)
            detail = (
                f"created {base_folder}; {_message_phrase(count)} in {base_folder} for {mailbox}"
            )
            return CheckResult(name="graph", passed=True, detail=detail), folder["id"]
        return CheckResult(
            name="graph",
            passed=False,
            messages=(
                f"no folder named {base_folder!r} under the mailbox root; "
                "run rah init to create it, or point base_folder at one that exists",
            ),
        ), None

    count = folder.get("totalItemCount", 0)
    return CheckResult(
        name="graph",
        passed=True,
        detail=f"{_message_phrase(count)} in {base_folder} for {mailbox}",
    ), folder["id"]


def _check_folders(client: GraphClient, base_id: str, slugs: list[str], fix: bool) -> CheckResult:
    report = resolve_layout(client, base_id, slugs, create=fix)
    if report.created:
        return CheckResult(
            name="folders", passed=True, detail=f"created {', '.join(report.created)}"
        )
    if report.missing:
        return CheckResult(
            name="folders",
            passed=False,
            messages=(f"missing {', '.join(report.missing)}; run rah init to create them",),
        )
    return CheckResult(name="folders", passed=True)


def _check_categories(client: GraphClient, fix: bool) -> CheckResult:
    if fix:
        created = seed_categories(client)
        if created:
            return CheckResult(
                name="categories", passed=True, detail=f"created {', '.join(created)}"
            )
        return CheckResult(name="categories", passed=True)
    missing = missing_categories(client)
    if missing:
        return CheckResult(
            name="categories",
            passed=False,
            messages=(f"missing {', '.join(missing)}; run rah init to seed them",),
        )
    return CheckResult(name="categories", passed=True)


def _resolve_base_folder(client: GraphClient, base_folder: str) -> dict | None:
    # "inbox" means the real Inbox (a well-known folder); anything else is a
    # named child of the mailbox root, matching how config validates it.
    if base_folder == "inbox":
        return client.get_well_known_folder("inbox")
    root = client.get_well_known_folder("msgFolderRoot")
    return client.find_child_folder(root["id"], base_folder)


def _graph_skip(reason: str) -> CheckResult:
    return CheckResult(name="graph", passed=None, messages=(f"graph not checked: {reason}",))


def _skip(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, passed=None, messages=(f"{name} not checked: {reason}",))


def _graph_advice(error: GraphError) -> str:
    if error.status is None:
        return f"couldn't reach the Graph API: {error}"
    return f"Graph rejected the request (HTTP {error.status}): {error}"


def _message_phrase(count: int) -> str:
    noun = "message" if count == 1 else "messages"
    return f"{count} {noun}"


def _rough_timespan(result: dict) -> str:
    """The token result's expires_in as words, e.g. "59 minutes".

    msal reports the cached token's *remaining* life, so ragged values like
    3541 are the norm; stray seconds get trimmed rather than spelled out.
    """
    seconds = int(result.get("expires_in", 0))
    if seconds >= 60:
        seconds -= seconds % 60
    return humanfriendly.format_timespan(seconds)


# -- reporting ------------------------------------------------------------


def _render_check(result: CheckResult) -> list[tuple[int, str]]:
    """One check as (log level, line) pairs, shared by stderr and file output.

    Both the live report and the -o text file run through here so their
    wording can't drift apart.
    """
    lines: list[tuple[int, str]] = []
    if result.passed is True:
        if result.detail:
            lines.append((logging.INFO, f"✅ {result.name} okay: {result.detail}"))
        else:
            lines.append((logging.INFO, f"✅ {result.name} okay"))
        for warning in result.warnings:
            lines.append((logging.WARNING, f"⚠️ {result.name}: {warning}"))
    elif result.passed is False:
        for message in result.messages:
            lines.append((logging.ERROR, f"❌ {result.name}: {message}"))
    else:
        for message in result.messages:
            lines.append((logging.INFO, message))
    return lines


def _report(result: CheckResult) -> None:
    for level, text in _render_check(result):
        logger.log(level, text)


def _emit_report(results: tuple[CheckResult, ...], json_output: bool, output: Path | None) -> None:
    # Human stderr logging already happened; this is the extra machine or file
    # copy. --json to stdout (or the file); -o without --json writes the plain
    # check lines to the file.
    if json_output:
        payload = json.dumps(_json_report(results))
        if output is None:
            typer.echo(payload)
            return
        _write_file(output, payload)
        return
    if output is not None:
        text = "\n".join(text for result in results for _, text in _render_check(result))
        _write_file(output, text)


def _json_report(results: tuple[CheckResult, ...]) -> dict:
    status = {True: "passed", False: "failed", None: "skipped"}
    return {
        "ok": not any(result.passed is False for result in results),
        "checks": [
            {
                "name": result.name,
                "status": status[result.passed],
                "detail": result.detail,
                "messages": list(result.messages),
                "warnings": list(result.warnings),
            }
            for result in results
        ],
    }


def _write_file(output: Path, text: str) -> None:
    try:
        output.write_text(text + "\n")
    except OSError as e:
        logger.error("💥 couldn't write the report to %s: %s", output, e.strerror)
        raise typer.Exit(1) from e
