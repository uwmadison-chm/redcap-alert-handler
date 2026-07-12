# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Turning a route's handler reference into a callable.

A route names its handler with a package-qualified reference,
`"package-name:handler_name"`: the distribution that registers the handler,
and the entry-point name it registers under, in the `rah.handlers` group.
The package half is matched to `EntryPoint.dist.name` after both sides are
canonicalized per PEP 503, so `redcap-alert-handler`, `redcap_alert_handler`,
and `Redcap.Alert.Handler` all mean the same package; the entry-point name is
matched exactly, since that's our own contract's identifier, not a
distribution name. A handler package registers its callables there, and rah
ships one of its own (`redcap-alert-handler:log_message`, in the sibling
`log_message` module) so the discovery machinery has something to find on a
fresh install.

`resolve_handler` loads one reference; `load_handlers` is the fail-fast
startup resolver that turns a loaded `Config` into a slug-keyed dict of
callables -- or refuses to start at all.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, distribution, entry_points
from typing import cast

from redcap_alert_handler.config import Config
from redcap_alert_handler.handlers.contract import Handler

HANDLER_GROUP = "rah.handlers"


class HandlerResolutionError(Exception):
    """One or more configured handler references couldn't be resolved.

    The message is written for the operator reading a doctor report: it
    names the package and handler and either points at what's missing (the
    package, or the name within it) or carries the import error that broke a
    registered one. `problems` holds one such message per broken reference,
    mirroring `config.ConfigError` -- `load_handlers` raises once with every
    failure it found, not one at a time. A bare string is accepted too and
    normalized to a one-element list, for callers (`resolve_handler`) that
    only ever have a single problem to report.
    """

    def __init__(self, problems: str | list[str]) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def _canonicalize(name: str) -> str:
    # PEP 503's distribution-name normalization: lowercase, and every run of
    # -, _, . collapses to a single -. Inlined rather than pulling in
    # `packaging` for one regex.
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve_handler(ref: str) -> object:
    """Load the handler a route's package-qualified reference points at.

    `ref` is `"package-name:handler_name"` -- see the module docstring for
    how the two halves are matched. Config validation normally guarantees
    this shape; the check here is defensive, for callers that skip it.

    Raises:
        HandlerResolutionError: `ref` isn't package-qualified, the named
            package isn't installed, the package is installed but doesn't
            register that name, or the entry point failed to import.
    """
    parts = ref.split(":")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise HandlerResolutionError(
            f"handler reference {ref!r} is not package-qualified; it should look "
            'like "package-name:handler_name"'
        )
    package, entry_name = (part.strip() for part in parts)
    target = _canonicalize(package)

    same_dist = [
        ep
        for ep in entry_points(group=HANDLER_GROUP)
        if ep.dist is not None and _canonicalize(ep.dist.name) == target
    ]

    for entry_point in same_dist:
        if entry_point.name == entry_name:
            try:
                return entry_point.load()
            except Exception as e:
                raise HandlerResolutionError(
                    f"handler {ref!r} ({HANDLER_GROUP}) failed to import: {e}"
                ) from e

    try:
        distribution(package)
    except PackageNotFoundError:
        raise HandlerResolutionError(
            f"package {package!r} isn't installed; is the handler package installed?"
        ) from None

    if same_dist:
        registered = ", ".join(sorted(ep.name for ep in same_dist))
        raise HandlerResolutionError(
            f"{package!r} doesn't register {entry_name!r} in {HANDLER_GROUP}; "
            f"it registers: {registered}"
        )
    raise HandlerResolutionError(f"{package!r} doesn't register any handlers in {HANDLER_GROUP}")


def load_handlers(config: Config) -> dict[str, Handler]:
    """Resolve every route's handler, fail-fast, with one error per problem.

    Routes that name the same handler reference resolve it once; every slug
    that named it ends up pointing at the very same callable, not separate
    copies. Resolution order follows the order routes first mention a
    reference, which only matters for the order problems are reported in.

    Raises:
        HandlerResolutionError: any reference failed to resolve.
            `problems` holds one message per broken reference, each naming
            every route slug that wanted it ("wanted by routes.a,
            routes.b"), so an operator sees every problem in the config at
            once instead of fixing them one `rah doctor` run at a time.
    """
    wanted_by: dict[str, list[str]] = {}
    for slug, route in config.routes.items():
        wanted_by.setdefault(route.handler, []).append(slug)

    resolved: dict[str, Handler] = {}
    problems: list[str] = []
    for ref, slugs in wanted_by.items():
        try:
            handler = resolve_handler(ref)
        except HandlerResolutionError as e:
            routes = ", ".join(f"routes.{slug}" for slug in slugs)
            problems.append(f"{e} (wanted by {routes})")
            continue
        for slug in slugs:
            # resolve_handler returns object -- it loads whatever an entry
            # point registers, without checking the signature matches Handler.
            # A route that gets past config validation and doctor's own
            # check has already committed to that shape.
            resolved[slug] = cast(Handler, handler)

    if problems:
        raise HandlerResolutionError(problems)

    return resolved
