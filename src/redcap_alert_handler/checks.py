# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The check list: what `rah doctor` reports and `rah process` runs at startup.

An ordered set of checks -- config, routes, handlers, checkups, secrets, token
cache, graph, folders, categories -- each returning pass/fail/skipped plus
messages. `doctor` reports them; `init` (and `doctor --fix`) runs the same list
with `fix` on, creating the mailbox folders and categories the checks look for;
`process` runs it at startup so a broken config shouts before the first pass
instead of one message at a time. One list, so a check can never mean one thing
to one command and something else to another.

The commands own presentation and exit codes; what's here is the checking
itself, plus `render_check` and `json_report` so every command's wording comes
from the same place.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import humanfriendly

from redcap_alert_handler import auth
from redcap_alert_handler.config import (
    Config,
    ConfigError,
    GlobalConfig,
    Secrets,
    load_config,
    load_secrets,
)
from redcap_alert_handler.graph import GraphClient, GraphError
from redcap_alert_handler.handlers.contract import Handler, build_context
from redcap_alert_handler.handlers.loader import HandlerResolutionError, load_handlers
from redcap_alert_handler.logs import get_logger
from redcap_alert_handler.mailbox import (
    missing_categories,
    resolve_base_folder,
    resolve_layout,
    seed_categories,
)

logger = get_logger(__name__)

# How close a self-reported client-secret expiry gets before doctor starts
# warning about it.
_SECRET_EXPIRY_WARNING_WINDOW = timedelta(days=30)

# The checks `rah process` refuses to start without. Everything else it runs is
# advice: a mailbox that isn't provisioned, an unreachable Graph, or a route
# whose checkup is unhappy all get reported and then handled by the pass loop,
# which already knows how to wait out infrastructure trouble under --watch.
REQUIRED_TO_START = ("config", "routes", "handlers", "secrets")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one check.

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


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Every check's result, plus whatever the checks managed to load.

    The loaded values are here so a caller that runs the checks and then does
    real work -- `rah process` -- doesn't parse the config, secrets, and
    handlers a second time. Each is None when its check didn't pass.
    """

    results: tuple[CheckResult, ...]
    config: Config | None
    secrets: Secrets | None
    handlers: Mapping[str, Handler] | None

    @property
    def ok(self) -> bool:
        """True when nothing failed. A skipped check doesn't count against it."""
        return not any(result.passed is False for result in self.results)

    def failures(self) -> tuple[str, ...]:
        """The names of the checks that failed, in check order."""
        return tuple(result.name for result in self.results if result.passed is False)


def run_checks(config_path: Path, secrets_path: Path | None, fix: bool = False) -> CheckReport:
    """Run the whole check list in order and collect the results.

    Nothing is reported here -- the caller decides how to render the results
    and what to do about them. With `fix`, the mailbox checks create the
    folders and categories they'd otherwise report as missing.
    """
    config_result, routes_result, config = _check_config_and_routes(config_path)
    handlers_result, handlers = _check_handlers(config)
    checkups_result = _check_checkups(config, handlers)
    secrets_result, secrets = _check_secrets(secrets_path)
    cache_result, token_result = _check_token_cache(config, secrets)
    graph_result, folders_result, categories_result = _run_graph_checks(
        config, secrets, token_result, fix
    )

    return CheckReport(
        results=(
            config_result,
            routes_result,
            handlers_result,
            checkups_result,
            secrets_result,
            cache_result,
            graph_result,
            folders_result,
            categories_result,
        ),
        config=config,
        secrets=secrets,
        handlers=handlers,
    )


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


def _check_handlers(config: Config | None) -> tuple[CheckResult, dict[str, Handler] | None]:
    # Returns the resolved handlers alongside the result: the checkups check
    # needs the callables, and process needs them to run a pass.
    name = "handlers"
    if config is None:
        return CheckResult(
            name=name,
            passed=None,
            messages=("handlers not checked: the config didn't load",),
        ), None

    try:
        handlers = load_handlers(config)
    except HandlerResolutionError as e:
        return CheckResult(name=name, passed=False, messages=tuple(e.problems)), None

    # "resolved" counts unique handler references, not routes -- two routes
    # sharing one handler still report as one handler resolved.
    count = len({route.handler for route in config.routes.values()})
    noun = "handler" if count == 1 else "handlers"
    return CheckResult(name=name, passed=True, detail=f"{count} {noun} resolved"), handlers


# -- checkups -------------------------------------------------------------


def _check_checkups(config: Config | None, handlers: Mapping[str, Handler] | None) -> CheckResult:
    """Ask each route's handler whether its own config makes sense.

    The engine can't judge a handler's keys -- it doesn't know what
    `input_fields` or `model_file` mean -- so it asks. A handler may attach a
    `checkup(context) -> list[str]` to its callable; rah calls it with the same
    Context a message would arrive with and reports whatever comes back. An
    empty list means healthy.

    A handler without a checkup is not a problem, just a route rah can't say
    anything about. A checkup that raises, isn't callable, or returns something
    other than strings is reported against its route rather than taking the
    check list down with it.
    """
    name = "checkups"
    if config is None:
        return _skip(name, "the config didn't load")
    if handlers is None:
        return _skip(name, "the handlers didn't resolve")

    problems: list[str] = []
    checked: list[str] = []
    without: list[str] = []
    for slug, route in config.routes.items():
        checkup = getattr(handlers[slug], "checkup", None)
        if checkup is None:
            without.append(slug)
            continue
        if not callable(checkup):
            problems.append(f"routes.{slug}: the handler's checkup isn't callable")
            continue
        checked.append(slug)
        context = build_context(config.global_config, route)
        try:
            reported = checkup(context)
        except Exception as e:
            # A checkup is a handler package's code running inside doctor;
            # whatever it does, the rest of the checks still have to run.
            problems.append(f"routes.{slug}: checkup raised {type(e).__name__}: {e}")
            continue
        problems.extend(f"routes.{slug}: {problem}" for problem in _checkup_problems(reported))

    if problems:
        return CheckResult(name=name, passed=False, messages=tuple(problems))
    return CheckResult(name=name, passed=True, detail=_checkup_detail(checked, without))


def _checkup_problems(reported: object) -> list[str]:
    """Normalize a checkup's return value into a list of problem strings.

    The contract is a list of strings, but a handler that reports its one
    problem as a bare string means the obvious thing, so that's accepted too
    (`HandlerResolutionError` takes the same shortcut). Anything else is
    itself a problem worth reporting -- quietly ignoring it would mean quietly
    ignoring whatever the checkup was trying to say.
    """
    if reported is None:
        return []
    if isinstance(reported, str):
        return [reported] if reported.strip() else []
    if not isinstance(reported, (list, tuple)):
        return [f"checkup returned {type(reported).__name__}, expected a list of problems"]
    problems: list[str] = []
    for item in reported:
        if isinstance(item, str):
            if item.strip():
                problems.append(item)
        else:
            problems.append(f"checkup reported a {type(item).__name__}, expected a string")
    return problems


def _checkup_detail(checked: list[str], without: list[str]) -> str:
    if not checked:
        return "no route's handler offers one"
    noun = "route" if len(checked) == 1 else "routes"
    if not without:
        return f"{len(checked)} {noun} checked"
    total = len(checked) + len(without)
    return f"{len(checked)} of {total} routes checked; no checkup for {', '.join(without)}"


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
    try:
        # build_app is inside the try on purpose: msal does tenant discovery
        # at construction, so a network/DNS outage raises here, not at the
        # refresh. Report it as advice rather than spilling a requests
        # traceback out of doctor.
        app = auth.build_app(secrets, cache)
        result = auth.refresh_silently(app)
    except auth.AuthNetworkError as e:
        return CheckResult(name=name, passed=False, messages=(str(e),)), None
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
        # monkeypatch redcap_alert_handler.checks.GraphClient.
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
    folder = resolve_base_folder(client, base_folder)
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


def render_check(result: CheckResult) -> list[tuple[int, str]]:
    """One check as (log level, line) pairs, shared by every way of showing it.

    The live report, process's startup report, and doctor's -o text file all
    run through here so their wording can't drift apart.
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


def report(results: tuple[CheckResult, ...]) -> None:
    """Log every check's lines at their own level."""
    for result in results:
        for level, text in render_check(result):
            logger.log(level, text)


def json_report(results: tuple[CheckResult, ...]) -> dict:
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
