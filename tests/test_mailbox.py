# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from redcap_alert_handler.mailbox import (
    RAH_CATEGORIES,
    missing_categories,
    required_folder_paths,
    resolve_layout,
    seed_categories,
)

# --- required_folder_paths ---


def test_required_folder_paths_orders_dead_letters_first_then_slugs_in_order():
    paths = required_folder_paths(["consent", "push_alert"])

    assert paths == (
        "dead-letters",
        "consent",
        "consent/completed",
        "consent/error",
        "push_alert",
        "push_alert/completed",
        "push_alert/error",
    )


def test_required_folder_paths_one_slug():
    assert required_folder_paths(["simple"]) == (
        "dead-letters",
        "simple",
        "simple/completed",
        "simple/error",
    )


# --- resolve_layout ---


def test_fresh_mailbox_reports_everything_missing(fake_graph, graph_client):
    base = fake_graph.add_folder("rah", parent_id=fake_graph.root_id)

    report = resolve_layout(graph_client, base["id"], ["consent"])

    assert report.missing == required_folder_paths(["consent"])
    assert report.folder_ids == {}
    assert report.created == ()


def test_create_builds_the_exact_tree(fake_graph, graph_client):
    base = fake_graph.add_folder("rah", parent_id=fake_graph.root_id)

    report = resolve_layout(graph_client, base["id"], ["consent", "push_alert"], create=True)

    assert report.missing == ()
    assert report.created == required_folder_paths(["consent", "push_alert"])
    assert set(report.folder_ids) == set(required_folder_paths(["consent", "push_alert"]))

    # Confirm the tree fake_graph actually holds matches path structure --
    # not just that resolve_layout claims success.
    dead_letters = _child(fake_graph, base["id"], "dead-letters")
    consent = _child(fake_graph, base["id"], "consent")
    push_alert = _child(fake_graph, base["id"], "push_alert")
    assert dead_letters is not None
    assert _child(fake_graph, consent["id"], "completed") is not None
    assert _child(fake_graph, consent["id"], "error") is not None
    assert _child(fake_graph, push_alert["id"], "completed") is not None
    assert _child(fake_graph, push_alert["id"], "error") is not None

    assert report.folder_ids["dead-letters"] == dead_letters["id"]
    assert report.folder_ids["consent"] == consent["id"]


def test_create_twice_is_idempotent(fake_graph, graph_client):
    base = fake_graph.add_folder("rah", parent_id=fake_graph.root_id)
    resolve_layout(graph_client, base["id"], ["consent"], create=True)

    second = resolve_layout(graph_client, base["id"], ["consent"], create=True)

    assert second.created == ()
    assert second.missing == ()
    assert set(second.folder_ids) == set(required_folder_paths(["consent"]))


def _child(fake_graph, parent_id, display_name):
    for folder in fake_graph.folders.values():
        if folder["parentFolderId"] == parent_id and folder["displayName"] == display_name:
            return folder
    return None


# --- categories ---


def test_missing_categories_on_a_fresh_mailbox_is_all_four(graph_client):
    assert set(missing_categories(graph_client)) == set(RAH_CATEGORIES)


def test_seed_categories_creates_the_absent_ones(fake_graph, graph_client):
    created = seed_categories(graph_client)

    assert set(created) == set(RAH_CATEGORIES)
    assert {c["displayName"] for c in fake_graph.categories} == set(RAH_CATEGORIES)


def test_seed_categories_twice_is_idempotent(graph_client):
    seed_categories(graph_client)

    second = seed_categories(graph_client)

    assert second == ()
