# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msal
import pytest

from redcap_alert_handler import auth

CACHES = Path(__file__).parent / "data" / "token_caches"


def test_scopes_never_include_reserved_ones():
    # msal raises if openid/profile/offline_access are requested explicitly
    assert not {"openid", "profile", "offline_access"} & set(auth.SCOPES)


# --- cache load / save ---


def test_load_missing_cache_is_empty(tmp_path):
    cache = auth.load_token_cache(tmp_path / "nope.json")
    assert not auth.cache_has_account(cache)


def test_load_cache_with_account():
    cache = auth.load_token_cache(CACHES / "with_account.json")
    assert auth.cache_has_account(cache)


def test_load_empty_cache_has_no_account():
    cache = auth.load_token_cache(CACHES / "empty.json")
    assert not auth.cache_has_account(cache)


def test_cached_username_names_the_account():
    cache = auth.load_token_cache(CACHES / "with_account.json")
    assert auth.cached_username(cache) == "svc-rah@example.edu"


def test_cached_username_is_none_when_nobody_signed_in():
    cache = auth.load_token_cache(CACHES / "empty.json")
    assert auth.cached_username(cache) is None


def test_save_creates_parents_and_restricts_mode(tmp_path):
    cache = auth.load_token_cache(CACHES / "with_account.json")
    cache.has_state_changed = True
    path = tmp_path / "deep" / "down" / "cache.json"

    auth.save_token_cache(cache, path)

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_skips_when_state_unchanged(tmp_path):
    # deserialize resets has_state_changed, so a fresh load must not rewrite
    cache = auth.load_token_cache(CACHES / "with_account.json")
    assert not cache.has_state_changed
    path = tmp_path / "cache.json"

    auth.save_token_cache(cache, path)

    assert not path.exists()


def test_save_rewrites_existing_file_to_0600(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{}")
    path.chmod(0o644)
    cache = auth.load_token_cache(CACHES / "with_account.json")
    cache.has_state_changed = True

    auth.save_token_cache(cache, path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.cache_has_account(auth.load_token_cache(path))


def test_save_and_load_round_trip(tmp_path):
    cache = auth.load_token_cache(CACHES / "with_account.json")
    cache.has_state_changed = True
    path = tmp_path / "cache.json"

    auth.save_token_cache(cache, path)
    reloaded = auth.load_token_cache(path)

    assert auth.cache_has_account(reloaded)
    assert isinstance(reloaded, msal.SerializableTokenCache)


# --- redeeming the pasted redirect URL ---

FLOW = {"state": "abc123", "auth_uri": "https://login.microsoftonline.com/fake/authorize"}


def test_redeem_good_url(fake_app):
    app = fake_app(redeem_result={"access_token": "AT", "expires_in": 3600})

    result = auth.redeem_auth_response(app, FLOW, "http://localhost/auth?code=xyz&state=abc123")

    assert result["access_token"] == "AT"
    # msal wants the query params as a plain dict
    assert app.redeem_calls == [(FLOW, {"code": "xyz", "state": "abc123"})]


def test_redeem_url_without_query_is_a_clean_error(fake_app):
    app = fake_app()

    with pytest.raises(auth.AuthError):
        auth.redeem_auth_response(app, FLOW, "http://localhost/auth")

    # never even reached msal
    assert app.redeem_calls == []


def test_redeem_error_result_raises_with_azures_words(fake_app):
    app = fake_app(redeem_result={"error": "access_denied", "error_description": "user said no"})

    with pytest.raises(auth.AuthError) as exc_info:
        auth.redeem_auth_response(app, FLOW, "http://localhost/auth?error=access_denied")

    assert exc_info.value.error == "access_denied"
    assert exc_info.value.description == "user said no"


def test_redeem_state_mismatch_becomes_auth_error(fake_app):
    # msal raises ValueError when the state check fails
    app = fake_app(redeem_raises=ValueError("state mismatch: abc123 vs evil"))

    with pytest.raises(auth.AuthError) as exc_info:
        auth.redeem_auth_response(app, FLOW, "http://localhost/auth?code=xyz&state=evil")

    assert "state mismatch" in str(exc_info.value)


# --- silent refresh ---


def test_refresh_returns_token_for_a_cached_account(fake_app):
    account = {"username": "svc-rah@example.edu"}
    app = fake_app(accounts=[account], silent_result={"access_token": "AT"})

    result = auth.refresh_silently(app)

    assert result == {"access_token": "AT"}
    assert app.silent_calls == [(auth.SCOPES, account)]


def test_refresh_with_no_accounts_returns_none(fake_app):
    app = fake_app(silent_result={"access_token": "AT"})

    assert auth.refresh_silently(app) is None
    assert app.silent_calls == []


def test_refresh_returns_none_when_msal_gives_nothing(fake_app):
    app = fake_app(accounts=[{"username": "svc-rah@example.edu"}], silent_result=None)

    assert auth.refresh_silently(app) is None


# --- result helpers ---


def test_token_expiry_is_now_plus_expires_in():
    before = datetime.now(UTC)
    expiry = auth.token_expiry({"expires_in": 3600})
    after = datetime.now(UTC)

    assert before + timedelta(seconds=3600) <= expiry <= after + timedelta(seconds=3600)


def test_describe_account_prefers_id_token_claims(fake_app):
    app = fake_app(accounts=[{"username": "fallback@example.edu"}])
    result = {"id_token_claims": {"preferred_username": "svc-rah@example.edu"}}

    assert auth.describe_account(app, result) == "svc-rah@example.edu"


def test_describe_account_falls_back_to_the_account(fake_app):
    app = fake_app(accounts=[{"username": "svc-rah@example.edu"}])

    assert auth.describe_account(app, {}) == "svc-rah@example.edu"


def test_describe_account_when_nothing_is_known(fake_app):
    assert auth.describe_account(fake_app(), {}) == "unknown account"
