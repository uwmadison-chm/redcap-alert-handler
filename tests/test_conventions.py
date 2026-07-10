import io
import logging

import pytest

from redcap_alert_handler.cli.conventions import resolve_log_level, resolve_use_color


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected_level", "expect_warning"),
    [
        (False, False, logging.INFO, False),
        (True, False, logging.DEBUG, False),
        (False, True, logging.ERROR, False),
        (True, True, logging.DEBUG, True),
    ],
)
def test_resolve_log_level(verbose, quiet, expected_level, expect_warning):
    level, warning = resolve_log_level(verbose, quiet)
    assert level == expected_level
    assert (warning is not None) == expect_warning


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
