# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from redcap_alert_handler.cli.main import app

runner = CliRunner()

CONFIGS = Path(__file__).parent / "data" / "configs"
SECRETS = Path(__file__).parent / "data" / "secrets"

# Tests that exercise --config directly clear both env vars first, so a
# developer's shell environment can't leak into the result.
CLEAN_ENV = {"RAH_CONFIG": None, "RAH_SECRETS": None}


def test_missing_config_is_a_usage_error():
    result = runner.invoke(app, ["doctor"], env=CLEAN_ENV)
    assert result.exit_code == 2


def test_good_config_exits_zero():
    result = runner.invoke(
        app, ["doctor", "--config", str(CONFIGS / "good_minimal.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_bad_config_reports_one_line_per_problem():
    result = runner.invoke(
        app, ["doctor", "--config", str(CONFIGS / "bad_multiple_problems.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    error_lines = [line for line in result.output.splitlines() if "❌ config:" in line]
    assert len(error_lines) >= 3


# The universal flags work in both positions: before the subcommand (group
# level) and after it (subcommand level, which is what the plan's Done-when
# spells out as `rah doctor -v`).
@pytest.mark.parametrize(
    "args",
    [
        ["-v", "doctor", "--config"],
        ["doctor", "-v", "--config"],
    ],
)
def test_verbose_dumps_loaded_config(args):
    result = runner.invoke(app, [*args, str(CONFIGS / "good_full.toml")], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "consent" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["-q", "doctor", "--config"],
        ["doctor", "-q", "--config"],
    ],
)
def test_quiet_good_config_has_no_checkmark(args):
    result = runner.invoke(app, [*args, str(CONFIGS / "good_minimal.toml")], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅" not in result.output


def test_quiet_bad_config_still_shows_errors():
    result = runner.invoke(
        app, ["-q", "doctor", "--config", str(CONFIGS / "bad_no_routes.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    assert "❌ config:" in result.output


def test_good_secrets_reports_okay():
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(CONFIGS / "good_minimal.toml"),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output


def test_bad_secrets_fails_the_run():
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(CONFIGS / "good_minimal.toml"),
            "--secrets",
            str(SECRETS / "bad_missing_key.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ secrets:" in result.output


def test_far_future_expiry_passes_without_warning():
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(CONFIGS / "good_minimal.toml"),
            "--secrets",
            str(SECRETS / "good_with_expiry.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output
    assert "⚠️" not in result.output


def test_expired_secret_fails_the_run():
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(CONFIGS / "good_minimal.toml"),
            "--secrets",
            str(SECRETS / "expired.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ secrets:" in result.output
    assert "expired" in result.output


def test_soon_expiring_secret_warns_but_passes(tmp_path):
    # The warning window is relative to today, so this fixture has to be
    # generated: the good secrets file plus a date two weeks out.
    soon = date.today() + timedelta(days=14)
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        (SECRETS / "good.toml").read_text() + f"client_secret_expires = {soon.isoformat()}\n"
    )
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(CONFIGS / "good_minimal.toml"),
            "--secrets",
            str(secrets_file),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output
    assert "⚠️ secrets:" in result.output


def test_omitted_secrets_notes_they_were_not_checked():
    result = runner.invoke(
        app, ["doctor", "--config", str(CONFIGS / "good_minimal.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 0
    assert "✅ secrets" not in result.output
    assert "❌ secrets" not in result.output
    assert "not checked" in result.output


def test_rah_config_env_var_is_picked_up():
    result = runner.invoke(
        app, ["doctor"], env={**CLEAN_ENV, "RAH_CONFIG": str(CONFIGS / "good_minimal.toml")}
    )
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_config_flag_beats_rah_config_env_var():
    result = runner.invoke(
        app,
        ["doctor", "--config", str(CONFIGS / "good_minimal.toml")],
        env={**CLEAN_ENV, "RAH_CONFIG": str(CONFIGS / "bad_no_routes.toml")},
    )
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_rah_secrets_env_var_is_picked_up():
    result = runner.invoke(
        app,
        ["doctor", "--config", str(CONFIGS / "good_minimal.toml")],
        env={**CLEAN_ENV, "RAH_SECRETS": str(SECRETS / "good.toml")},
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output


def test_rah_debug_env_var_enables_debug_output():
    result = runner.invoke(
        app,
        ["doctor", "--config", str(CONFIGS / "good_full.toml")],
        env={**CLEAN_ENV, "RAH_DEBUG": "1"},
    )
    assert result.exit_code == 0
    assert "consent" in result.output
