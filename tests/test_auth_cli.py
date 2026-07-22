# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from pathlib import Path

import requests
from typer.testing import CliRunner

from redcap_alert_handler.cli.main import app

runner = CliRunner()

CONFIGS = Path(__file__).parent / "data" / "configs"
SECRETS = Path(__file__).parent / "data" / "secrets"

CLEAN_ENV = {"RAH_CONFIG": None, "RAH_SECRETS": None}

GOOD_SECRETS = str(SECRETS / "good.toml")


def _invoke(config_path, *, secrets=GOOD_SECRETS, input=None):
    return runner.invoke(
        app,
        ["auth", "--config", str(config_path), "--secrets", secrets],
        env=CLEAN_ENV,
        input=input,
    )


def test_missing_secrets_is_a_usage_error():
    result = runner.invoke(
        app, ["auth", "--config", str(CONFIGS / "good_minimal.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 2


def test_bad_config_reports_problems_and_exits_one():
    result = _invoke(CONFIGS / "bad_multiple_problems.toml")
    assert result.exit_code == 1
    assert "❌ config:" in result.output


def test_bad_secrets_reports_problems_and_exits_one():
    result = _invoke(CONFIGS / "good_minimal.toml", secrets=str(SECRETS / "bad_missing_key.toml"))
    assert result.exit_code == 1
    assert "❌ secrets:" in result.output


def test_bad_config_and_secrets_report_together():
    result = _invoke(CONFIGS / "bad_no_routes.toml", secrets=str(SECRETS / "bad_missing_key.toml"))
    assert result.exit_code == 1
    assert "❌ config:" in result.output
    assert "❌ secrets:" in result.output


def test_silent_refresh_short_circuits(
    write_config, cache_with_account, fake_app, install_fake_msal
):
    config = write_config(cache_with_account)
    install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )

    result = _invoke(config)

    assert result.exit_code == 0
    assert "✅" in result.output
    assert "cached token" in result.output
    assert "svc-rah@example.edu" in result.output


def test_network_failure_reports_advice_and_never_prompts(
    write_config, cache_with_account, monkeypatch
):
    # A DNS outage at build_app must exit with advice, not a traceback and not
    # a sign-in prompt the operator can't complete offline.
    config = write_config(cache_with_account)

    def raise_it(*args, **kwargs):
        raise requests.ConnectionError("Temporary failure in name resolution")

    monkeypatch.setattr("redcap_alert_handler.auth.msal.ConfidentialClientApplication", raise_it)

    result = _invoke(config)

    assert result.exit_code == 1
    assert "network/DNS" in result.output
    assert "Traceback" not in result.output
    assert "Redirect URL" not in result.output


def test_silent_refresh_never_prompts(
    write_config, cache_with_account, fake_app, install_fake_msal
):
    config = write_config(cache_with_account)
    app_fake = install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )

    result = _invoke(config)

    assert result.exit_code == 0
    assert app_fake.initiate_calls == []
    assert app_fake.redeem_calls == []


def test_interactive_flow_signs_in(write_config, tmp_path, fake_app, install_fake_msal):
    config = write_config(tmp_path / "cache.json")  # no cache file yet
    app_fake = install_fake_msal(
        fake_app(
            redeem_result={
                "access_token": "AT",
                "expires_in": 3600,
                "id_token_claims": {"preferred_username": "svc-rah@example.edu"},
            },
        )
    )

    result = _invoke(config, input="http://localhost/auth?code=xyz&state=abc123\n")

    assert result.exit_code == 0
    # the sign-in URL is printed plainly so it survives -q and pipes
    assert app_fake.flow["auth_uri"] in result.output
    assert "✅" in result.output
    assert "svc-rah@example.edu" in result.output
    assert app_fake.redeem_calls == [(app_fake.flow, {"code": "xyz", "state": "abc123"})]


def test_msal_error_exits_one(write_config, tmp_path, fake_app, install_fake_msal):
    config = write_config(tmp_path / "cache.json")
    install_fake_msal(
        fake_app(
            redeem_result={
                "error": "invalid_grant",
                "error_description": "the code has expired",
            },
        )
    )

    result = _invoke(config, input="http://localhost/auth?code=xyz&state=abc123\n")

    assert result.exit_code == 1
    assert "💥" in result.output
    assert "invalid_grant" in result.output
    assert "the code has expired" in result.output


def test_pasted_url_without_query_exits_cleanly(
    write_config, tmp_path, fake_app, install_fake_msal
):
    config = write_config(tmp_path / "cache.json")
    install_fake_msal(fake_app())

    result = _invoke(config, input="http://localhost/auth\n")

    assert result.exit_code == 1
    assert "💥" in result.output
    assert "Traceback" not in result.output
