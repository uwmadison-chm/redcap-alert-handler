# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from datetime import date, timedelta
from pathlib import Path

import pytest

from redcap_alert_handler.config import ConfigError, load_config, load_secrets

CONFIGS = Path(__file__).parent / "data" / "configs"
SECRETS = Path(__file__).parent / "data" / "secrets"


def test_load_good_minimal_config():
    config = load_config(CONFIGS / "good_minimal.toml")

    assert config.global_config.mailbox == "svc-rah@example.edu"
    # not set in the fixture: the watcher polls the real Inbox by default
    assert config.global_config.base_folder == "inbox"
    assert config.global_config.token_cache_path == Path("/var/lib/rah/token-cache.json")
    assert config.global_config.state_base_dir == Path("/var/lib/rah/state")
    assert config.global_config.polling_interval == timedelta(seconds=5)
    assert config.global_config.handler_timeout == timedelta(seconds=60)
    assert config.global_config.max_retries == 5
    assert config.global_config.retry_backoff == timedelta(minutes=5)
    assert config.global_config.max_age == timedelta(days=1)
    assert config.global_config.extra == {}

    assert set(config.routes) == {"simple"}
    route = config.routes["simple"]
    assert route.slug == "simple"
    assert route.handler == "redcap-alert-handler:log_message"
    assert route.max_age == timedelta(days=1)
    assert route.extra == {}


def test_load_good_full_config():
    config = load_config(CONFIGS / "good_full.toml")

    assert config.global_config.base_folder == "rah"
    assert config.global_config.extra == {"graph_base_url": "https://graph.microsoft.com/v1.0"}
    assert set(config.routes) == {"consent", "push_alert"}

    consent = config.routes["consent"]
    assert consent.handler == "redcap-alert-handler:log_message"
    # per-route override wins over the global default
    assert consent.max_age == timedelta(hours=3)
    assert consent.extra == {"template": "consent_v2"}

    push_alert = config.routes["push_alert"]
    assert push_alert.handler == "redcap-alert-handler:log_message"
    # no override: falls back to global max_age
    assert push_alert.max_age == timedelta(days=1)
    assert push_alert.extra == {"model": "group-assign-v3"}


BAD_CONFIGS = [
    (
        "bad_missing_global_keys.toml",
        [
            "global.mailbox",
            "global.polling_interval",
            "global.handler_timeout",
            "global.max_retries",
            "global.retry_backoff",
            "global.max_age",
        ],
    ),
    ("bad_wrong_types.toml", ["global.token_cache_path", "global.polling_interval"]),
    ("bad_relative_paths.toml", ["global.token_cache_path", "global.state_base_dir"]),
    ("bad_duration_strings.toml", ["global.polling_interval"]),
    ("bad_nonpositive_durations.toml", ["global.polling_interval", "global.handler_timeout"]),
    ("bad_slug.toml", ["routes.bad-slug"]),
    ("bad_missing_handler.toml", ["routes.myslug"]),
    ("bad_empty_handler.toml", ["routes.myslug"]),
    ("bad_unqualified_handler.toml", ["routes.simple"]),
    ("bad_unknown_top_level_section.toml", ["gobal"]),
    ("bad_no_routes.toml", ["routes"]),
    ("bad_scalar_route.toml", ["routes.myslug"]),
    ("bad_negative_max_retries.toml", ["global.max_retries"]),
    ("bad_base_folder.toml", ["global.base_folder"]),
    ("bad_boolean_max_retries.toml", ["global.max_retries"]),
    (
        "bad_multiple_problems.toml",
        ["global.token_cache_path", "global.max_retries", "routes.bad-slug"],
    ),
    ("bad_missing_global_section.toml", ["global"]),
]


@pytest.mark.parametrize(("filename", "expected_substrings"), BAD_CONFIGS)
def test_bad_config_reports_problems(filename, expected_substrings):
    with pytest.raises(ConfigError) as exc_info:
        load_config(CONFIGS / filename)

    problems = exc_info.value.problems
    for substring in expected_substrings:
        assert any(substring in problem for problem in problems), (
            f"expected a problem mentioning {substring!r}, got {problems!r}"
        )


def test_bad_unqualified_handler_reports_the_qualified_form():
    # A bare entry-point name is no longer enough; the message has to show
    # an operator what a fixed handler line looks like.
    with pytest.raises(ConfigError) as exc_info:
        load_config(CONFIGS / "bad_unqualified_handler.toml")

    problems = exc_info.value.problems
    assert any("package-name:handler_name" in problem for problem in problems)


def test_bad_multiple_problems_reports_all_at_once():
    with pytest.raises(ConfigError) as exc_info:
        load_config(CONFIGS / "bad_multiple_problems.toml")

    # relative path, bad slug, and missing handler should all show up together,
    # not just the first one hit
    assert len(exc_info.value.problems) >= 3


def test_missing_config_file():
    with pytest.raises(ConfigError):
        load_config(CONFIGS / "does_not_exist.toml")


def test_non_utf8_config_file():
    # A latin-1 config must come back as advice, not a UnicodeDecodeError.
    with pytest.raises(ConfigError) as exc_info:
        load_config(CONFIGS / "bad_not_utf8.toml")
    assert any("UTF-8" in problem for problem in exc_info.value.problems)


def test_load_good_secrets():
    secrets = load_secrets(SECRETS / "good.toml")
    assert secrets.tenant_id == "11111111-1111-1111-1111-111111111111"
    assert secrets.client_id == "22222222-2222-2222-2222-222222222222"
    assert secrets.client_secret == "correct-horse-battery-staple"
    # optional; absent means nobody recorded it
    assert secrets.client_secret_expires is None


def test_load_secrets_with_expiry_date():
    secrets = load_secrets(SECRETS / "good_with_expiry.toml")
    assert secrets.client_secret_expires == date(2035, 1, 9)


def test_expired_secrets_still_load():
    # A past date is doctor's business, not a validation failure
    secrets = load_secrets(SECRETS / "expired.toml")
    assert secrets.client_secret_expires == date(2020, 1, 9)


def test_secrets_repr_does_not_leak_client_secret():
    secrets = load_secrets(SECRETS / "good.toml")
    assert "correct-horse-battery-staple" not in repr(secrets)


BAD_SECRETS = [
    ("bad_missing_key.toml", ["secrets.client_secret"]),
    ("bad_empty_value.toml", ["secrets.client_id"]),
    ("bad_unknown_key.toml", ["secrets.client_secert"]),
    ("bad_expiry_string.toml", ["secrets.client_secret_expires"]),
]


@pytest.mark.parametrize(("filename", "expected_substrings"), BAD_SECRETS)
def test_bad_secrets_reports_problems(filename, expected_substrings):
    with pytest.raises(ConfigError) as exc_info:
        load_secrets(SECRETS / filename)

    problems = exc_info.value.problems
    for substring in expected_substrings:
        assert any(substring in problem for problem in problems), (
            f"expected a problem mentioning {substring!r}, got {problems!r}"
        )


def test_missing_secrets_file():
    with pytest.raises(ConfigError):
        load_secrets(SECRETS / "does_not_exist.toml")
