# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""A stateful in-memory Graph mailbox behind an httpx.MockTransport.

Every later integration test rides on this, so it aims to behave like the
real Graph on the handful of endpoints rah touches: list/get/patch/move
messages, resolve well-known folders, find/create child folders, seed the
master category list. State lives in plain dicts; `transport()` hands back a
MockTransport that routes real httpx requests into that state.

Two bits of realism earn their keep in later crash-recovery tests:
- a move assigns the message a brand-new id, the way real Graph does, because
  the Graph id changes when a message crosses folders;
- singleValueExtendedProperties are only returned when the caller `$expand`s
  them, and only the ids it asked for.

Canned message bodies are files under data/graph/; `add_message` clones one
and applies overrides. Error injection (`enqueue_status`, `enqueue_exception`)
serves forced responses ahead of normal routing so retry and failure paths
get exercised without a real server.
"""

from __future__ import annotations

import itertools
import json
import re
import urllib.parse
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

DATA_GRAPH = Path(__file__).parent / "data" / "graph"

_EXPAND_ID_RE = re.compile(r"id eq '([^']*)'")
_DISPLAY_NAME_RE = re.compile(r"displayName eq '((?:[^']|'')*)'")


class FakeGraph:
    """An in-memory Office 365 mailbox that answers Graph v1.0 requests."""

    def __init__(self, mailbox: str = "svc-rah@example.edu", page_size: int = 50) -> None:
        self.mailbox = mailbox
        # Small enough that a test can drop it to 2 and force a nextLink.
        self.page_size = page_size
        self.folders: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.categories: list[dict] = []
        # Every request lands here as (method, url) so tests can assert paging
        # actually followed a nextLink, etc.
        self.requests: list[tuple[str, str]] = []

        self._well_known: dict[str, str] = {}
        self._forced: deque[httpx.Response | Exception] = deque()
        self._ids = itertools.count(1)

        root = self.add_folder("Top of Information Store", well_known="msgFolderRoot")
        inbox = self.add_folder("Inbox", parent_id=root["id"], well_known="inbox")
        self.root_id = root["id"]
        self.inbox_id = inbox["id"]

    # -- transport --------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        """A MockTransport wired to this mailbox; hand it to a GraphClient."""
        return httpx.MockTransport(self._handle)

    # -- seeding state ----------------------------------------------------

    def add_folder(
        self, display_name: str, parent_id: str | None = None, well_known: str | None = None
    ) -> dict:
        """Create a mail folder and return its resource dict."""
        folder_id = self._next_id("folder")
        folder = {"id": folder_id, "displayName": display_name, "parentFolderId": parent_id}
        self.folders[folder_id] = folder
        if well_known is not None:
            self._well_known[well_known] = folder_id
        return folder

    def add_message(
        self,
        folder_id: str,
        *,
        fixture: str = "message.json",
        message_id: str | None = None,
        subject: str | None = None,
        internet_message_id: str | None = None,
        categories: Sequence[str] | None = None,
        properties: Mapping[str, str] | None = None,
    ) -> dict:
        """Clone a fixture message into a folder, applying any overrides."""
        message = json.loads((DATA_GRAPH / fixture).read_text())
        message["id"] = message_id or self._next_id("msg")
        message["parentFolderId"] = folder_id
        if subject is not None:
            message["subject"] = subject
        if internet_message_id is not None:
            message["internetMessageId"] = internet_message_id
        if categories is not None:
            message["categories"] = list(categories)
        if properties is not None:
            message["singleValueExtendedProperties"] = [
                {"id": pid, "value": value} for pid, value in properties.items()
            ]
        self.messages[message["id"]] = message
        return message

    def add_category(self, display_name: str, color: str = "preset0") -> dict:
        cat = {"id": self._next_id("cat"), "displayName": display_name, "color": color}
        self.categories.append(cat)
        return cat

    # -- error injection --------------------------------------------------

    def enqueue_status(
        self,
        status_code: int,
        *,
        retry_after: int | None = None,
        json_body: object = None,
        content: bytes | None = None,
    ) -> None:
        """Serve one forced response ahead of normal routing.

        `retry_after` sets the header the retry path reads; `content` (raw
        bytes) is the way to hand back a malformed body.
        """
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        if content is not None:
            self._forced.append(httpx.Response(status_code, headers=headers, content=content))
        else:
            self._forced.append(httpx.Response(status_code, headers=headers, json=json_body or {}))

    def enqueue_exception(self, exc: Exception) -> None:
        """Make the next request raise -- stands in for a transport failure."""
        self._forced.append(exc)

    # -- routing ----------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if self._forced:
            forced = self._forced.popleft()
            if isinstance(forced, Exception):
                raise forced
            return forced
        return self._route(request)

    def _route(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/v1.0/users/{self.mailbox}/"
        path = urllib.parse.unquote(request.url.path)
        rest = path[len(prefix) :] if path.startswith(prefix) else path
        segments = rest.split("/")
        method = request.method
        params = request.url.params

        match segments:
            case ["outlook", "masterCategories"]:
                if method == "GET":
                    return _json(200, {"value": self.categories})
                if method == "POST":
                    return self._create_category(request)
            case ["mailFolders", folder_ref]:
                if method == "GET":
                    return self._get_folder(folder_ref)
            case ["mailFolders", folder_ref, "messages"]:
                if method == "GET":
                    return self._list_messages(folder_ref, request)
            case ["mailFolders", folder_ref, "childFolders"]:
                if method == "GET":
                    return self._list_child_folders(folder_ref, params)
                if method == "POST":
                    return self._create_child_folder(folder_ref, request)
            case ["messages", message_id]:
                if method == "GET":
                    return self._get_message(message_id, params)
                if method == "PATCH":
                    return self._patch_message(message_id, request)
            case ["messages", message_id, "move"]:
                if method == "POST":
                    return self._move_message(message_id, request)

        return _error(404, "ResourceNotFound", f"no fake route for {method} {rest}")

    # -- message operations ----------------------------------------------

    def _list_messages(self, folder_ref: str, request: httpx.Request) -> httpx.Response:
        folder_id = self._resolve_folder(folder_ref)
        params = request.url.params
        # Insertion order is deterministic, which keeps paging stable.
        matches = [m for m in self.messages.values() if m["parentFolderId"] == folder_id]
        skip = int(params.get("$skip", 0))
        expand_ids = _parse_expand(params.get("$expand"))
        page = matches[skip : skip + self.page_size]
        body: dict = {"value": [_project(m, expand_ids) for m in page]}
        if skip + self.page_size < len(matches):
            next_url = request.url.copy_set_param("$skip", str(skip + self.page_size))
            body["@odata.nextLink"] = str(next_url)
        return _json(200, body)

    def _get_message(self, message_id: str, params: httpx.QueryParams) -> httpx.Response:
        message = self.messages.get(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", f"no message {message_id}")
        return _json(200, _project(message, _parse_expand(params.get("$expand"))))

    def _patch_message(self, message_id: str, request: httpx.Request) -> httpx.Response:
        message = self.messages.get(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", f"no message {message_id}")
        body = json.loads(request.content)
        if "categories" in body:
            message["categories"] = list(body["categories"])
        if "singleValueExtendedProperties" in body:
            by_id = {p["id"]: p for p in message.get("singleValueExtendedProperties", [])}
            for prop in body["singleValueExtendedProperties"]:
                by_id[prop["id"]] = {"id": prop["id"], "value": prop["value"]}
            message["singleValueExtendedProperties"] = list(by_id.values())
        return _json(200, _project(message, None))

    def _move_message(self, message_id: str, request: httpx.Request) -> httpx.Response:
        message = self.messages.get(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", f"no message {message_id}")
        destination = self._resolve_folder(json.loads(request.content)["destinationId"])
        moved = dict(message)
        del self.messages[message_id]
        # New id on move: the real Graph reissues one when a message changes
        # folders, and crash-recovery logic downstream relies on that.
        moved["id"] = self._next_id("msg")
        moved["parentFolderId"] = destination
        self.messages[moved["id"]] = moved
        return _json(201, _project(moved, None))

    # -- folder operations ------------------------------------------------

    def _get_folder(self, folder_ref: str) -> httpx.Response:
        folder = self.folders.get(self._resolve_folder(folder_ref))
        if folder is None:
            return _error(404, "ErrorItemNotFound", f"no folder {folder_ref}")
        return _json(200, self._project_folder(folder))

    def _list_child_folders(self, folder_ref: str, params: httpx.QueryParams) -> httpx.Response:
        parent_id = self._resolve_folder(folder_ref)
        children = [f for f in self.folders.values() if f["parentFolderId"] == parent_id]
        wanted = _parse_display_name(params.get("$filter"))
        if wanted is not None:
            children = [f for f in children if f["displayName"] == wanted]
        return _json(200, {"value": [self._project_folder(f) for f in children]})

    def _create_child_folder(self, folder_ref: str, request: httpx.Request) -> httpx.Response:
        parent_id = self._resolve_folder(folder_ref)
        display_name = json.loads(request.content)["displayName"]
        created = self.add_folder(display_name, parent_id=parent_id)
        return _json(201, self._project_folder(created))

    def _create_category(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return _json(201, self.add_category(body["displayName"], body.get("color", "preset0")))

    # -- helpers ----------------------------------------------------------

    def _project_folder(self, folder: dict) -> dict:
        """A folder as Graph would return it, with totalItemCount computed live.

        Real Graph tracks the count on the folder resource itself; the fake
        keeps only messages and folders as state, so it counts at serve time
        rather than maintaining a second, driftable number.
        """
        count = sum(1 for m in self.messages.values() if m["parentFolderId"] == folder["id"])
        return {**folder, "totalItemCount": count}

    def _resolve_folder(self, folder_ref: str) -> str:
        # A request may name a folder by its well-known alias (inbox,
        # msgFolderRoot) or by its opaque id; normalize both to an id.
        return self._well_known.get(folder_ref, folder_ref)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"


def _project(message: dict, expand_ids: set[str] | None) -> dict:
    """A message as Graph would return it: extended props only when expanded."""
    projected = {k: v for k, v in message.items() if k != "singleValueExtendedProperties"}
    if expand_ids is not None:
        props = message.get("singleValueExtendedProperties", [])
        projected["singleValueExtendedProperties"] = [p for p in props if p["id"] in expand_ids]
    return projected


def _parse_expand(expand: str | None) -> set[str] | None:
    """The property ids named in a singleValueExtendedProperties $expand."""
    if expand is None:
        return None
    return set(_EXPAND_ID_RE.findall(expand))


def _parse_display_name(filter_clause: str | None) -> str | None:
    if filter_clause is None:
        return None
    match = _DISPLAY_NAME_RE.search(filter_clause)
    if match is None:
        return None
    return match.group(1).replace("''", "'")


def _json(status_code: int, body: object) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def _error(status_code: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status_code, json={"error": {"code": code, "message": message}})
