# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import io
import logging

# get_logger comes in through the package root on purpose: that's the
# spelling the handler contract promises to handler packages.
from redcap_alert_handler import get_logger
from redcap_alert_handler.logs import LOGGER_NAME, configure_logging, resolve_use_color


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# --- get_logger: everyone logs inside rah's hierarchy ----------------------


def test_get_logger_namespaces_a_handler_package_under_rah():
    logger = get_logger("acme_handlers.consent")

    assert logger.name == "redcap_alert_handler.handlers.acme_handlers.consent"


def test_get_logger_leaves_rah_internal_names_alone():
    logger = get_logger("redcap_alert_handler.handlers.log_message")

    assert logger.name == "redcap_alert_handler.handlers.log_message"


def test_handler_package_log_lines_reach_rah_output():
    # The point of get_logger: rah's handler sits on the package logger with
    # propagation off, so a logger outside the hierarchy never reaches this
    # stream. One built through get_logger does.
    stream = io.StringIO()
    configure_logging(logging.INFO, use_color=False, stream=stream)

    get_logger("acme_handlers.consent").info("handled one message")
    logging.getLogger("acme_handlers.consent").info("this one is lost")

    assert "handled one message" in stream.getvalue()
    assert "this one is lost" not in stream.getvalue()


# --- color resolution -------------------------------------------------------


def test_resolve_use_color_flag_wins(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("RAH_NO_COLOR", raising=False)
    assert resolve_use_color(True, FakeTTY()) is False


def test_resolve_use_color_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("RAH_NO_COLOR", raising=False)
    assert resolve_use_color(False, FakeTTY()) is False


def test_resolve_use_color_no_color_env_empty_does_not_count(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("RAH_NO_COLOR", raising=False)
    assert resolve_use_color(False, FakeTTY()) is True


def test_resolve_use_color_rah_no_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("RAH_NO_COLOR", "1")
    assert resolve_use_color(False, FakeTTY()) is False


def test_resolve_use_color_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("RAH_NO_COLOR", raising=False)
    assert resolve_use_color(False, io.StringIO()) is False


def test_resolve_use_color_tty_with_nothing_disabling(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("RAH_NO_COLOR", raising=False)
    assert resolve_use_color(False, FakeTTY()) is True


# --- the log handler itself --------------------------------------------------


def test_logging_to_a_closed_stream_does_not_raise():
    # A log handler can outlive its stream (CliRunner's captured stderr in
    # tests; a vanished stderr under systemd). Logging must never crash the
    # program that called it.
    stream = io.StringIO()
    configure_logging(logging.DEBUG, use_color=False, stream=stream)
    stream.close()

    logging.getLogger(LOGGER_NAME).debug("nobody is listening")
