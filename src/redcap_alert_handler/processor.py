# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""One pass over the base folder: read mail, run the state machine, write back.

This is the I/O side of the dispatch core. `dispatch` decides what should
happen to a message and hands back plain Patch/Move instructions; this module
lists the folder, calls those pure functions, runs handlers on the worker
pool, and executes the instructions against a live GraphClient. Anything with
a side effect -- a Graph call, a handler, a state directory -- lives here, so
`dispatch` can stay testable with plain values.

There's no loop and no CLI here. `run_pass` walks the folder once and returns
counts; the CLI layer owns the polling, the auth, and the exit codes. A
GraphError is left to propagate: it's the infrastructure-trouble signal the
CLI turns into a nonzero exit for a one-shot run, or a logged retry for a
watching one.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event

from redcap_alert_handler import dispatch
from redcap_alert_handler.config import Config, GlobalConfig, RouteConfig
from redcap_alert_handler.graph import GraphClient
from redcap_alert_handler.handlers.contract import Handler, Message, build_context
from redcap_alert_handler.handlers.errors import PermanentError, TransientError
from redcap_alert_handler.logs import get_logger
from redcap_alert_handler.pool import HandlerPool, Job

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PassStats:
    """Counts from one pass, for the CLI to log.

    dispatched is the number of handlers this pass started; each of those
    resolves to exactly one of completed, transient_failures,
    permanent_failures, abandoned, or dry_run, so those five sum back to
    dispatched. dry_run collects every outcome from a dry-run route, since
    none of them is written back. The rest cover the decisions that never
    reach a handler.
    """

    dispatched: int = 0
    completed: int = 0
    transient_failures: int = 0
    permanent_failures: int = 0
    abandoned: int = 0
    dry_run: int = 0
    expired: int = 0
    dead_lettered: int = 0
    finished: int = 0
    waiting: int = 0
    skipped: int = 0


# What a real pass would have done, keyed by the outcome name _collect
# produces -- for the one log line a dry-run message gets, since nothing is
# written back to show for it.
_DRY_RUN_WOULD_HAVE = {
    "completed": "completed and filed the message",
    "permanent": "filed the message as a permanent error",
    "transient": "left the message to retry",
    "abandoned": "timed out and left the message to retry",
}


@dataclass(frozen=True, slots=True)
class _Dispatched:
    """A handler that's running (or queued), with what we need to finish it."""

    job: Job
    deadline: float  # time.monotonic() seconds; when the timeout runs out
    slug: str
    graph_id: str
    internet_message_id: str
    dry_run: bool


def build_message(raw: dict, fetch_text: Callable[[], str]) -> Message:
    """Turn a Graph message dict into the contract's Message.

    Graph stores one native body per message. When that body is HTML, the
    handler still wants a text rendering, so `fetch_text` -- a zero-arg call
    that returns the text body -- fills body_text; it's only called for an
    html-native message, never for one that's already text.
    """
    body = raw.get("body") or {}
    if body.get("contentType") == "html":
        body_html = body.get("content")
        body_text = fetch_text()
    else:
        body_text = body.get("content")
        body_html = None

    return Message(
        internet_message_id=raw["internetMessageId"],
        subject=raw["subject"],
        body_text=body_text,
        body_html=body_html,
        sender=_sender_address(raw),
        received_at=datetime.fromisoformat(raw["receivedDateTime"]),
    )


def run_pass(
    client: GraphClient,
    config: Config,
    handlers: Mapping[str, Handler],
    base_folder_id: str,
    folder_ids: Mapping[str, str],
    pool: HandlerPool,
    *,
    now: Callable[[], datetime],
    stop: Event,
) -> PassStats:
    """Process every message in the base folder once and return the counts.

    Lists the folder, runs each message through `dispatch.decide`, claims and
    dispatches the ones that should run, then collects the handlers and writes
    their outcomes back. When `stop` is set, no new message is claimed -- the
    remaining ones are counted as skipped -- but handlers already dispatched
    are still waited on and applied, so a graceful shutdown doesn't strand
    in-flight work.

    Raises:
        GraphError: any Graph call failed. Left to propagate as the
            infrastructure-trouble signal the CLI acts on.
    """
    global_config = config.global_config
    counts: Counter[str] = Counter()
    running: list[_Dispatched] = []

    messages = client.list_messages(base_folder_id, expand_properties=dispatch.MESSAGE_PROPERTY_IDS)

    for raw in messages:
        state = dispatch.parse_message_state(raw)
        imid = state.internet_message_id

        if stop.is_set():
            logger.debug("skipping %s; stop requested", imid)
            counts["skipped"] += 1
            continue

        slug = dispatch.match_slug(state.subject, config.routes)
        route = config.routes.get(slug) if slug is not None else None
        decision = dispatch.decide(state, route, now())
        graph_id = raw["id"]

        match decision:
            case dispatch.WaitForRetry(until=until):
                logger.debug("%s waiting until %s; %s", slug, until, imid)
                counts["waiting"] += 1
            case dispatch.Expire():
                _apply(client, folder_ids, graph_id, dispatch.expire())
                logger.info("❌ %s expired; %s", slug, imid)
                counts["expired"] += 1
            case dispatch.DeadLetter(reason=reason):
                _apply(client, folder_ids, graph_id, dispatch.dead_letter())
                # slug is None exactly when the reason is "unroutable", so the
                # two lines never lose information between them.
                if slug is None:
                    logger.info("❌ dead-lettered (unroutable); %s", imid)
                else:
                    logger.info("❌ %s dead-lettered (%s); %s", slug, reason, imid)
                counts["dead_lettered"] += 1
            case dispatch.Finish(terminal_state=terminal_state):
                assert slug is not None  # decide only finishes a routable message
                _apply(client, folder_ids, graph_id, dispatch.finish(terminal_state, slug))
                logger.info("%s finished (%s); %s", slug, terminal_state, imid)
                counts["finished"] += 1
            case dispatch.Dispatch():
                assert slug is not None and route is not None
                running.append(
                    _claim_and_dispatch(
                        client,
                        global_config,
                        handlers[slug],
                        route,
                        pool,
                        state,
                        raw,
                        graph_id,
                    )
                )
                counts["dispatched"] += 1

    _collect(client, folder_ids, global_config.retry_backoff, pool, running, now, counts)

    return PassStats(
        dispatched=counts["dispatched"],
        completed=counts["completed"],
        transient_failures=counts["transient_failures"],
        permanent_failures=counts["permanent_failures"],
        abandoned=counts["abandoned"],
        dry_run=counts["dry_run"],
        expired=counts["expired"],
        dead_lettered=counts["dead_lettered"],
        finished=counts["finished"],
        waiting=counts["waiting"],
        skipped=counts["skipped"],
    )


def _claim_and_dispatch(
    client: GraphClient,
    global_config: GlobalConfig,
    handler: Handler,
    route: RouteConfig,
    pool: HandlerPool,
    state: dispatch.MessageState,
    raw: dict,
    graph_id: str,
) -> _Dispatched:
    """Burn an attempt, build the handler's inputs, and hand it to the pool.

    The claim (decrement + processing marker) is written before the handler
    runs, so a wedged handler still spends its attempt -- that's the poison
    guard, and it's why an abandonment later applies the transient writer
    without touching retries-left again.

    A dry-run route skips the claim entirely: the engine writes nothing to a
    dry-run message, before or after, so there's no attempt to spend and the
    message is left exactly as it was found. The handler still runs, so it can
    log what it would have done; it's trusted to make no real changes.
    """
    slug = route.slug
    if not route.dry_run:
        _apply(client, {}, graph_id, dispatch.claim(state.retries_left, global_config.max_retries))

    state_dir = global_config.state_base_dir / slug
    state_dir.mkdir(parents=True, exist_ok=True)

    def fetch_text() -> str:
        return client.get_message(graph_id, body_format="text")["body"]["content"]

    message = build_message(raw, fetch_text)
    context = build_context(global_config, route)

    job = pool.submit(lambda: handler(message, context))
    deadline = time.monotonic() + global_config.handler_timeout.total_seconds()
    suffix = " (dry run)" if route.dry_run else ""
    logger.info("%s dispatched%s; %s", slug, suffix, state.internet_message_id)
    return _Dispatched(
        job=job,
        deadline=deadline,
        slug=slug,
        graph_id=graph_id,
        internet_message_id=state.internet_message_id,
        dry_run=route.dry_run,
    )


def _collect(
    client: GraphClient,
    folder_ids: Mapping[str, str],
    retry_backoff: timedelta,
    pool: HandlerPool,
    running: list[_Dispatched],
    now: Callable[[], datetime],
    counts: Counter[str],
) -> None:
    """Wait on each dispatched handler in submission order and apply its outcome."""
    for item in running:
        remaining = max(0.0, item.deadline - time.monotonic())
        try:
            item.job.future.result(timeout=remaining)
            outcome = "completed"
        except TimeoutError:
            # abandon() is False when the job finished just under the wire; the
            # real result is on the future, so read it instead of retrying.
            outcome = "abandoned" if pool.abandon(item.job) else _settled_outcome(item.job)
        except PermanentError:
            outcome = "permanent"
        except TransientError:
            outcome = "transient"
        except Exception:
            # Anything the handler didn't label is treated as transient; the
            # attempt counter, not the exception type, is what stops a loop.
            outcome = "transient"

        _apply_outcome(client, folder_ids, retry_backoff, now, item, outcome, counts)


def _settled_outcome(job: Job) -> str:
    """The outcome name for a future that's already settled (won't block)."""
    try:
        job.future.result(timeout=0)
        return "completed"
    except PermanentError:
        return "permanent"
    except TransientError:
        return "transient"
    except Exception:
        return "transient"


def _apply_outcome(
    client: GraphClient,
    folder_ids: Mapping[str, str],
    retry_backoff: timedelta,
    now: Callable[[], datetime],
    item: _Dispatched,
    outcome: str,
    counts: Counter[str],
) -> None:
    slug, gid, imid = item.slug, item.graph_id, item.internet_message_id
    if item.dry_run:
        # Dry-run routes are never claimed and never written back: the message
        # stays put and re-runs next pass. One line reports what a real pass
        # would have done, since nothing on the message shows for it.
        would_have = _DRY_RUN_WOULD_HAVE.get(outcome, outcome)
        logger.info("%s dry run, no changes written; would have %s; %s", slug, would_have, imid)
        counts["dry_run"] += 1
        return
    if outcome == "completed":
        _apply(client, folder_ids, gid, dispatch.completed(slug))
        logger.info("✅ %s completed; %s", slug, imid)
        counts["completed"] += 1
    elif outcome == "permanent":
        _apply(client, folder_ids, gid, dispatch.permanent_failure(slug))
        logger.info("❌ %s permanent failure; %s", slug, imid)
        counts["permanent_failures"] += 1
    elif outcome == "abandoned":
        _apply(client, folder_ids, gid, dispatch.transient_failure(now(), retry_backoff))
        logger.info("❌ %s abandoned after timeout, will retry; %s", slug, imid)
        counts["abandoned"] += 1
    else:  # transient
        _apply(client, folder_ids, gid, dispatch.transient_failure(now(), retry_backoff))
        logger.info("❌ %s transient failure, will retry; %s", slug, imid)
        counts["transient_failures"] += 1


def _apply(
    client: GraphClient,
    folder_ids: Mapping[str, str],
    graph_id: str,
    instructions: tuple[dispatch.Patch | dispatch.Move, ...],
) -> None:
    """Execute a writer's Patch/Move instructions in order against Graph.

    A Patch always precedes its Move, which is what keeps the authoritative
    property write ahead of the folder move. A move reissues the message id,
    but the Move is always last, so nothing here needs the new one.
    """
    current_id = graph_id
    for instruction in instructions:
        if isinstance(instruction, dispatch.Patch):
            client.patch_message(
                current_id,
                properties=instruction.properties,
                categories=instruction.categories,
            )
        else:  # dispatch.Move
            moved = client.move_message(current_id, folder_ids[instruction.path])
            current_id = moved["id"]


def _sender_address(raw: dict) -> str:
    """The sender's email address, from `from`, falling back to `sender`."""
    source = raw.get("from") or raw.get("sender") or {}
    return source.get("emailAddress", {}).get("address", "")
