# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Integration tests for the pass coordinator, against the fake Graph.

Every test wires a real GraphClient to a FakeGraph and a real HandlerPool, so
the only stand-ins are the clock and the handlers themselves. The mailbox tree
is built through mailbox.resolve_layout -- the same path rah init uses -- so
the folder ids fed to run_pass are the ones the real code would resolve.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from redcap_alert_handler import dispatch, mailbox
from redcap_alert_handler.config import Config, GlobalConfig, RouteConfig
from redcap_alert_handler.handlers.errors import PermanentError, TransientError
from redcap_alert_handler.pool import HandlerPool
from redcap_alert_handler.processor import build_message, run_pass

# -- fixtures and helpers --------------------------------------------------


class Clock:
    """A hand-cranked clock: run_pass reads it, tests move it."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def pool():
    p = HandlerPool(max_workers=2)
    try:
        yield p
    finally:
        p.close()


def make_config(
    state_base_dir: Path,
    *,
    max_retries: int = 3,
    handler_timeout: timedelta = timedelta(seconds=30),
    retry_backoff: timedelta = timedelta(hours=1),
    max_age: timedelta = timedelta(days=30),
    slug: str = "consent",
    dry_run: bool = False,
) -> Config:
    """A Config with one route, timings the test can pin down."""
    global_config = GlobalConfig(
        mailbox="svc-rah@example.edu",
        base_folder="inbox",
        token_cache_path=Path("/var/lib/rah/token-cache.json"),
        state_base_dir=state_base_dir,
        handler_timeout=handler_timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        max_age=max_age,
        max_workers=2,
        dry_run=dry_run,
        extra=MappingProxyType({}),
    )
    route = RouteConfig(
        slug=slug,
        handler="test-handlers:consent",
        max_age=max_age,
        dry_run=dry_run,
        extra=MappingProxyType({}),
    )
    return Config(global_config=global_config, routes={slug: route})


def in_folder(fake, folder_id: str) -> list[dict]:
    return [m for m in fake.messages.values() if m["parentFolderId"] == folder_id]


def one_in(fake, folder_id: str) -> dict:
    matches = in_folder(fake, folder_id)
    assert len(matches) == 1, f"expected one message in {folder_id}, found {len(matches)}"
    return matches[0]


def prop(message: dict, prop_id: str) -> str | None:
    for p in message.get("singleValueExtendedProperties", []):
        if p["id"] == prop_id:
            return p["value"]
    return None


def text_body_gets(fake) -> list[str]:
    """URLs of the standalone message GETs -- the text-body fetches, not the list."""
    return [
        url
        for method, url in fake.requests
        if method == "GET" and "/mailFolders/" not in url and "/messages/" in url
    ]


def setup_mailbox(graph_client, fake, slug="consent"):
    """Build the folder tree under the inbox and return (base_id, folder_ids)."""
    base_id = fake.inbox_id
    report = mailbox.resolve_layout(graph_client, base_id, [slug], create=True)
    return base_id, report.folder_ids


# -- tests -----------------------------------------------------------------


def test_happy_path_routes_to_completed(fake_graph, graph_client, pool, tmp_path):
    state_dir = tmp_path / "state"
    config = make_config(state_dir)
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)  # message.json: "consent|participant 4021", text-native

    seen = []
    handlers = {"consent": lambda m, c: seen.append((m, c))}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert stats.dispatched == 1
    assert stats.completed == 1
    assert len(seen) == 1
    message, context = seen[0]
    assert message.internet_message_id == "<redcap-0001@redcap.example.edu>"
    assert message.sender == "redcap@example.edu"
    assert message.body_text == 'slug = "consent"\n'
    assert message.body_html is None
    assert context.slug == "consent"
    assert context.state_dir == state_dir / "consent"
    assert context.state_dir.is_dir()

    landed = one_in(fake_graph, folder_ids["consent/completed"])
    assert prop(landed, dispatch.PROP_STATE) == "completed"
    # The claim wrote retries-left before dispatch; a brand-new message gets
    # the full budget.
    assert prop(landed, dispatch.PROP_RETRIES_LEFT) == "3"


def test_html_body_fetches_text_rendering(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id, fixture="message_html.json")

    seen = []
    handlers = {"consent": lambda m, c: seen.append((m, c))}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    message = seen[0][0]
    assert message.body_html is not None
    assert "participant_id = 4022" in message.body_html
    assert message.body_text == 'slug = "consent"\nparticipant_id = 4022\n'
    assert len(text_body_gets(fake_graph)) == 1


def test_text_body_needs_no_extra_get(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)  # text-native

    handlers = {"consent": lambda m, c: None}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert text_body_gets(fake_graph) == []


def test_transient_then_wait_then_success(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state", retry_backoff=timedelta(hours=1))
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    calls = []

    def handler(m, c):
        calls.append(m.internet_message_id)
        if len(calls) == 1:
            raise TransientError("come back later")

    handlers = {"consent": handler}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))
    event = threading.Event()

    # First pass: transient failure. Message stays put, retry-at is written.
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.transient_failures == 1
    resting = one_in(fake_graph, base_id)
    assert prop(resting, dispatch.PROP_RETRY_AT) is not None
    assert prop(resting, dispatch.PROP_RETRIES_LEFT) == "3"

    # Second pass, still before retry-at: left alone, handler not called again.
    clock.value = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.waiting == 1
    assert stats.dispatched == 0
    assert len(calls) == 1
    assert one_in(fake_graph, base_id)  # still resting in the base folder

    # Third pass, past retry-at: dispatched again, succeeds, filed as completed.
    clock.value = datetime(2026, 7, 9, 15, 30, tzinfo=UTC)
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.completed == 1
    assert len(calls) == 2
    landed = one_in(fake_graph, folder_ids["consent/completed"])
    assert prop(landed, dispatch.PROP_STATE) == "completed"


def test_permanent_error_routes_to_error_folder(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    def handler(m, c):
        raise PermanentError("never going to parse")

    handlers = {"consent": handler}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert stats.permanent_failures == 1
    landed = one_in(fake_graph, folder_ids["consent/error"])
    assert prop(landed, dispatch.PROP_STATE) == "errored"
    assert landed["categories"] == [dispatch.CATEGORY_ERRORED]


def test_poison_exhausts_retries_to_dead_letters(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state", max_retries=1, retry_backoff=timedelta(hours=1))
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    calls = []

    def handler(m, c):
        calls.append(1)
        raise RuntimeError("wedged in a way nobody labeled")

    handlers = {"consent": handler}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))
    event = threading.Event()

    # First attempt: claim writes the full budget (1), unexpected error = transient.
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.transient_failures == 1
    assert prop(one_in(fake_graph, base_id), dispatch.PROP_RETRIES_LEFT) == "1"

    # Second attempt, past backoff: claim decrements to 0, fails again.
    clock.value = datetime(2026, 7, 9, 16, 0, tzinfo=UTC)
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.transient_failures == 1
    assert prop(one_in(fake_graph, base_id), dispatch.PROP_RETRIES_LEFT) == "0"

    # Third pass: exhausted, so it's dead-lettered without another dispatch.
    clock.value = datetime(2026, 7, 9, 18, 0, tzinfo=UTC)
    stats = run_pass(
        graph_client, config, handlers, base_id, folder_ids, pool, now=clock, stop=event
    )
    assert stats.dead_lettered == 1
    assert stats.dispatched == 0
    assert len(calls) == 2  # dispatched exactly twice over the three passes

    landed = one_in(fake_graph, folder_ids["dead-letters"])
    assert prop(landed, dispatch.PROP_STATE) == "dead"
    assert landed["categories"] == [dispatch.CATEGORY_DEAD]


def test_crash_recovery_finishes_the_move_without_dispatch(
    fake_graph, graph_client, pool, tmp_path
):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    # A terminal marker was written, but the process died before the move.
    fake_graph.add_message(base_id, properties={dispatch.PROP_STATE: "completed"})

    called = []
    handlers = {"consent": lambda m, c: called.append(1)}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert stats.finished == 1
    assert stats.dispatched == 0
    assert called == []
    landed = one_in(fake_graph, folder_ids["consent/completed"])
    assert prop(landed, dispatch.PROP_STATE) == "completed"
    # Finish is move-only: nothing here should have issued a PATCH.
    assert not any(method == "PATCH" for method, _ in fake_graph.requests)


def test_unroutable_goes_to_dead_letters(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id, subject="nomatch|whatever")

    called = []
    handlers = {"consent": lambda m, c: called.append(1)}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert stats.dead_lettered == 1
    assert called == []
    landed = one_in(fake_graph, folder_ids["dead-letters"])
    assert prop(landed, dispatch.PROP_STATE) == "dead"


def test_expired_message_skips_dispatch(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state", max_age=timedelta(hours=1))
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)  # received 2026-07-09T13:45:12Z

    called = []
    handlers = {"consent": lambda m, c: called.append(1)}
    clock = Clock(datetime(2026, 7, 9, 20, 0, tzinfo=UTC))  # hours past max_age

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    assert stats.expired == 1
    assert stats.dispatched == 0
    assert called == []
    landed = one_in(fake_graph, folder_ids["dead-letters"])
    assert prop(landed, dispatch.PROP_STATE) == "expired"
    assert landed["categories"] == [dispatch.CATEGORY_EXPIRED]


def test_timeout_abandons_and_counts_as_transient(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state", handler_timeout=timedelta(milliseconds=50))
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    release = threading.Event()

    def wedged(m, c):
        release.wait(timeout=5)

    handlers = {"consent": wedged}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    try:
        stats = run_pass(
            graph_client,
            config,
            handlers,
            base_id,
            folder_ids,
            pool,
            now=clock,
            stop=threading.Event(),
        )
        assert stats.abandoned == 1
        assert stats.dispatched == 1
        resting = one_in(fake_graph, base_id)  # transient treatment leaves it put
        assert prop(resting, dispatch.PROP_RETRY_AT) is not None
    finally:
        release.set()


def test_stop_preset_skips_everything(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state")
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    called = []
    handlers = {"consent": lambda m, c: called.append(1)}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))
    stop = threading.Event()
    stop.set()

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=stop,
    )

    assert stats.skipped == 1
    assert stats.dispatched == 0
    assert called == []
    # Untouched: still in the base folder with no state written.
    resting = one_in(fake_graph, base_id)
    assert prop(resting, dispatch.PROP_STATE) is None


def test_dry_run_route_runs_handler_but_writes_nothing(fake_graph, graph_client, pool, tmp_path):
    config = make_config(tmp_path / "state", dry_run=True)
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    seen = []
    handlers = {"consent": lambda m, c: seen.append((m, c))}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    # The handler ran and saw dry_run in its context...
    assert stats.dispatched == 1
    assert stats.dry_run == 1
    assert stats.completed == 0
    assert len(seen) == 1
    assert seen[0][1].config["dry_run"] is True

    # ...but the message is untouched: still in the base folder, no claim, no
    # terminal state, and rah issued no PATCH at all.
    resting = one_in(fake_graph, base_id)
    assert prop(resting, dispatch.PROP_RETRIES_LEFT) is None
    assert prop(resting, dispatch.PROP_STATE) is None
    assert resting["categories"] == []
    assert not any(method == "PATCH" for method, _ in fake_graph.requests)


def test_dry_run_leaves_message_put_even_when_handler_errors(
    fake_graph, graph_client, pool, tmp_path
):
    config = make_config(tmp_path / "state", dry_run=True)
    base_id, folder_ids = setup_mailbox(graph_client, fake_graph)
    fake_graph.add_message(base_id)

    def handler(m, c):
        raise PermanentError("would never parse")

    handlers = {"consent": handler}
    clock = Clock(datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    stats = run_pass(
        graph_client,
        config,
        handlers,
        base_id,
        folder_ids,
        pool,
        now=clock,
        stop=threading.Event(),
    )

    # A dry-run route swallows the would-be permanent failure: nothing moves.
    assert stats.dry_run == 1
    assert stats.permanent_failures == 0
    resting = one_in(fake_graph, base_id)
    assert prop(resting, dispatch.PROP_STATE) is None
    assert not in_folder(fake_graph, folder_ids["consent/error"])


# -- build_message unit coverage -------------------------------------------


def test_build_message_falls_back_to_sender_when_from_missing():
    raw = {
        "internetMessageId": "<x@example.edu>",
        "subject": "consent|x",
        "receivedDateTime": "2026-07-09T13:45:12Z",
        "sender": {"emailAddress": {"address": "fallback@example.edu"}},
        "body": {"contentType": "text", "content": "hi"},
    }

    message = build_message(raw, lambda: "unused")

    assert message.sender == "fallback@example.edu"
    assert message.body_text == "hi"
    assert message.body_html is None
    assert message.received_at == datetime(2026, 7, 9, 13, 45, 12, tzinfo=UTC)
