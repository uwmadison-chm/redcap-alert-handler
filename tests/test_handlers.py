# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import pytest

from redcap_alert_handler.handlers import HANDLER_GROUP, HandlerResolutionError, resolve_handler


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
