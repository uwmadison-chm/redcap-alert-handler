# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The one thin wrapper over the Graph REST API that everything else calls.

Small and boring on purpose: list/get/patch/move messages, resolve and create
folders, seed the master category list. No Graph SDK -- just httpx against the
v1.0 endpoints, so callers work with plain parsed JSON and the client stays
easy to fake (see tests/fake_graph.py).

The base URL is /users/{mailbox}, never /me: the config names the mailbox, and
doctor's whole job is to catch "authed as X, config wants Y". One thing to
catch, too -- every failure, HTTP or transport or a mangled body, comes back
as GraphError.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType

import httpx

from redcap_alert_handler.logs import get_logger

logger = get_logger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

# Ask Graph for immutable ids on every request. By default an Outlook item's id
# encodes its change key, so any write rotates the id -- and a message id we
# captured at list time goes stale the moment the claim patch modifies it, which
# breaks the later completion patch with an ErrorIrresolvableConflict. Immutable
# ids stay put across edits and folder moves, so one captured id stays usable for
# the whole dispatch. See learn.microsoft.com/graph/outlook-immutable-id.
_IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'

# How long to wait before a retry when Graph doesn't tell us (no Retry-After,
# or one we can't read as an integer). Deliberately short; the caps below stop
# a wedged endpoint from spinning forever.
_DEFAULT_BACKOFF_SECONDS = 1
_MAX_ATTEMPTS = 5


class GraphError(Exception):
    """A Graph request failed in a way the caller can't paper over.

    Wraps HTTP error responses, httpx transport failures, and non-JSON bodies
    alike, so callers catch one thing. `status` is the HTTP status where there
    was one (None for a transport failure); `code` is Graph's own error code
    when the body carried one.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(message)


class GraphClient:
    """A session against one mailbox's Graph endpoints.

    Owns a single httpx.Client; use it as a context manager or call close().
    The transport and sleep are injectable so tests run against a fake Graph
    with no network and no real waiting.
    """

    def __init__(
        self,
        mailbox: str,
        get_token: Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], object] = time.sleep,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._base = f"{GRAPH_ROOT}/users/{mailbox}"
        self._get_token = get_token
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._client = httpx.Client(transport=transport, timeout=httpx.Timeout(30.0))

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool. Safe to call twice."""
        self._client.close()

    # -- messages ---------------------------------------------------------

    def list_messages(self, folder_id: str, expand_properties: Sequence[str] = ()) -> list[dict]:
        """Every message in a folder, following @odata.nextLink to the end."""
        params: dict[str, str] | None = {}
        if expand_properties:
            params = {"$expand": _expand_clause(expand_properties)}
        url = f"{self._base}/mailFolders/{folder_id}/messages"

        messages: list[dict] = []
        while url is not None:
            data = self._request("GET", url, params=params)
            messages.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            # nextLink already carries the query, so don't re-send params.
            params = None
        return messages

    def get_message(
        self,
        message_id: str,
        expand_properties: Sequence[str] = (),
        *,
        body_format: str | None = None,
    ) -> dict:
        """One message, optionally with its named extended properties expanded.

        Graph stores one native body per message, usually HTML. Passing
        body_format="text" asks for the text rendering instead, via the
        Prefer header rather than a query parameter.
        """
        params = None
        if expand_properties:
            params = {"$expand": _expand_clause(expand_properties)}
        headers = None
        if body_format is not None:
            headers = {"Prefer": f'outlook.body-content-type="{body_format}"'}
        return self._request(
            "GET", f"{self._base}/messages/{message_id}", params=params, extra_headers=headers
        )

    def patch_message(
        self,
        message_id: str,
        properties: Mapping[str, str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> dict:
        """Update a message's extended properties and/or categories in one PATCH."""
        body: dict = {}
        if categories is not None:
            body["categories"] = list(categories)
        if properties is not None:
            body["singleValueExtendedProperties"] = [
                {"id": pid, "value": value} for pid, value in properties.items()
            ]
        return self._request("PATCH", f"{self._base}/messages/{message_id}", json_body=body)

    def move_message(self, message_id: str, destination_folder_id: str) -> dict:
        """Move a message; the returned copy carries its new, post-move id."""
        return self._request(
            "POST",
            f"{self._base}/messages/{message_id}/move",
            json_body={"destinationId": destination_folder_id},
        )

    # -- folders ----------------------------------------------------------

    def get_well_known_folder(self, name: str) -> dict:
        """Resolve a well-known folder (e.g. "inbox", "msgFolderRoot")."""
        return self._request("GET", f"{self._base}/mailFolders/{name}")

    def find_child_folder(self, parent_id: str, display_name: str) -> dict | None:
        """The child folder with this display name, or None if there isn't one."""
        params = {"$filter": f"displayName eq '{_escape_odata(display_name)}'"}
        data = self._request(
            "GET", f"{self._base}/mailFolders/{parent_id}/childFolders", params=params
        )
        for folder in data.get("value", []):
            return folder
        return None

    def create_child_folder(self, parent_id: str, display_name: str) -> dict:
        """Create a child folder under parent_id and return it."""
        return self._request(
            "POST",
            f"{self._base}/mailFolders/{parent_id}/childFolders",
            json_body={"displayName": display_name},
        )

    # -- categories -------------------------------------------------------

    def list_categories(self) -> list[dict]:
        """The mailbox's master category list."""
        data = self._request("GET", f"{self._base}/outlook/masterCategories")
        return data.get("value", [])

    def create_category(self, name: str, color: str) -> dict:
        """Add one category to the master list (color is a Graph preset name)."""
        return self._request(
            "POST",
            f"{self._base}/outlook/masterCategories",
            json_body={"displayName": name, "color": color},
        )

    # -- the single request path -----------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: object = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        # Immutable ids ride on Prefer, the same header get_message uses for the
        # body format; both values go in one comma-joined Prefer, so neither
        # clobbers the other.
        prefer = [_IMMUTABLE_ID_PREFER]
        if extra_headers:
            for key, value in extra_headers.items():
                if key.casefold() == "prefer":
                    prefer.append(value)
                else:
                    headers[key] = value
        headers["Prefer"] = ", ".join(prefer)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as e:
                raise GraphError(f"couldn't reach the Graph API: {e}") from e

            status = response.status_code
            if _is_retryable(status) and attempt < self._max_attempts:
                delay = self._retry_after(response)
                logger.debug("graph %s on attempt %d, retrying in %ss", status, attempt, delay)
                self._sleep(delay)
                continue
            if status >= 400:
                raise _error_from_response(response)
            try:
                return response.json()
            except json.JSONDecodeError as e:
                raise GraphError(
                    f"Graph returned a body that wasn't JSON (HTTP {status})", status=status
                ) from e
        # The loop always returns or raises; this keeps type checkers happy.
        raise AssertionError("unreachable")

    def _retry_after(self, response: httpx.Response) -> float:
        header = response.headers.get("Retry-After")
        if header is not None:
            try:
                return int(header)
            except ValueError:
                # A date-form Retry-After is legal but rare from Graph; the
                # default keeps us moving rather than parsing HTTP dates.
                pass
        return _DEFAULT_BACKOFF_SECONDS


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _error_from_response(response: httpx.Response) -> GraphError:
    status = response.status_code
    code = None
    detail = None
    try:
        error = response.json().get("error", {})
        code = error.get("code")
        detail = error.get("message")
    except json.JSONDecodeError, AttributeError:
        pass
    described = detail or code or f"HTTP {status}"
    return GraphError(f"Graph request failed: {described}", status=status, code=code)


def _expand_clause(property_ids: Sequence[str]) -> str:
    clauses = " or ".join(f"id eq '{_escape_odata(pid)}'" for pid in property_ids)
    return f"singleValueExtendedProperties($filter={clauses})"


def _escape_odata(value: str) -> str:
    # OData escapes a single quote by doubling it.
    return value.replace("'", "''")
