# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from redcap_alert_handler.config import Config, GlobalConfig, RouteConfig
from redcap_alert_handler.handlers import Context, Message
from redcap_alert_handler.handlers.loader import (
    HANDLER_GROUP,
    HandlerResolutionError,
    load_handlers,
    resolve_handler,
)
from redcap_alert_handler.handlers.log_message import log_message


# No monkeypatching of importlib here: the point of this loader is that it
# goes through real entry points, and log_message is the one rah ships and
# registers under its own group for exactly this purpose.
def test_resolve_handler_finds_the_built_in_log_message():
    handler = resolve_handler("redcap-alert-handler:log_message")

    assert callable(handler)


def test_resolve_handler_matches_a_pep503_variant_spelling():
    # Redcap_Alert.Handler canonicalizes to redcap-alert-handler; a route
    # written with underscores or dots should still resolve.
    handler = resolve_handler("Redcap_Alert.Handler:log_message")

    assert callable(handler)


def test_resolve_unknown_name_in_installed_package_lists_what_it_registers():
    with pytest.raises(HandlerResolutionError) as exc_info:
        resolve_handler("redcap-alert-handler:nonexistent_handler")

    message = str(exc_info.value)
    assert "redcap-alert-handler" in message
    assert "nonexistent_handler" in message
    assert HANDLER_GROUP in message
    # what the package actually registers, so the operator can fix the typo
    assert "log_message" in message


def test_resolve_uninstalled_package_asks_if_it_is_installed():
    with pytest.raises(HandlerResolutionError) as exc_info:
        resolve_handler("study-acme-handlers:consent")

    message = str(exc_info.value)
    assert "study-acme-handlers" in message
    assert "is the handler package installed" in message


def test_resolve_malformed_ref_raises_mentioning_the_expected_form():
    with pytest.raises(HandlerResolutionError) as exc_info:
        resolve_handler("log_message")

    assert "package-name:handler_name" in str(exc_info.value)


# --- load_handlers: the fail-fast startup resolver ------------------------


def _config(routes: dict[str, str]) -> Config:
    # Only .routes matters to load_handlers; the global section is filled in
    # with values a route would never see.
    global_config = GlobalConfig(
        mailbox="svc-rah@example.edu",
        base_folder="inbox",
        token_cache_path=Path("/var/lib/rah/token-cache.json"),
        state_base_dir=Path("/var/lib/rah/state"),
        handler_timeout=timedelta(seconds=60),
        max_retries=5,
        retry_backoff=timedelta(minutes=5),
        max_age=timedelta(days=1),
        max_workers=4,
        extra=MappingProxyType({}),
    )
    route_configs = {
        slug: RouteConfig(slug=slug, handler=handler, max_age=timedelta(days=1), extra={})
        for slug, handler in routes.items()
    }
    return Config(global_config=global_config, routes=route_configs)


def test_load_handlers_happy_path_returns_slug_keyed_callables():
    config = _config({"simple": "redcap-alert-handler:log_message"})

    handlers = load_handlers(config)

    assert set(handlers) == {"simple"}
    assert callable(handlers["simple"])


def test_load_handlers_one_bad_ref_names_the_ref_and_the_route():
    config = _config({"simple": "redcap-alert-handler:nonexistent_handler"})

    with pytest.raises(HandlerResolutionError) as exc_info:
        load_handlers(config)

    message = str(exc_info.value)
    assert "nonexistent_handler" in message
    assert "wanted by routes.simple" in message


def test_load_handlers_two_bad_refs_both_reported_in_one_raise():
    config = _config(
        {
            "consent": "redcap-alert-handler:nonexistent_handler",
            "push_alert": "study-acme-handlers:consent",
        }
    )

    with pytest.raises(HandlerResolutionError) as exc_info:
        load_handlers(config)

    problems = exc_info.value.problems
    assert len(problems) == 2
    assert any("nonexistent_handler" in p and "wanted by routes.consent" in p for p in problems)
    assert any("study-acme-handlers" in p and "wanted by routes.push_alert" in p for p in problems)


def test_load_handlers_shared_ref_resolved_once_same_object_for_both_slugs():
    config = _config(
        {
            "consent": "redcap-alert-handler:log_message",
            "push_alert": "redcap-alert-handler:log_message",
        }
    )

    handlers = load_handlers(config)

    assert handlers["consent"] is handlers["push_alert"]


# --- the built-in log_message handler --------------------------------------


def test_log_message_logs_slug_and_id_but_never_the_subject(caplog):
    message = Message(
        internet_message_id="<abc123@example.edu>",
        subject="a subject that must never reach the log",
        body_text="body",
        body_html=None,
        sender="redcap@example.edu",
        received_at=datetime(2026, 7, 12, 9, 30, tzinfo=UTC),
    )
    context = Context(slug="simple", config={}, state_dir=Path("/var/lib/rah/state/simple"))

    with caplog.at_level(logging.INFO):
        log_message(message, context)

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "simple" in output
    assert "<abc123@example.edu>" in output
    assert "a subject that must never reach the log" not in output
