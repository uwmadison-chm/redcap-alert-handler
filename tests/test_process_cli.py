# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
from typer.testing import CliRunner

import redcap_alert_handler.cli.process as process_mod
from redcap_alert_handler.cli.main import app

runner = CliRunner()

CONFIGS = Path(__file__).parent / "data" / "configs"
SECRETS = Path(__file__).parent / "data" / "secrets"
GOOD_SECRETS = SECRETS / "good.toml"

# Clear both env vars so a developer's shell can't leak a config or secrets
# path into a test that means to run without one.
CLEAN_ENV = {"RAH_CONFIG": None, "RAH_SECRETS": None}

REFRESHED = {"access_token": "AT", "expires_in": 3600}
ACCOUNTS = [{"username": "svc-rah@example.edu"}]


def write_process_config(tmp_path, cache_path, base="good_minimal.toml"):
    """A fixture config with both /var/lib paths swapped for tmp_path ones.

    process actually runs a pass, so state_base_dir has to land somewhere
    writable -- unlike doctor, which only ever reads.
    """
    text = (CONFIGS / base).read_text()
    text = text.replace('"/var/lib/rah/token-cache.json"', f'"{cache_path}"')
    text = text.replace('"/var/lib/rah/state"', f'"{tmp_path / "state"}"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def seed_folders(fake, base_id, slugs=("simple",)):
    """Build the base folder's rah tree on the fake; return path -> id."""
    from redcap_alert_handler.mailbox import required_folder_paths

    ids = {"": base_id}
    for path in required_folder_paths(slugs):
        parent, _, name = path.rpartition("/")
        folder = fake.add_folder(name, parent_id=ids[parent])
        ids[path] = folder["id"]
    return ids


def add_fresh_message(fake, folder_id, subject="simple|x"):
    """A routable message received just now, so max_age never expires it."""
    message = fake.add_message(folder_id, subject=subject)
    # message.json is dated 2026-07-09; a live now() would age it out under
    # good_minimal's 1d max_age, so stamp it to the wall clock instead.
    message["receivedDateTime"] = datetime.now(UTC).isoformat()
    return message


def wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph):
    """Fake msal (a working refresh) plus a fake Graph patched into process."""
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    return install_fake_graph(module=process_mod)


def messages_in(fake, folder_id):
    return [m for m in fake.messages.values() if m["parentFolderId"] == folder_id]


def prop(message, prop_id):
    for p in message.get("singleValueExtendedProperties", []):
        if p["id"] == prop_id:
            return p["value"]
    return None


# -- one-shot ---------------------------------------------------------------


def test_happy_one_shot_files_the_message(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    fake = wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    ids = seed_folders(fake, fake.inbox_id)
    add_fresh_message(fake, fake.inbox_id)
    config = write_process_config(tmp_path, cache_with_account)

    result = runner.invoke(
        app,
        ["process", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    landed = messages_in(fake, ids["simple/completed"])
    assert len(landed) == 1


def test_one_shot_survives_a_failing_handler(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph, monkeypatch
):
    from redcap_alert_handler import dispatch

    fake = wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    seed_folders(fake, fake.inbox_id)
    add_fresh_message(fake, fake.inbox_id)
    config = write_process_config(tmp_path, cache_with_account)

    def boom(message, context):
        raise RuntimeError("handler blew up")

    # The loader is tested elsewhere; here we only need a handler that fails.
    monkeypatch.setattr(process_mod, "load_handlers", lambda cfg: {"simple": boom})

    result = runner.invoke(
        app,
        ["process", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    # A failed handler is a recorded outcome, not infrastructure trouble.
    assert result.exit_code == 0, result.output
    resting = messages_in(fake, fake.inbox_id)
    assert len(resting) == 1
    assert prop(resting[0], dispatch.PROP_RETRY_AT) is not None


def test_missing_folders_one_shot_points_at_rah_init(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    # Base folder (inbox) exists, but its rah tree was never provisioned.
    wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    config = write_process_config(tmp_path, cache_with_account)

    result = runner.invoke(
        app,
        ["process", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "rah init" in result.output


def test_graph_transport_failure_one_shot(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    fake = wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    seed_folders(fake, fake.inbox_id)
    fake.enqueue_exception(httpx.ConnectError("boom"))
    config = write_process_config(tmp_path, cache_with_account)

    result = runner.invoke(
        app,
        ["process", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "💥" in result.output


def test_auth_refresh_failure_one_shot_mentions_rah_auth(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=None))
    install_fake_graph(module=process_mod)
    config = write_process_config(tmp_path, cache_with_account)

    result = runner.invoke(
        app,
        ["process", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "rah auth" in result.output


# -- startup / usage errors -------------------------------------------------


def test_bad_config_reports_one_line_per_problem():
    result = runner.invoke(
        app,
        [
            "process",
            "--config",
            str(CONFIGS / "bad_missing_global_keys.toml"),
            "--secrets",
            str(GOOD_SECRETS),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    problem_lines = [line for line in result.output.splitlines() if "💥 config:" in line]
    assert len(problem_lines) >= 3


def test_missing_config_is_a_usage_error():
    result = runner.invoke(app, ["process"], env=CLEAN_ENV)
    assert result.exit_code == 2


# -- poll interval ----------------------------------------------------------


def test_poll_interval_without_watch_warns_but_runs(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph
):
    fake = wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    ids = seed_folders(fake, fake.inbox_id)
    add_fresh_message(fake, fake.inbox_id)
    config = write_process_config(tmp_path, cache_with_account)

    result = runner.invoke(
        app,
        [
            "process",
            "--poll-interval",
            "3s",
            "--config",
            str(config),
            "--secrets",
            str(GOOD_SECRETS),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    assert "--watch" in result.output  # the warning names the missing flag
    assert len(messages_in(fake, ids["simple/completed"])) == 1


def test_unparseable_poll_interval_is_a_usage_error(tmp_path, cache_with_account):
    config = write_process_config(tmp_path, cache_with_account)
    result = runner.invoke(
        app,
        [
            "process",
            "--poll-interval",
            "nope",
            "--config",
            str(config),
            "--secrets",
            str(GOOD_SECRETS),
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 2


# -- watch mode -------------------------------------------------------------


def test_watch_survives_a_graph_error_and_keeps_polling(
    tmp_path, cache_with_account, fake_app, install_fake_msal, install_fake_graph, monkeypatch
):
    fake = wired_auth_and_graph(install_fake_msal, fake_app, install_fake_graph)
    seed_folders(fake, fake.inbox_id)
    add_fresh_message(fake, fake.inbox_id)
    config = write_process_config(tmp_path, cache_with_account)

    # Bound the loop through the wait seam, and use it to inject a transport
    # failure between cycles: the second cycle's list should raise, get logged,
    # and leave the loop running rather than crashing it.
    waits = []

    def fake_wait(stop, seconds):
        waits.append(seconds)
        if len(waits) == 1:
            fake.enqueue_exception(httpx.ConnectError("boom"))
        else:
            stop.set()

    monkeypatch.setattr(process_mod, "_wait_for_next_cycle", fake_wait)

    result = runner.invoke(
        app,
        ["process", "--watch", "--config", str(config), "--secrets", str(GOOD_SECRETS)],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    assert len(waits) == 2  # two cycles ran
    lists = [
        url
        for method, url in fake.requests
        if method == "GET" and "/mailFolders/" in url and url.split("?")[0].endswith("/messages")
    ]
    assert len(lists) == 2
    assert "💥" in result.output
