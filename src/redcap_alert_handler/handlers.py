# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Handler resolution and the built-in log_message stub.

A route names its handler with a package-qualified reference,
`"package-name:handler_name"`: the distribution that registers the handler,
and the entry-point name it registers under, in the `rah.handlers` group.
The package half is matched to `EntryPoint.dist.name` after both sides are
canonicalized per PEP 503, so `redcap-alert-handler`, `redcap_alert_handler`,
and `Redcap.Alert.Handler` all mean the same package; the entry-point name is
matched exactly, since that's our own contract's identifier, not a
distribution name. A handler package registers its callables there, and rah
ships one of its own (`redcap-alert-handler:log_message`) so the discovery
machinery has something to find on a fresh install.

Step 5 defines the real handler contract (Message, Context, the error
hierarchy) and grows this loader into the fail-fast startup resolver. For now
it's just enough for doctor to tell an operator whether their configured
handlers can be found.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, distribution, entry_points

from redcap_alert_handler.cli.conventions import get_logger

logger = get_logger(__name__)

HANDLER_GROUP = "rah.handlers"


class HandlerResolutionError(Exception):
    """A configured handler reference couldn't be resolved to a callable.

    The message is written for the operator reading a doctor report: it names
    the package and handler and either points at what's missing (the package,
    or the name within it) or carries the import error that broke a
    registered one.
    """


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


def log_message(*args: object, **kwargs: object) -> None:
    """Built-in stub handler: log that it ran and do nothing else.

    The signature is loose on purpose -- step 5 pins the real Message/Context
    contract and rewrites this body against it. Nothing message-derived beyond
    safe identifiers goes to the log, per the project's no-subjects-in-logs
    rule, so a one-line "invoked" is all it says.
    """
    logger.info("log_message handler invoked")
