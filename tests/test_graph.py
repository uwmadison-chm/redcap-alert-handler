# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

import httpx
import pytest

from redcap_alert_handler.dispatch import PROP_RETRIES_LEFT, PROP_RETRY_AT
from redcap_alert_handler.graph import GraphClient, GraphError

# --- listing and paging ---


def test_list_messages_returns_the_folder_contents(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, subject="one")
    fake_graph.add_message(fake_graph.inbox_id, subject="two")

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert [m["subject"] for m in messages] == ["one", "two"]


def test_list_messages_resolves_the_inbox_alias(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, subject="hi")

    # "inbox" is the well-known name, not the opaque folder id.
    messages = graph_client.list_messages("inbox")

    assert len(messages) == 1


def test_list_messages_ignores_other_folders(fake_graph, graph_client):
    other = fake_graph.add_folder("errors", parent_id=fake_graph.root_id)
    fake_graph.add_message(fake_graph.inbox_id, subject="mine")
    fake_graph.add_message(other["id"], subject="theirs")

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert [m["subject"] for m in messages] == ["mine"]


def test_list_messages_follows_next_link(fake_graph, graph_client):
    fake_graph.page_size = 2
    for i in range(5):
        fake_graph.add_message(fake_graph.inbox_id, subject=f"m{i}")

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert len(messages) == 5
    # 5 messages at 2 per page is three GETs against the messages endpoint.
    list_gets = [r for r in fake_graph.requests if "/messages" in r[1] and r[0] == "GET"]
    assert len(list_gets) == 3


def test_list_messages_expands_extended_properties(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, fixture="message_with_state.json")

    messages = graph_client.list_messages(
        fake_graph.inbox_id, expand_properties=(PROP_RETRIES_LEFT, PROP_RETRY_AT)
    )

    props = {p["id"]: p["value"] for p in messages[0]["singleValueExtendedProperties"]}
    assert props[PROP_RETRIES_LEFT] == "3"
    assert props[PROP_RETRY_AT] == "2026-07-09T15:00:00Z"


def test_list_messages_omits_properties_without_expand(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, fixture="message_with_state.json")

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert "singleValueExtendedProperties" not in messages[0]


# --- immutable ids ---


def test_list_messages_requests_immutable_ids(fake_graph, graph_client):
    # The id captured here gets reused for the claim and completion patches, so
    # it has to be one that survives an edit -- the immutable kind.
    fake_graph.add_message(fake_graph.inbox_id, subject="hi")

    graph_client.list_messages(fake_graph.inbox_id)

    assert 'IdType="ImmutableId"' in fake_graph.request_headers[-1]["Prefer"]


def test_get_message_requests_immutable_ids(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, subject="hi")

    graph_client.get_message(added["id"])

    assert 'IdType="ImmutableId"' in fake_graph.request_headers[-1]["Prefer"]


# --- change-key staleness (fake fidelity) ---
#
# These drive the fake directly, without a GraphClient, so a request can choose
# whether to ask for immutable ids -- the whole point being to prove the fake
# rotates a change key on write and that immutable ids dodge it.


def _raw_client(fake_graph) -> httpx.Client:
    return httpx.Client(
        transport=fake_graph.transport(),
        base_url=f"https://graph.microsoft.com/v1.0/users/{fake_graph.mailbox}/",
    )


def test_strict_change_keys_make_a_captured_id_stale_for_the_next_write(fake_graph):
    fake_graph.strict_change_keys = True
    added = fake_graph.add_message(fake_graph.inbox_id, subject="hi")

    with _raw_client(fake_graph) as client:
        # A plain list (no immutable-id Prefer) hands back a change-key-tagged id.
        listed = client.get(f"mailFolders/{fake_graph.inbox_id}/messages").json()
        captured_id = listed["value"][0]["id"]
        assert captured_id != added["id"]

        # The first write lands and rotates the change key.
        first = client.patch(f"messages/{captured_id}", json={"categories": ["rah:processing"]})
        assert first.status_code == 200

        # The captured id is now stale: the next write conflicts, the way the
        # completion patch did in the field.
        stale = client.patch(f"messages/{captured_id}", json={"categories": ["rah:errored"]})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "ErrorIrresolvableConflict"

        # A read through the stale id still works -- reads tolerate a mismatch.
        assert client.get(f"messages/{captured_id}").status_code == 200


def test_strict_change_keys_leave_an_immutable_id_usable_across_writes(fake_graph):
    fake_graph.strict_change_keys = True
    fake_graph.add_message(fake_graph.inbox_id, subject="hi")
    headers = {"Prefer": 'IdType="ImmutableId"'}

    with _raw_client(fake_graph) as client:
        listed = client.get(f"mailFolders/{fake_graph.inbox_id}/messages", headers=headers).json()
        immutable_id = listed["value"][0]["id"]

        first = client.patch(
            f"messages/{immutable_id}", json={"categories": ["rah:processing"]}, headers=headers
        )
        second = client.patch(
            f"messages/{immutable_id}", json={"categories": ["rah:errored"]}, headers=headers
        )

        assert first.status_code == 200
        assert second.status_code == 200


# --- getting one message ---


def test_get_message_returns_it(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, subject="find me")

    message = graph_client.get_message(added["id"])

    assert message["subject"] == "find me"


def test_get_message_expand_round_trips(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, fixture="message_with_state.json")

    message = graph_client.get_message(added["id"], expand_properties=(PROP_RETRIES_LEFT,))

    props = {p["id"]: p["value"] for p in message["singleValueExtendedProperties"]}
    assert props == {PROP_RETRIES_LEFT: "3"}


def test_get_missing_message_raises(fake_graph, graph_client):
    with pytest.raises(GraphError) as exc_info:
        graph_client.get_message("nope")

    assert exc_info.value.status == 404


# --- body rendering ---


def test_get_message_without_body_format_returns_the_native_body(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, fixture="message_html.json")

    message = graph_client.get_message(added["id"])

    assert message["body"] == added["body"]


def test_get_message_body_format_text_renders_an_html_native_body(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, fixture="message_html.json")

    message = graph_client.get_message(added["id"], body_format="text")

    assert message["body"] == {"contentType": "text", "content": added["textBody"]}


def test_get_message_body_format_sends_the_prefer_header(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, fixture="message_html.json")

    graph_client.get_message(added["id"], body_format="text")

    # The body-format Prefer shares the header with the always-on immutable-id
    # Prefer; both values are present, comma-joined.
    prefer = fake_graph.request_headers[-1]["Prefer"]
    assert 'outlook.body-content-type="text"' in prefer
    assert 'IdType="ImmutableId"' in prefer


def test_get_message_body_format_text_leaves_a_text_native_body_unchanged(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id)  # message.json is text-native

    message = graph_client.get_message(added["id"], body_format="text")

    assert message["body"] == added["body"]


def test_text_body_never_appears_in_a_response(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, fixture="message_html.json")

    listed = graph_client.list_messages(fake_graph.inbox_id)
    fetched = graph_client.get_message(listed[0]["id"])

    assert "textBody" not in listed[0]
    assert "textBody" not in fetched


# --- patching ---


def test_patch_sets_categories(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id)

    graph_client.patch_message(added["id"], categories=["rah:processing"])

    assert fake_graph.messages[added["id"]]["categories"] == ["rah:processing"]


def test_patch_sets_extended_properties(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id)

    graph_client.patch_message(added["id"], properties={PROP_RETRIES_LEFT: "2"})

    stored = fake_graph.messages[added["id"]]["singleValueExtendedProperties"]
    assert {"id": PROP_RETRIES_LEFT, "value": "2"} in stored


# --- moving ---


def test_move_returns_a_new_id(fake_graph, graph_client):
    added = fake_graph.add_message(fake_graph.inbox_id, subject="moving")
    destination = fake_graph.add_folder("completed", parent_id=fake_graph.root_id)

    moved = graph_client.move_message(added["id"], destination["id"])

    assert moved["id"] != added["id"]
    assert moved["parentFolderId"] == destination["id"]
    # The old id is gone; the new one resolves.
    with pytest.raises(GraphError):
        graph_client.get_message(added["id"])
    assert graph_client.get_message(moved["id"])["subject"] == "moving"


# --- folders ---


def test_get_well_known_folder(fake_graph, graph_client):
    folder = graph_client.get_well_known_folder("inbox")

    assert folder["id"] == fake_graph.inbox_id


def test_find_child_folder_hit(fake_graph, graph_client):
    fake_graph.add_folder("rah", parent_id=fake_graph.root_id)

    folder = graph_client.find_child_folder(fake_graph.root_id, "rah")

    assert folder is not None
    assert folder["displayName"] == "rah"


def test_find_child_folder_miss_returns_none(fake_graph, graph_client):
    assert graph_client.find_child_folder(fake_graph.root_id, "ghost") is None


def test_create_child_folder(fake_graph, graph_client):
    created = graph_client.create_child_folder(fake_graph.root_id, "dead-letters")

    assert created["displayName"] == "dead-letters"
    assert graph_client.find_child_folder(fake_graph.root_id, "dead-letters") is not None


# --- categories ---


def test_list_categories(fake_graph, graph_client):
    fake_graph.add_category("rah:processing", "preset0")

    categories = graph_client.list_categories()

    assert [c["displayName"] for c in categories] == ["rah:processing"]


def test_create_category(fake_graph, graph_client):
    created = graph_client.create_category("rah:errored", "preset1")

    assert created["displayName"] == "rah:errored"
    assert created["color"] == "preset1"
    assert len(fake_graph.categories) == 1


# --- retry behavior ---


def test_429_retries_after_the_retry_after_header(fake_graph, graph_client, sleep_spy):
    fake_graph.add_message(fake_graph.inbox_id, subject="eventually")
    fake_graph.enqueue_status(429, retry_after=2)

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert [m["subject"] for m in messages] == ["eventually"]
    assert sleep_spy.calls == [2]


def test_5xx_retries_with_a_default_backoff(fake_graph, graph_client, sleep_spy):
    fake_graph.add_message(fake_graph.inbox_id, subject="ok now")
    fake_graph.enqueue_status(503)

    messages = graph_client.list_messages(fake_graph.inbox_id)

    assert len(messages) == 1
    # No Retry-After, so it fell back to the default and slept once.
    assert len(sleep_spy.calls) == 1


def test_retry_exhaustion_raises(fake_graph, graph_client, sleep_spy):
    for _ in range(5):
        fake_graph.enqueue_status(500)

    with pytest.raises(GraphError) as exc_info:
        graph_client.list_messages(fake_graph.inbox_id)

    assert exc_info.value.status == 500
    # Five attempts means four waits between them.
    assert len(sleep_spy.calls) == 4


def test_non_retryable_4xx_raises_without_retrying(fake_graph, graph_client, sleep_spy):
    fake_graph.enqueue_status(403, json_body={"error": {"code": "ErrorAccessDenied"}})

    with pytest.raises(GraphError) as exc_info:
        graph_client.list_messages(fake_graph.inbox_id)

    assert exc_info.value.status == 403
    assert exc_info.value.code == "ErrorAccessDenied"
    assert sleep_spy.calls == []


def test_transport_error_becomes_graph_error(fake_graph, graph_client):
    fake_graph.enqueue_exception(httpx.ConnectError("no route to host"))

    with pytest.raises(GraphError) as exc_info:
        graph_client.list_messages(fake_graph.inbox_id)

    # A transport failure has no HTTP status to report.
    assert exc_info.value.status is None


def test_malformed_body_becomes_graph_error(fake_graph, graph_client):
    fake_graph.enqueue_status(200, content=b"{ this is not json")

    with pytest.raises(GraphError):
        graph_client.list_messages(fake_graph.inbox_id)


# --- plumbing ---


def test_base_url_targets_the_configured_mailbox_not_me(fake_graph, graph_client):
    graph_client.list_messages(fake_graph.inbox_id)

    method, url = fake_graph.requests[0]
    assert f"/v1.0/users/{fake_graph.mailbox}/" in url
    assert "/me/" not in url


def test_each_request_carries_a_bearer_token(fake_graph):
    seen = {}

    def capture(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"value": []})

    with GraphClient(
        fake_graph.mailbox,
        get_token=lambda: "shiny-token",
        transport=httpx.MockTransport(capture),
    ) as client:
        client.list_messages("inbox")

    assert seen["auth"] == "Bearer shiny-token"


def test_close_is_idempotent(fake_graph):
    client = GraphClient(
        fake_graph.mailbox, get_token=lambda: "t", transport=fake_graph.transport()
    )
    client.close()
    client.close()
