# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Token acquisition and caching for the Graph API, via msal.

rah is a confidential client (the tenant won't register public ones), so
sign-in is the auth-code flow with a client secret. There's no localhost
listener: `rah auth` prints the authorization URL, the operator signs in
from any convenient browser, and pastes the redirect URL back. The redirect
URI is never served -- the browser's failed navigation still carries the
auth code in its address bar.

Everything here is shared: `rah auth` seeds the cache, `rah doctor` reports
on it, and `rah watch` will refresh from it.
"""

from __future__ import annotations

import contextlib
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msal

from redcap_alert_handler.config import Secrets

REDIRECT_URI = "http://localhost/auth"

# Never add openid/profile/offline_access here: msal requests those reserved
# scopes itself and raises if we pass them explicitly.
SCOPES = ["Mail.ReadWrite", "MailboxSettings.ReadWrite", "User.Read"]


class AuthError(Exception):
    """A sign-in attempt failed in a way the operator has to sort out.

    Wraps both msal's error-dict results and its state-check ValueError, so
    the CLI has one thing to catch. `error` and `description` are Azure's
    own words where we have them.
    """

    def __init__(self, error: str, description: str | None = None) -> None:
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}" if description else error)


def load_token_cache(path: Path) -> msal.SerializableTokenCache:
    """Return the token cache at path, or an empty one if there's no file."""
    cache = msal.SerializableTokenCache()
    with contextlib.suppress(FileNotFoundError):
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def save_token_cache(cache: msal.SerializableTokenCache, path: Path) -> None:
    """Write the cache to path, mode 0600, if msal changed it since loading."""
    if not cache.has_state_changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # os.open applies the mode only on creation; the fchmod pulls an existing
    # file back to 0600 in case something loosened it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(fd, 0o600)
        handle.write(cache.serialize())


def cached_username(cache: msal.SerializableTokenCache) -> str | None:
    """The signed-in account's username, or None if nobody has signed in.

    Reads the cache directly, so it works without secrets -- doctor names
    the account even when it can't build an app to try a refresh.
    """
    for account in cache.search(msal.TokenCache.CredentialType.ACCOUNT):
        return account.get("username")
    return None


def cache_has_account(cache: msal.SerializableTokenCache) -> bool:
    """True if anyone has ever signed in to this cache."""
    return cached_username(cache) is not None


def build_app(
    secrets: Secrets, cache: msal.SerializableTokenCache
) -> msal.ConfidentialClientApplication:
    """Construct the msal confidential-client app, with the cache attached."""
    return msal.ConfidentialClientApplication(
        secrets.client_id,
        authority=f"https://login.microsoftonline.com/{secrets.tenant_id}",
        client_credential=secrets.client_secret,
        token_cache=cache,
    )


def start_auth_flow(app: msal.ConfidentialClientApplication) -> dict:
    """Begin the auth-code flow; the result's auth_uri is what the operator opens."""
    return app.initiate_auth_code_flow(SCOPES, redirect_uri=REDIRECT_URI)


def redeem_auth_response(
    app: msal.ConfidentialClientApplication, flow: dict, redirect_url: str
) -> dict:
    """Trade the pasted redirect URL for tokens.

    msal checks the state parameter and redeems the code; a mismatch or an
    error response from Azure both come back as AuthError.

    Raises:
        AuthError: the URL carried no query string, the state check failed,
            or Azure returned an error instead of tokens.
    """
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(redirect_url).query))
    if not query:
        raise AuthError(
            "nothing to redeem",
            "that URL has no query string; paste the whole address the browser "
            "ended up at, the one with ?code=...&state=...",
        )
    try:
        result = app.acquire_token_by_auth_code_flow(flow, query)
    except ValueError as e:
        # msal raises this on a state mismatch or a malformed response
        raise AuthError("auth response rejected", str(e)) from e
    if "error" in result:
        raise AuthError(result["error"], result.get("error_description"))
    return result


def refresh_silently(app: msal.ConfidentialClientApplication) -> dict | None:
    """Try to get a token from the cache without bothering anyone.

    Returns the msal token result, or None when there's no cached account or
    the refresh didn't produce one. This is the path `watch` leans on: a
    None here means it's time for a human to run `rah auth` again.
    """
    for account in app.get_accounts():
        result = app.acquire_token_silent(SCOPES, account=account)
        if result:
            return result
    return None


def token_expiry(result: dict) -> datetime:
    """When the access token in an msal result stops working, as a UTC datetime."""
    return datetime.now(UTC) + timedelta(seconds=int(result.get("expires_in", 0)))


def describe_account(app: msal.ConfidentialClientApplication, result: dict) -> str:
    """Name the signed-in account, as best the token result reveals it."""
    claims = result.get("id_token_claims") or {}
    username = claims.get("preferred_username")
    if username:
        return username
    accounts = app.get_accounts()
    if accounts:
        return accounts[0].get("username", "unknown account")
    return "unknown account"
