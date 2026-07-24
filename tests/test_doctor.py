# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import requests
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


def _seed_folders(fake, base_folder_id, slugs=("simple",)):
    """Build the folder tree doctor's folders check expects, under base_folder_id.

    Imported lazily -- mailbox.py is what step 4 adds, so a test run before
    it exists should fail here with a clear ImportError, not at collection.
    """
    from redcap_alert_handler.mailbox import required_folder_paths

    ids = {"": base_folder_id}
    for path in required_folder_paths(slugs):
        parent_path, _, name = path.rpartition("/")
        folder = fake.add_folder(name, parent_id=ids[parent_path])
        ids[path] = folder["id"]
    return ids


def _seed_categories(fake):
    from redcap_alert_handler.mailbox import RAH_CATEGORIES

    for name, color in RAH_CATEGORIES.items():
        fake.add_category(name, color)


def _seed_full_layout(fake, base_folder_id, slugs=("simple",)):
    """Folders and categories both present, matching good_minimal's one route.

    good_full-based tests pass their two slugs (consent, push_alert)
    explicitly; everything else here defaults to good_minimal's "simple".
    """
    _seed_folders(fake, base_folder_id, slugs)
    _seed_categories(fake)


def test_missing_config_is_a_usage_error():
    result = runner.invoke(app, ["doctor"], env=CLEAN_ENV)
    assert result.exit_code == 2


def test_good_config_exits_zero(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ config okay" in result.output


def test_bad_config_reports_one_line_per_problem():
    # Six keys missing from [global], no route-level problems -- every
    # problem here belongs to the config check, not the newer routes check.
    result = runner.invoke(
        app,
        ["doctor", "--config", str(CONFIGS / "bad_missing_global_keys.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    error_lines = [line for line in result.output.splitlines() if "❌ config:" in line]
    assert len(error_lines) >= 3
    assert "❌ routes:" not in result.output


def test_config_and_route_problems_split_between_the_two_checks():
    # bad_multiple_problems.toml carries both a broken [global] (relative
    # token_cache_path, missing max_retries) and a broken route (bad-slug
    # slug, missing handler) -- each half belongs on its own check's lines.
    result = runner.invoke(
        app, ["doctor", "--config", str(CONFIGS / "bad_multiple_problems.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    lines = result.output.splitlines()
    config_lines = [line for line in lines if "❌ config:" in line]
    routes_lines = [line for line in lines if "❌ routes:" in line]

    assert any("token_cache_path" in line for line in config_lines)
    assert any("max_retries" in line for line in config_lines)
    assert not any("bad-slug" in line for line in config_lines)

    assert len(routes_lines) == 2
    assert all("routes.bad-slug" in line for line in routes_lines)


def test_routes_and_handlers_skipped_when_config_fails_to_load():
    # bad_missing_global_keys.toml never gets far enough to say anything
    # about routes -- global fails outright, so both downstream checks are
    # "not checked", the same way token cache and graph already are.
    result = runner.invoke(
        app,
        ["doctor", "--config", str(CONFIGS / "bad_missing_global_keys.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "routes not checked: the config didn't load" in result.output
    assert "handlers not checked: the config didn't load" in result.output
    assert "checkups not checked: the config didn't load" in result.output
    assert "❌ routes:" not in result.output
    assert "❌ handlers:" not in result.output


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
        app, ["-q", "doctor", "--config", str(CONFIGS / "bad_wrong_types.toml")], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    assert "❌ config:" in result.output


def test_good_secrets_reports_okay(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ secrets okay" in result.output
    # Nothing was missing, so folders/categories pass with no "created" detail.
    assert "✅ folders okay" in result.output
    assert "✅ categories okay" in result.output


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
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
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
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
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
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
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


# --- the routes check ---


def test_routes_line_lists_slugs_in_config_order(write_config, cache_with_account):
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert "✅ routes okay: 2 routes: consent, push_alert" in result.output


def test_routes_line_is_singular_for_one_route(write_config, cache_with_account):
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert "✅ routes okay: 1 route: simple" in result.output


# --- the handlers check ---


def test_handlers_resolve_through_real_entry_points(write_config, cache_with_account):
    # No monkeypatching: log_message is the handler rah ships and registers
    # under rah.handlers itself, so resolving it here is the real path.
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ handlers okay: 1 handler resolved" in result.output


def test_unknown_handler_fails_with_route_name_and_group(write_config, cache_with_account):
    # redcap-alert-handler is installed; it just doesn't register this name.
    config = write_config(cache_with_account, base="good_unknown_handler.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    handler_lines = [line for line in result.output.splitlines() if "❌ handlers:" in line]
    assert len(handler_lines) == 1
    line = handler_lines[0]
    assert "redcap-alert-handler" in line
    assert "nonexistent_handler" in line
    assert "routes.simple" in line
    assert "rah.handlers" in line


def test_uninstalled_handler_package_fails_asking_if_installed(write_config, cache_with_account):
    config = write_config(cache_with_account, base="good_uninstalled_package_handler.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "is the handler package installed" in result.output


def test_unqualified_handler_fails_the_routes_check_not_the_handlers_check(
    write_config, cache_with_account
):
    # A bare entry-point name is now a config validation problem: the config
    # never finishes loading, so handlers never gets a chance to run at all.
    config = write_config(cache_with_account, base="bad_unqualified_handler.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ routes:" in result.output
    assert "handlers not checked: the config didn't load" in result.output


# --- the checkups check ---


def _handler(checkup=None):
    """A handler that refuses to be dispatched, optionally carrying a checkup."""

    def handle(message, context):
        raise AssertionError("doctor should never dispatch a message")

    if checkup is not None:
        # Same ignore a real handler package needs: attaching an attribute to
        # a function is fine at runtime, invisible to the type checker.
        handle.checkup = checkup  # ty: ignore[unresolved-attribute]
    return handle


def _install_handlers(monkeypatch, handlers):
    """Hand the checks a fixed slug -> handler map.

    Real entry-point resolution is covered above; what these tests need is a
    handler with a checkup attached, which no installed package has.
    """
    import redcap_alert_handler.checks as checks_mod

    monkeypatch.setattr(checks_mod, "load_handlers", lambda config: dict(handlers))


def test_a_handler_without_a_checkup_is_not_a_problem(write_config, cache_with_account):
    # log_message, the real registered handler, has no checkup -- and that's
    # an ordinary state of affairs, not something to fail over.
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ checkups okay: no route's handler offers one" in result.output


def test_a_happy_checkup_passes(write_config, cache_with_account, monkeypatch):
    _install_handlers(monkeypatch, {"simple": _handler(lambda context: [])})
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ checkups okay: 1 route checked" in result.output


def test_checkup_problems_fail_the_run_with_the_route_named(
    write_config, cache_with_account, monkeypatch
):
    _install_handlers(
        monkeypatch,
        {"simple": _handler(lambda context: ["model_file points at nothing", "no input_fields"])},
    )
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if "❌ checkups:" in line]
    assert len(lines) == 2
    assert all("routes.simple" in line for line in lines)
    assert any("model_file points at nothing" in line for line in lines)


def test_a_checkup_sees_its_route_context(write_config, cache_with_account, monkeypatch):
    seen = {}

    def checkup(context):
        seen[context.slug] = context
        return []

    _install_handlers(monkeypatch, {"consent": _handler(checkup), "push_alert": _handler(checkup)})
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0

    # The same Context a message would arrive with: the route's own extras,
    # the resolved max_age, and the route's state dir.
    assert set(seen) == {"consent", "push_alert"}
    consent = seen["consent"]
    assert consent.config["template"] == "consent_v2"
    assert consent.config["handler"] == "redcap-alert-handler:log_message"
    assert consent.config["max_age"] == timedelta(hours=3)
    assert consent.state_dir.name == "consent"


def test_only_some_routes_offering_a_checkup_says_which(
    write_config, cache_with_account, monkeypatch
):
    _install_handlers(
        monkeypatch, {"consent": _handler(lambda context: []), "push_alert": _handler()}
    )
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ checkups okay: 1 of 2 routes checked; no checkup for push_alert" in result.output


def test_a_raising_checkup_is_reported_and_the_rest_still_run(
    write_config, cache_with_account, monkeypatch
):
    def boom(context):
        raise RuntimeError("joblib exploded")

    _install_handlers(monkeypatch, {"consent": _handler(boom), "push_alert": _handler()})
    config = write_config(cache_with_account, base="good_full.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ checkups: routes.consent: checkup raised RuntimeError: joblib exploded" in (
        result.output
    )
    # A handler package's bad day doesn't take the rest of the report with it.
    assert "✅ token cache okay" in result.output


def test_a_checkup_that_returns_nonsense_is_reported(write_config, cache_with_account, monkeypatch):
    _install_handlers(monkeypatch, {"simple": _handler(lambda context: 17)})
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "checkup returned int, expected a list of problems" in result.output


def test_a_checkup_that_is_not_callable_is_reported(write_config, cache_with_account, monkeypatch):
    _install_handlers(monkeypatch, {"simple": _handler("not a function")})
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ checkups: routes.simple: the handler's checkup isn't callable" in result.output


def test_a_bare_string_from_a_checkup_counts_as_one_problem(
    write_config, cache_with_account, monkeypatch
):
    _install_handlers(monkeypatch, {"simple": _handler(lambda context: "everything is wrong")})
    config = write_config(cache_with_account)
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "❌ checkups: routes.simple: everything is wrong" in result.output


def test_checkups_skipped_when_the_handlers_dont_resolve(write_config, cache_with_account):
    config = write_config(cache_with_account, base="good_unknown_handler.toml")
    result = runner.invoke(app, ["doctor", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "checkups not checked: the handlers didn't resolve" in result.output
    assert "❌ checkups:" not in result.output


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
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
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
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
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


def test_network_failure_reports_advice_not_a_traceback(
    write_config, cache_with_account, fake_app, install_fake_msal
):
    # A DNS/connection outage during refresh must read as a check failure with
    # advice, not spill msal's requests traceback out of doctor.
    config = write_config(cache_with_account)
    install_fake_msal(
        fake_app(
            accounts=ACCOUNTS,
            silent_raises=requests.ConnectionError("Temporary failure in name resolution"),
        )
    )
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ token cache:" in result.output
    assert "network/DNS" in result.output
    assert "Traceback" not in result.output


def test_network_failure_at_app_construction_reports_advice(
    write_config, cache_with_account, monkeypatch
):
    # The real-world path: msal's tenant discovery runs when the app is built,
    # so a DNS outage raises inside build_app, before any refresh. doctor has
    # to catch it there too, not just around refresh_silently.
    config = write_config(cache_with_account)

    def raise_it(*args, **kwargs):
        raise requests.ConnectionError("Temporary failure in name resolution")

    monkeypatch.setattr("redcap_alert_handler.auth.msal.ConfidentialClientApplication", raise_it)
    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "❌ token cache:" in result.output
    assert "network/DNS" in result.output
    assert "Traceback" not in result.output


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
    _seed_full_layout(fake, rah["id"], slugs=("consent", "push_alert"))

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
    _seed_full_layout(fake, fake.inbox_id)

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
    assert "run rah init" in result.output


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
    # Both downstream checks need a folder id from the graph check to work
    # with, so they can't run either.
    assert "folders not checked" in result.output
    assert "categories not checked" in result.output


def test_fix_creates_a_missing_named_base_folder(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    # No "rah" folder exists yet; --fix should create it and carry on.
    install_fake_graph()

    result = runner.invoke(
        app,
        [
            "doctor",
            "--fix",
            "--config",
            str(config),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "created rah; 0 messages in rah for svc-rah@example.edu" in result.output


# --- the folders check ---


def test_missing_folders_fails_and_points_at_rah_init(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    from redcap_alert_handler.mailbox import required_folder_paths

    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    # The base folder itself exists (graph passes); nothing under it does.
    fake.add_folder("rah", parent_id=fake.root_id)

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    folder_lines = [line for line in result.output.splitlines() if "❌ folders:" in line]
    assert len(folder_lines) == 1
    expected = ", ".join(required_folder_paths(["consent", "push_alert"]))
    assert f"missing {expected}; run rah init to create them" in folder_lines[0]


# --- the categories check ---


def test_missing_categories_fails_and_points_at_rah_init(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    rah = fake.add_folder("rah", parent_id=fake.root_id)
    # Folders present, categories aren't -- isolates the categories check.
    _seed_folders(fake, rah["id"], slugs=("consent", "push_alert"))

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "✅ folders okay" in result.output
    assert (
        "❌ categories: missing rah:processing, rah:errored, rah:expired, rah:dead; "
        "run rah init to seed them" in result.output
    )


# --- --fix and `rah init` ---


def test_fix_provisions_missing_layout_and_categories(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    from redcap_alert_handler.mailbox import RAH_CATEGORIES, required_folder_paths

    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.add_folder("rah", parent_id=fake.root_id)

    result = runner.invoke(
        app,
        [
            "doctor",
            "--fix",
            "--config",
            str(config),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    expected_paths = required_folder_paths(["consent", "push_alert"])
    assert f"✅ folders okay: created {', '.join(expected_paths)}" in result.output
    assert "✅ categories okay: created" in result.output
    assert {c["displayName"] for c in fake.categories} == set(RAH_CATEGORIES)

    # Running it again finds everything already in place: idempotent, no
    # second round of creation.
    second = runner.invoke(
        app,
        [
            "doctor",
            "--fix",
            "--config",
            str(config),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert second.exit_code == 0
    assert "✅ folders okay" in second.output
    assert "folders okay: created" not in second.output
    assert "✅ categories okay" in second.output
    assert "categories okay: created" not in second.output


def test_init_behaves_like_doctor_fix(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account, base="good_full.toml")
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.add_folder("rah", parent_id=fake.root_id)

    result = runner.invoke(
        app,
        ["init", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "✅ folders okay: created" in result.output
    assert "✅ categories okay: created" in result.output


# --- --json ---


def test_json_report_on_a_green_run(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)

    # -q keeps stderr's INFO lines out of the way; CliRunner separates
    # result.stdout from result.stderr here (recent click's default), so
    # this isn't load-bearing for the parse below, just tidy.
    result = runner.invoke(
        app,
        [
            "doctor",
            "-q",
            "--json",
            "--config",
            str(config),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["ok"] is True
    names = [check["name"] for check in report["checks"]]
    assert names == [
        "config",
        "routes",
        "handlers",
        "checkups",
        "secrets",
        "token cache",
        "graph",
        "folders",
        "categories",
    ]
    assert all(check["status"] == "passed" for check in report["checks"])


def test_json_report_on_a_failing_run(write_config, empty_cache):
    config = write_config(empty_cache)
    result = runner.invoke(app, ["doctor", "-q", "--json", "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["token cache"]["status"] == "failed"
    assert by_name["secrets"]["status"] == "skipped"


# --- -o/--output ---


def test_output_flag_writes_json_to_a_file(
    tmp_path, write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)
    out = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "doctor",
            "--json",
            "-o",
            str(out),
            "--config",
            str(config),
            "--secrets",
            str(SECRETS / "good.toml"),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    report = json.loads(out.read_text())
    assert "checks" in report


def test_output_flag_without_json_writes_text_lines(write_config, cache_with_account, tmp_path):
    config = write_config(cache_with_account)
    out = tmp_path / "report.txt"

    result = runner.invoke(app, ["doctor", "-o", str(out), "--config", str(config)], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "✅ config okay" in out.read_text()


# --- message count and its cost ---


def test_zero_messages_reports_cleanly(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    _seed_full_layout(fake, fake.inbox_id)

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "0 messages in inbox for svc-rah@example.edu" in result.output


def test_message_count_comes_from_the_folder_not_a_listing(
    write_config, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # A doctor run shouldn't page through the whole mailbox just to report a
    # count -- the folder resource already carries totalItemCount.
    config = write_config(cache_with_account)
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.page_size = 2
    for i in range(5):
        fake.add_message(fake.inbox_id, subject=f"m{i}")
    _seed_full_layout(fake, fake.inbox_id)

    result = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--secrets", str(SECRETS / "good.toml")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0
    assert "5 messages in inbox for svc-rah@example.edu" in result.output
    assert not any("/messages" in url for _, url in fake.requests)
