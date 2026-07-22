# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msal
import pytest
import requests

from redcap_alert_handler import auth
from redcap_alert_handler.config import Secrets

CACHES = Path(__file__).parent / "data" / "token_caches"

SECRETS = Secrets(tenant_id="tenant", client_id="client", client_secret="secret")


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


def test_refresh_reraises_a_transport_failure_as_auth_network_error(fake_app):
    # msal reaches Microsoft over `requests`; a DNS/connection failure has to
    # come back as an AuthNetworkError, not a None that reads as "run rah auth".
    boom = requests.ConnectionError("Temporary failure in name resolution")
    app = fake_app(accounts=[{"username": "svc-rah@example.edu"}], silent_raises=boom)

    with pytest.raises(auth.AuthNetworkError) as exc_info:
        auth.refresh_silently(app)

    assert exc_info.value.__cause__ is boom


def test_auth_network_error_is_an_auth_error():
    # The single `except AuthError` in process/watch depends on this.
    assert issubclass(auth.AuthNetworkError, auth.AuthError)


def test_build_app_reraises_a_construction_transport_error(monkeypatch):
    # msal does tenant discovery at construction, so a real DNS outage lands
    # inside build_app -- before any refresh. It must become AuthNetworkError
    # there, or it escapes as a raw requests traceback (the actual bug this
    # replaces: the earlier fix only wrapped the refresh call).
    boom = requests.ConnectionError("Temporary failure in name resolution")

    def raise_it(*args, **kwargs):
        raise boom

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", raise_it)

    with pytest.raises(auth.AuthNetworkError) as exc_info:
        auth.build_app(SECRETS, msal.SerializableTokenCache())

    assert exc_info.value.__cause__ is boom


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


# --- TokenProvider ---


def test_get_token_returns_access_token_and_is_cached(
    install_fake_msal, fake_app, cache_with_account
):
    app = install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )
    provider = auth.TokenProvider(SECRETS, cache_with_account)

    assert provider.get_token() == "AT"
    assert provider.get_token() == "AT"

    # second call was served from the held result, not a fresh silent call
    assert len(app.silent_calls) == 1


def test_get_token_refreshes_once_the_expiry_margin_is_crossed(
    install_fake_msal, fake_app, cache_with_account
):
    app = install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    provider = auth.TokenProvider(SECRETS, cache_with_account, now=lambda: clock[0])

    assert provider.get_token() == "AT"
    assert len(app.silent_calls) == 1

    # still well inside the token's life -- no refresh needed
    clock[0] += timedelta(minutes=30)
    assert provider.get_token() == "AT"
    assert len(app.silent_calls) == 1

    # now inside the 5-minute expiry margin -- must refresh before handing it out
    clock[0] += timedelta(minutes=26)
    assert provider.get_token() == "AT"
    assert len(app.silent_calls) == 2


def test_refresh_if_stale_waits_for_the_staleness_interval(
    install_fake_msal, fake_app, cache_with_account
):
    app = install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    provider = auth.TokenProvider(SECRETS, cache_with_account, now=lambda: clock[0])

    assert provider.refresh_if_stale() is True
    assert len(app.silent_calls) == 1

    clock[0] += timedelta(minutes=30)
    assert provider.refresh_if_stale() is False
    assert len(app.silent_calls) == 1

    clock[0] += timedelta(minutes=31)
    assert provider.refresh_if_stale() is True
    assert len(app.silent_calls) == 2


def test_get_token_raises_when_refresh_silently_gives_nothing(
    install_fake_msal, fake_app, cache_with_account
):
    install_fake_msal(fake_app(accounts=[{"username": "svc-rah@example.edu"}], silent_result=None))
    provider = auth.TokenProvider(SECRETS, cache_with_account)

    with pytest.raises(auth.AuthError) as exc_info:
        provider.get_token()

    assert "rah auth" in str(exc_info.value)


def test_get_token_surfaces_a_network_failure_as_auth_network_error(
    install_fake_msal, fake_app, cache_with_account
):
    # A DNS blip mid-run must reach the watch loop as an AuthError it already
    # catches and rides out -- not as a raw requests traceback.
    install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_raises=requests.ConnectionError("name resolution failed"),
        )
    )
    provider = auth.TokenProvider(SECRETS, cache_with_account)

    with pytest.raises(auth.AuthNetworkError):
        provider.get_token()


def test_refresh_rereads_the_cache_file_and_recovers(monkeypatch, fake_app, tmp_path):
    # Starts signed out; a human fixing this overwrites the cache file on
    # disk without restarting the process, so the next refresh must pick it
    # up by re-reading rather than trusting whatever it loaded last time.
    cache_path = tmp_path / "token-cache.json"
    shutil.copy(CACHES / "empty.json", cache_path)

    bad_app = fake_app(silent_result=None)
    good_app = fake_app(
        accounts=[{"username": "svc-rah@example.edu"}],
        silent_result={"access_token": "AT2", "expires_in": 3600},
    )
    apps = iter([bad_app, good_app])
    monkeypatch.setattr(
        auth.msal, "ConfidentialClientApplication", lambda *args, **kwargs: next(apps)
    )
    provider = auth.TokenProvider(SECRETS, cache_path)

    with pytest.raises(auth.AuthError):
        provider.get_token()

    shutil.copy(CACHES / "with_account.json", cache_path)

    assert provider.get_token() == "AT2"


def test_successful_refresh_persists_the_cache_when_msal_changed_it(
    monkeypatch, fake_app, cache_with_account
):
    app = fake_app(
        accounts=[{"username": "svc-rah@example.edu"}],
        silent_result={"access_token": "AT", "expires_in": 3600},
    )

    def factory(*args, **kwargs):
        # Stand in for what a real refresh does to the cache msal was handed;
        # a rotated refresh token is what makes has_state_changed true.
        kwargs["token_cache"].has_state_changed = True
        return app

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", factory)

    before_mtime = cache_with_account.stat().st_mtime_ns
    before_text = cache_with_account.read_text(encoding="utf-8")

    provider = auth.TokenProvider(SECRETS, cache_with_account)
    assert provider.get_token() == "AT"

    assert cache_with_account.stat().st_mtime_ns != before_mtime
    assert cache_with_account.read_text(encoding="utf-8") != before_text
    assert stat.S_IMODE(cache_with_account.stat().st_mode) == 0o600


def test_invalidate_forces_the_next_get_token_to_refresh(
    install_fake_msal, fake_app, cache_with_account
):
    app = install_fake_msal(
        fake_app(
            accounts=[{"username": "svc-rah@example.edu"}],
            silent_result={"access_token": "AT", "expires_in": 3600},
        )
    )
    provider = auth.TokenProvider(SECRETS, cache_with_account)

    assert provider.get_token() == "AT"
    assert len(app.silent_calls) == 1

    # A 401 mid-life means the token was revoked with plenty of expiry left;
    # invalidate is what makes the next call refresh anyway.
    provider.invalidate()
    assert provider.get_token() == "AT"
    assert len(app.silent_calls) == 2
