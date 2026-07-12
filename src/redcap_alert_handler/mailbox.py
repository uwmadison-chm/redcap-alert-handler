# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""What a provisioned rah mailbox looks like, and how to check or build it.

One place says what folders and categories a mailbox needs: doctor checks the
live mailbox against it, `rah init` (doctor --fix) creates what's missing, and
step 7's watch loop resolves folder ids through the same layout. The folder
tree hangs off the config's base folder -- a dead-letters folder, plus a
completed/error pair under each route slug.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from redcap_alert_handler.graph import GraphClient

# The brief's four categories, each with a Graph preset color. These ride
# along on messages as advisory state, so the colors are chosen to read at a
# glance in Outlook: processing blue, errored red, expired yellow, dead
# cranberry.
RAH_CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "rah:processing": "preset7",
        "rah:errored": "preset0",
        "rah:expired": "preset3",
        "rah:dead": "preset9",
    }
)


@dataclass(frozen=True, slots=True)
class LayoutReport:
    """What resolve_layout found and (optionally) built.

    folder_ids maps every required path that exists after the call to its
    Graph folder id; missing lists the paths still absent (only when
    create=False); created lists the paths this call actually made.
    """

    folder_ids: Mapping[str, str]
    missing: tuple[str, ...]
    created: tuple[str, ...]


def required_folder_paths(slugs: Iterable[str]) -> tuple[str, ...]:
    """The folder paths a mailbox needs, dead-letters first then each slug's tree.

    Paths are relative to the base folder and use `/` for nesting, e.g.
    `consent/completed`.
    """
    paths = ["dead-letters"]
    for slug in slugs:
        paths.extend((slug, f"{slug}/completed", f"{slug}/error"))
    return tuple(paths)


def resolve_layout(
    client: GraphClient,
    base_folder_id: str,
    slugs: Iterable[str],
    create: bool = False,
) -> LayoutReport:
    """Walk the required folder tree under base_folder_id, optionally creating it.

    Each path's parent is the base folder (for a top-level path) or the
    already-resolved parent path. A missing parent makes its children missing
    too, without a doomed lookup against an id we don't have. With create=True
    the walk is idempotent: existing folders are reused, only absent ones are
    made and reported in `created`.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    created: list[str] = []

    for path in required_folder_paths(slugs):
        parent_path, _, name = path.rpartition("/")
        if parent_path == "":
            parent_id = base_folder_id
        elif parent_path in resolved:
            parent_id = resolved[parent_path]
        else:
            # Parent is missing (and wasn't created), so this child can't
            # exist either. Don't look it up under an id we never resolved.
            missing.append(path)
            continue

        found = client.find_child_folder(parent_id, name)
        if found is not None:
            resolved[path] = found["id"]
        elif create:
            made = client.create_child_folder(parent_id, name)
            resolved[path] = made["id"]
            created.append(path)
        else:
            missing.append(path)

    return LayoutReport(
        folder_ids=MappingProxyType(resolved),
        missing=tuple(missing),
        created=tuple(created),
    )


def missing_categories(client: GraphClient) -> tuple[str, ...]:
    """The RAH_CATEGORIES names not present in the mailbox, in RAH_CATEGORIES order."""
    present = {category["displayName"] for category in client.list_categories()}
    return tuple(name for name in RAH_CATEGORIES if name not in present)


def seed_categories(client: GraphClient) -> tuple[str, ...]:
    """Create the RAH_CATEGORIES the mailbox is missing; return the names created."""
    created = missing_categories(client)
    for name in created:
        client.create_category(name, RAH_CATEGORIES[name])
    return created
