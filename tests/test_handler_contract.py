# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import pickle
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from redcap_alert_handler.config import GlobalConfig, RouteConfig
from redcap_alert_handler.handlers import (
    Context,
    HandlerError,
    Message,
    PermanentError,
    TransientError,
)
from redcap_alert_handler.handlers.contract import build_context

# --- exception hierarchy --------------------------------------------------


def test_transient_error_is_a_handler_error():
    assert issubclass(TransientError, HandlerError)


def test_permanent_error_is_a_handler_error():
    assert issubclass(PermanentError, HandlerError)


def test_handler_error_is_an_exception():
    assert issubclass(HandlerError, Exception)


# --- Message and Context: frozen, picklable ------------------------------


def _message(**overrides):
    fields = {
        "internet_message_id": "<abc123@example.edu>",
        "subject": "REDCap Alert",
        "body_text": "something happened",
        "body_html": None,
        "sender": "redcap@example.edu",
        "received_at": datetime(2026, 7, 12, 9, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return Message(**fields)


def _context(**overrides):
    fields = {
        "slug": "simple",
        "config": {"handler": "redcap-alert-handler:log_message", "max_age": timedelta(days=1)},
        "state_dir": Path("/var/lib/rah/state/simple"),
    }
    fields.update(overrides)
    return Context(**fields)


def test_message_is_frozen():
    message = _message()
    with pytest.raises(FrozenInstanceError):
        message.subject = "changed"


def test_context_is_frozen():
    context = _context()
    with pytest.raises(FrozenInstanceError):
        context.slug = "changed"


def test_message_survives_a_pickle_round_trip():
    message = _message()
    assert pickle.loads(pickle.dumps(message)) == message


def test_context_survives_a_pickle_round_trip():
    context = _context()
    assert pickle.loads(pickle.dumps(context)) == context


# --- build_context: the merge rule ----------------------------------------


def _global_config(**overrides):
    fields = {
        "mailbox": "svc-rah@example.edu",
        "base_folder": "inbox",
        "token_cache_path": Path("/var/lib/rah/token-cache.json"),
        "state_base_dir": Path("/var/lib/rah/state"),
        "handler_timeout": timedelta(seconds=60),
        "max_retries": 5,
        "retry_backoff": timedelta(minutes=5),
        "max_age": timedelta(days=1),
        "max_workers": 4,
        "extra": MappingProxyType({}),
    }
    fields.update(overrides)
    return GlobalConfig(**fields)


def _route_config(**overrides):
    fields = {
        "slug": "simple",
        "handler": "redcap-alert-handler:log_message",
        "max_age": timedelta(days=1),
        "extra": MappingProxyType({}),
    }
    fields.update(overrides)
    return RouteConfig(**fields)


def test_build_context_state_dir_is_state_base_dir_slash_slug():
    global_config = _global_config(state_base_dir=Path("/var/lib/rah/state"))
    route = _route_config(slug="consent")

    context = build_context(global_config, route)

    assert context.state_dir == Path("/var/lib/rah/state/consent")


def test_build_context_carries_slug():
    global_config = _global_config()
    route = _route_config(slug="push_alert")

    context = build_context(global_config, route)

    assert context.slug == "push_alert"


def test_build_context_config_carries_handler_and_resolved_max_age():
    global_config = _global_config(max_age=timedelta(days=1))
    route = _route_config(handler="redcap-alert-handler:log_message", max_age=timedelta(hours=3))

    context = build_context(global_config, route)

    assert context.config["handler"] == "redcap-alert-handler:log_message"
    assert context.config["max_age"] == timedelta(hours=3)


def test_build_context_global_extra_flows_into_config():
    global_config = _global_config(extra=MappingProxyType({"graph_base_url": "https://x"}))
    route = _route_config()

    context = build_context(global_config, route)

    assert context.config["graph_base_url"] == "https://x"


def test_build_context_route_extra_overrides_same_named_global_extra():
    global_config = _global_config(extra=MappingProxyType({"model": "group-assign-v1"}))
    route = _route_config(extra=MappingProxyType({"model": "group-assign-v3"}))

    context = build_context(global_config, route)

    assert context.config["model"] == "group-assign-v3"


def test_build_context_route_extra_without_conflict_also_present():
    global_config = _global_config(extra=MappingProxyType({"graph_base_url": "https://x"}))
    route = _route_config(extra=MappingProxyType({"template": "consent_v2"}))

    context = build_context(global_config, route)

    assert context.config["graph_base_url"] == "https://x"
    assert context.config["template"] == "consent_v2"


_ENGINE_KEYS = (
    "mailbox",
    "base_folder",
    "token_cache_path",
    "state_base_dir",
    "handler_timeout",
    "max_retries",
    "retry_backoff",
    "max_workers",
)


@pytest.mark.parametrize("key", _ENGINE_KEYS)
def test_build_context_does_not_leak_engine_keys(key):
    global_config = _global_config()
    route = _route_config()

    context = build_context(global_config, route)

    assert key not in context.config
