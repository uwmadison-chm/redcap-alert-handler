# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from redcap_alert_handler.cli.main import app

runner = CliRunner()

CONFIGS = Path(__file__).parent / "data" / "configs"
SECRETS = Path(__file__).parent / "data" / "secrets"

# Tests that exercise --config directly clear both env vars first, so a
# developer's shell environment can't leak into the result.
CLEAN_ENV = {"RAH_CONFIG": None, "RAH_SECRETS": None}

# Most all-green tests want a token cache that exists and holds an account,
# so the write_config/cache_with_account fixtures (conftest.py) stand in for
# the /var/lib paths the checked-in fixture configs carry.

REFRESHED = {"access_token": "AT", "expires_in": 3600}
ACCOUNTS = [{"username": "svc-rah@example.edu"}]


def test_missing_config_is_a_usage_error():
    result = runner.invoke(app, ["doctor"], env=CLEAN_ENV)
    assert result.exit_code == 2


def test_good_config_exits_zero(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
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
def test_verbose_dumps_loaded_config(args, write_config, cache_with_account):
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(app, [*args, str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "consent" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["-q", "doctor", "--config"],
        ["doctor", "-q", "--config"],
    ],
)
def test_quiet_good_config_has_no_checkmark(args, write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, [*args, str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅" not in result.output


def test_quiet_bad_config_still_shows_errors():
    result = runner.invoke(
        app, ["-q", "doctor", "--config", str(CONFIGS / "bad_no_routes.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    assert "❌ config:" in result.output


def test_good_secrets_reports_okay(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
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


def test_far_future_expiry_passes_without_warning(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good_with_expiry.toml")],
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


def test_soon_expiring_secret_warns_but_passes(
    tmp_path, write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # The warning window is relative to today, so this fixture has to be
    # generated: the good secrets file plus a date two weeks out.
    soon = date.today() + timedelta(days=14)
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        (SECRETS / "good.toml").read_text() + f"client_secret_expires = {soon.isoformat()}\n"
    )
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(secrets_file)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output
    assert "⚠️ secrets:" in result.output


def test_omitted_secrets_notes_they_were_not_checked(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ secrets" not in result.output
    assert "❌ secrets" not in result.output
    assert "not checked" in result.output


def test_rah_config_env_var_is_picked_up(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor"], env={**CLEAN_ENV, "RAH_CONFIG": str(config)})
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_config_flag_beats_rah_config_env_var(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config)],
        env={**CLEAN_ENV, "RAH_CONFIG": str(CONFIGS / "bad_no_routes.toml")},
    )
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_rah_secrets_env_var_is_picked_up(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config)],
        env={**CLEAN_ENV, "RAH_SECRETS": str(SECRETS / "good.toml")},
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output


def test_rah_debug_env_var_enables_debug_output(write_config, cache_with_account):
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config)],
        env={**CLEAN_ENV, "RAH_DEBUG": "1"},
    )
    assert result.exit_code == 0
    assert "consent" in result.output


# --- the token cache check ---


def test_missing_cache_file_fails_and_points_at_rah_auth(write_config, tmp_path):
    config = write_config(tmp_path / "no-such-cache.json")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ token cache:" in result.output
    assert "rah auth" in result.output


def test_cache_without_account_fails(write_config, empty_cache):
    config = write_config(empty_cache)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ token cache:" in result.output
    assert "rah auth" in result.output


def test_cache_with_account_but_no_secrets_passes_with_a_note(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert (
        "✅ token cache okay: signed in as svc-rah@example.edu, "
        "but refresh not tried without secrets" in result.output
    )


def test_refreshable_cache_passes(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ token cache okay: authenticated as svc-rah@example.edu" in result.output
    # token lifetime is DEBUG-only detail
    assert "token valid" not in result.output
    assert "good until" not in result.output


def test_verbose_shows_token_validity(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # msal reports the cached token's *remaining* life, so odd values like
    # 3541 are the norm; "59 minutes and 1 second" is more than anyone needs
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result={"expires_in": 3541}))
    install_fake_graph()
    result = runner.invoke(
        app,
        ["doctor", "-v", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "token valid for 59 minutes" in result.output
    assert "good until" in result.output


def test_unrefreshable_cache_fails(write_config, cache_with_account, fake_app, install_fake_msal):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=None))
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ token cache:" in result.output
    assert "rah auth" in result.output


def test_broken_config_skips_the_cache_check():
    result = runner.invoke(
        app, ["doctor", "--config", str(CONFIGS / "bad_no_routes.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    assert "❌ token cache" not in result.output
    assert "✅ token cache" not in result.output


# --- the Graph check ---


def test_healthy_mailbox_reports_count(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # good_full sets base_folder = "rah", a child of the mailbox root.
    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    rah = fake.add_folder("rah", parent_id=fake.root_id)
    for i in range(4):
        fake.add_message(rah["id"], subject=f"consent {i}")

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ graph okay: 4 messages in rah for svc-rah@example.edu" in result.output


def test_inbox_base_folder_uses_the_well_known_inbox(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # good_minimal leaves base_folder at its "inbox" default.
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.add_message(fake.inbox_id, subject="just one")

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ graph okay: 1 message in inbox for svc-rah@example.edu" in result.output


def test_missing_base_folder_fails_with_a_clear_message(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    # No "rah" folder created, so resolving the base folder comes up empty.
    install_fake_graph()

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ graph:" in result.output
    assert "rah" in result.output


def test_graph_unreachable_fails_without_a_traceback(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.enqueue_exception(httpx.ConnectError("no route to host"))

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ graph:" in result.output
    assert "Traceback" not in result.output


def test_graph_skipped_without_a_refreshed_token(write_config, cache_with_account):
    # No secrets given, so the token never refreshes and Graph can't be tried.
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "graph not checked" in result.output
    assert "✅ graph" not in result.output
    assert "❌ graph" not in result.output
