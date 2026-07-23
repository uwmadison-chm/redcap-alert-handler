# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The config and secrets data structures, and the error the loaders raise.

These are plain frozen dataclasses -- no config-framework help. The parsing
that fills them (and collects every problem in one pass) lives in parsing.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


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
    handler_timeout: timedelta
    max_retries: int
    retry_backoff: timedelta
    max_age: timedelta
    max_workers: int
    dry_run: bool
    extra: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RouteConfig:
    """One `[routes.<slug>]` table, with max_age and dry_run resolved against the global default."""

    slug: str
    handler: str
    max_age: timedelta
    dry_run: bool
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
