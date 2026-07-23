# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""What a handler is called with: the message, and the route it runs under.

Both values are plain and picklable on purpose -- no Graph types, no live
sessions -- so a future process-pool dispatcher could hand them to a worker
unchanged. Outcomes are the exceptions in the sibling `errors` module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from redcap_alert_handler.config import GlobalConfig, RouteConfig


@dataclass(frozen=True, slots=True)
class Message:
    """One alert email, stripped down to what a handler needs.

    No Graph types anywhere in the contract: this is a plain, picklable
    value so a future process-pool dispatcher can hand it to a worker
    without dragging a live Graph session along. `internet_message_id` is
    the RFC 5322 Message-ID header -- stable across moves, retries, and
    rah restarts, and the identifier handlers claim against for idempotency
    (see `TransientError`).
    """

    internet_message_id: str
    subject: str
    body_text: str | None
    body_html: str | None
    sender: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Context:
    """Everything about the route a handler is running under, minus the message.

    `config` is the merge of the route's own settings over the global
    ones -- see `build_context` for the exact rule -- plus `handler` and the
    resolved `max_age`. Its keys are opaque to the engine: a handler package
    documents what it reads from `config`, and must ignore any key it
    doesn't recognize, since the same file's `[global]` extras reach every
    route. Treat it as read-only; nothing enforces that beyond convention,
    since `MappingProxyType` doesn't survive a pickle round-trip and
    picklable handler arguments are part of the contract.

    `state_dir` is the route's own directory -- `state_base_dir / slug` --
    for a handler's claim store and any other state it needs to keep. The
    watcher creates it before dispatch; a handler can assume it exists.
    """

    slug: str
    config: Mapping[str, object]
    state_dir: Path


type Handler = Callable[[Message, Context], None]
"""A route's handler: called with the message and its route's context.

Outcomes are exceptions, not return values -- raise `TransientError` or
`PermanentError` to ask for a retry or send the message to the error folder,
raise anything else and the engine treats it as transient, or return
normally and the message is done. The return value itself is ignored.
"""


def build_context(global_config: GlobalConfig, route: RouteConfig) -> Context:
    """Build the Context a route's handler runs with.

    `config` merges `global_config.extra` and `route.extra`, with the route's
    keys winning on a clash -- an operator can set a default in `[global]`
    and override it for one route without repeating everything else. Engine
    keys (`mailbox`, `token_cache_path`, `handler_timeout`, and the rest of
    `GlobalConfig`'s own fields) never appear here; only the `extra` tables
    do, plus `handler`, the resolved `max_age`, and the resolved `dry_run`,
    which every handler can read regardless of what its route's operator
    wrote. A handler is expected to honor `dry_run` by doing everything except
    its outward side effects -- the engine skips the claim and writes nothing
    back for a dry-run message, so a handler that quietly made changes anyway
    would defeat the point.
    """
    config: dict[str, object] = {
        **dict(global_config.extra),
        **dict(route.extra),
        "handler": route.handler,
        "max_age": route.max_age,
        "dry_run": route.dry_run,
    }
    return Context(
        slug=route.slug,
        config=config,
        state_dir=global_config.state_base_dir / route.slug,
    )
