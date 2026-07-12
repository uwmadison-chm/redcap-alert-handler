# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import logging

import pytest

from redcap_alert_handler.cli.conventions import resolve_log_level


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
