# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Config and secrets models, loaded from TOML with no config-framework help.

Both loaders collect every problem in one pass instead of stopping at the
first one -- see ConfigError. Each problem string is `key.path: message`, so
callers (rah doctor, for now) can print them one per line as-is.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import cast

import humanfriendly

_SLUG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The keys the engine owns; everything else flows into `extra` untouched.
# Required-ness is enforced by the per-key parsers, not these sets.
_GLOBAL_KNOWN_KEYS = frozenset(
    {
        "mailbox",
        "base_folder",
        "token_cache_path",
        "state_base_dir",
        "polling_interval",
        "handler_timeout",
        "max_retries",
        "retry_backoff",
        "max_age",
    }
)
_ROUTE_KNOWN_KEYS = frozenset({"handler", "max_age"})
_SECRETS_REQUIRED_KEYS = frozenset({"tenant_id", "client_id", "client_secret"})
_SECRETS_KNOWN_KEYS = _SECRETS_REQUIRED_KEYS | {"client_secret_expires"}


class ConfigError(Exception):
    """A config or secrets file failed to load.

    Carries every problem found, not just the first -- validation is the
    project's first UX surface, and a one-fix-at-a-time loop is a bad one.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """The `[global]` section: operational policy shared by every route."""

    mailbox: str
    base_folder: str
    token_cache_path: Path
    state_base_dir: Path
    polling_interval: timedelta
    handler_timeout: timedelta
    max_retries: int
    retry_backoff: timedelta
    max_age: timedelta
    extra: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RouteConfig:
    """One `[routes.<slug>]` table, with max_age resolved against the global default."""

    slug: str
    handler: str
    max_age: timedelta
    extra: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Config:
    """A fully loaded, validated config file."""

    global_config: GlobalConfig
    routes: Mapping[str, RouteConfig]


@dataclass(frozen=True, slots=True)
class Secrets:
    """Credentials for the confidential-client auth flow (see rah auth)."""

    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    # Self-reported (copied from the portal when the secret is made); Azure
    # won't tell us without Application.Read.All. doctor warns as it nears.
    client_secret_expires: date | None = None


def load_config(path: Path) -> Config:
    """Parse and validate a main config TOML file.

    Raises:
        ConfigError: the file is unreadable, isn't valid TOML, or fails
            validation. `problems` on the exception lists everything wrong.
    """
    data = _read_toml(path, "global")

    problems: list[str] = []
    known_sections = {"global", "routes"}
    for key in sorted(set(data) - known_sections):
        problems.append(f"{key}: unknown section; expected [global] or [routes.<slug>]")

    global_config, global_problems = _parse_global(data.get("global"))
    problems.extend(global_problems)

    fallback_max_age = global_config.max_age if global_config is not None else None
    routes, route_problems = _parse_routes(data.get("routes"), fallback_max_age)
    problems.extend(route_problems)

    if problems:
        raise ConfigError(problems)

    assert global_config is not None  # no problems means global parsed clean
    return Config(global_config=global_config, routes=routes)


def load_secrets(path: Path) -> Secrets:
    """Parse and validate a secrets TOML file.

    Raises:
        ConfigError: the file is unreadable, isn't valid TOML, or fails
            validation. `problems` on the exception lists everything wrong.
    """
    data = _read_toml(path, "secrets")

    problems: list[str] = []
    values: dict[str, str] = {}
    # sorted: frozensets iterate in hash order, and problem output must be
    # stable from run to run.
    for key in sorted(_SECRETS_REQUIRED_KEYS):
        value = data.get(key)
        if value is None:
            problems.append(f'secrets.{key}: missing; add {key} = "..."')
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"secrets.{key}: must be a non-empty string")
        else:
            values[key] = value

    expires: date | None = None
    if "client_secret_expires" in data:
        value = data["client_secret_expires"]
        # datetime first: it's a subclass of date, so the order matters
        if isinstance(value, datetime):
            expires = value.date()
        elif isinstance(value, date):
            expires = value
        else:
            problems.append(
                "secrets.client_secret_expires: write it as a TOML date (no quotes), "
                "like client_secret_expires = 2028-07-09"
            )

    for key in sorted(set(data) - _SECRETS_KNOWN_KEYS):
        problems.append(f"secrets.{key}: unknown key; check for a typo")

    if problems:
        raise ConfigError(problems)

    return Secrets(
        tenant_id=values["tenant_id"],
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        client_secret_expires=expires,
    )


def _read_toml(path: Path, error_key: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigError([f"{error_key}: can't read {path}: {e.strerror}"]) from e
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ConfigError([f"{error_key}: {path} isn't UTF-8 text; re-save it as UTF-8"]) from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{error_key}: {path} isn't valid TOML: {e}"]) from e


def _parse_global(
    raw: object,
) -> tuple[GlobalConfig | None, list[str]]:
    if raw is None:
        return None, [
            "global: no [global] section; add one with token_cache_path, "
            "state_base_dir, and the timing settings"
        ]
    if not isinstance(raw, dict):
        return None, ["global: must be a table, written as [global]"]
    global_raw = cast(dict[str, object], raw)

    problems: list[str] = []

    mailbox = _parse_mailbox(global_raw, problems)
    base_folder = _parse_base_folder(global_raw, problems)
    token_cache_path = _parse_abs_path(global_raw, "token_cache_path", "global", problems)
    state_base_dir = _parse_abs_path(global_raw, "state_base_dir", "global", problems)
    max_retries = _parse_max_retries(global_raw, problems)

    durations: dict[str, timedelta | None] = {}
    for key in ("polling_interval", "handler_timeout", "retry_backoff", "max_age"):
        durations[key] = _parse_duration(global_raw, key, "global", problems)

    extra = {k: v for k, v in global_raw.items() if k not in _GLOBAL_KNOWN_KEYS}

    if problems:
        return None, problems

    assert mailbox is not None
    assert base_folder is not None
    assert token_cache_path is not None
    assert state_base_dir is not None
    assert max_retries is not None
    polling_interval = durations["polling_interval"]
    handler_timeout = durations["handler_timeout"]
    retry_backoff = durations["retry_backoff"]
    max_age = durations["max_age"]
    assert polling_interval is not None
    assert handler_timeout is not None
    assert retry_backoff is not None
    assert max_age is not None
    return (
        GlobalConfig(
            mailbox=mailbox,
            base_folder=base_folder,
            token_cache_path=token_cache_path,
            state_base_dir=state_base_dir,
            polling_interval=polling_interval,
            handler_timeout=handler_timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            max_age=max_age,
            extra=MappingProxyType(extra),
        ),
        [],
    )


def _parse_routes(
    raw: object, fallback_max_age: timedelta | None
) -> tuple[dict[str, RouteConfig], list[str]]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return {}, ["routes: no routes configured; add at least one [routes.<slug>] section"]
    if not isinstance(raw, dict):
        return {}, ["routes: must be a table of [routes.<slug>] entries"]
    routes_raw = cast(dict[str, object], raw)

    problems: list[str] = []
    routes: dict[str, RouteConfig] = {}

    for slug, entry in routes_raw.items():
        route, route_problems = _parse_route(slug, entry, fallback_max_age)
        problems.extend(route_problems)
        if route is not None:
            routes[slug] = route

    return routes, problems


def _parse_route(
    slug: str, entry: object, fallback_max_age: timedelta | None
) -> tuple[RouteConfig | None, list[str]]:
    problems: list[str] = []

    if not _SLUG_RE.match(slug):
        problems.append(
            f"routes.{slug}: slug must look like a Python identifier (letters, digits, "
            "underscore, not starting with a digit); slugs become mailbox folder names"
        )

    if not isinstance(entry, dict):
        problems.append(f"routes.{slug}: must be a table, written as [routes.{slug}]")
        return None, problems
    entry_raw = cast(dict[str, object], entry)

    handler = entry_raw.get("handler")
    if handler is None:
        problems.append(
            f'routes.{slug}: missing handler; set handler = "package-name:handler_name"'
        )
    elif not isinstance(handler, str):
        problems.append(f"routes.{slug}: handler must be a string")
    elif not _is_qualified_handler_ref(handler):
        problems.append(
            f'routes.{slug}: handler must look like "package-name:handler_name" '
            "(the installed package and its registered handler)"
        )

    max_age = fallback_max_age
    if "max_age" in entry_raw:
        max_age = _parse_duration(entry_raw, "max_age", f"routes.{slug}", problems)

    if problems:
        return None, problems

    if max_age is None:
        # Global's own max_age is broken (or [global] is missing outright)
        # and this route has no override. Global's problems already explain
        # why, so this route quietly contributes nothing rather than piling
        # on a second, confusing error.
        return None, []

    assert isinstance(handler, str)
    extra = {k: v for k, v in entry_raw.items() if k not in _ROUTE_KNOWN_KEYS}
    return (
        RouteConfig(slug=slug, handler=handler, max_age=max_age, extra=MappingProxyType(extra)),
        [],
    )


def _is_qualified_handler_ref(value: str) -> bool:
    # Shape only -- exactly one colon, both halves non-empty once stripped.
    # Resolving the halves against installed packages is the loader's job.
    package, colon, name = value.partition(":")
    return bool(colon) and value.count(":") == 1 and bool(package.strip()) and bool(name.strip())


def _parse_mailbox(raw: dict[str, object], problems: list[str]) -> str | None:
    # Graph takes either form in /users/{...}, so a UPN or a GUID both work;
    # the UPN is the readable choice.
    if "mailbox" not in raw:
        problems.append(
            "global.mailbox: missing; set the mailbox address (UPN), "
            'like mailbox = "svc-rah@example.edu"'
        )
        return None
    value = raw["mailbox"]
    if not isinstance(value, str) or not value.strip():
        problems.append("global.mailbox: must be a non-empty string, the mailbox address (UPN)")
        return None
    return value


def _parse_base_folder(raw: dict[str, object], problems: list[str]) -> str | None:
    # The folder the watcher polls. "inbox" (the default) means the real
    # Inbox; anything else is a folder at the mailbox root, which lets a dev
    # setup point rah at a folder in a personal account instead of a
    # dedicated mailbox.
    if "base_folder" not in raw:
        return "inbox"
    value = raw["base_folder"]
    if not isinstance(value, str) or not value.strip():
        problems.append("global.base_folder: must be a non-empty folder name")
        return None
    if "/" in value:
        problems.append(
            "global.base_folder: must be a single folder name at the mailbox root, "
            "not a path with /"
        )
        return None
    return value


def _parse_abs_path(
    raw: dict[str, object], key: str, section: str, problems: list[str]
) -> Path | None:
    if key not in raw:
        problems.append(f"{section}.{key}: missing; set an absolute path")
        return None
    value = raw[key]
    if not isinstance(value, str):
        problems.append(f"{section}.{key}: must be a string path")
        return None
    path = Path(value)
    if not path.is_absolute():
        problems.append(f"{section}.{key}: must be an absolute path")
        return None
    return path


def _parse_max_retries(raw: dict[str, object], problems: list[str]) -> int | None:
    key = "max_retries"
    if key not in raw:
        problems.append(f"global.{key}: missing; set a whole number of retries")
        return None
    value = raw[key]
    # bool is a subclass of int in Python, so TOML's true/false would
    # otherwise sneak through as 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"global.{key}: must be a whole number, not {value!r}")
        return None
    if value < 0:
        problems.append(f"global.{key}: must be zero or greater")
        return None
    return value


def _parse_duration(
    raw: dict[str, object], key: str, section: str, problems: list[str]
) -> timedelta | None:
    key_path = f"{section}.{key}"
    if key not in raw:
        problems.append(f'{key_path}: missing; set a duration like "5s" or a number of seconds')
        return None
    value = raw[key]

    if isinstance(value, bool):
        problems.append(f"{key_path}: must be a duration, not a boolean")
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = humanfriendly.parse_timespan(value)
        except humanfriendly.InvalidTimespan:
            problems.append(
                f'{key_path}: not a valid duration; try something like "5s", "3h", "1d"'
            )
            return None
    else:
        problems.append(f"{key_path}: must be a duration string or a number of seconds")
        return None

    if seconds <= 0:
        problems.append(f"{key_path}: duration must be positive")
        return None
    return timedelta(seconds=seconds)
