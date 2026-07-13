# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The dispatch core: the brief's state model as pure functions.

`decide` reads a message's parsed state and its route and says what happens
next; the transition writers turn each outcome into mailbox-write
instructions -- extended properties, categories, a folder move. Nothing here
calls Graph. Step 7's `rah process` reads a message, calls these functions,
and executes the instructions against a GraphClient. Keeping I/O out is what
lets the whole state machine be tested with plain values, no fake mailbox
required.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from redcap_alert_handler.config import RouteConfig

# Permanent, once anything is in flight: every property written under this
# namespace depends on it, and changing it orphans whatever state is
# sitting on live messages. Don't touch it.
_NAMESPACE_GUID = "a52b54e5-d9b2-410a-aa59-bdf5e3d571d6"

PROP_RETRIES_LEFT = f"Integer {{{_NAMESPACE_GUID}}} Name rah-retries-left"
PROP_RETRY_AT = f"SystemTime {{{_NAMESPACE_GUID}}} Name rah-retry-at"
PROP_STATE = f"String {{{_NAMESPACE_GUID}}} Name rah-state"

MESSAGE_PROPERTY_IDS = (PROP_RETRIES_LEFT, PROP_RETRY_AT, PROP_STATE)

# Advisory only -- rah-state is what decide() trusts. The names live here,
# not in mailbox, because this module can't import mailbox (that would drag
# in graph/httpx); mailbox imports these to map each one to a color.
CATEGORY_PROCESSING = "rah:processing"
CATEGORY_ERRORED = "rah:errored"
CATEGORY_EXPIRED = "rah:expired"
CATEGORY_DEAD = "rah:dead"


class TerminalState(StrEnum):
    """The four rah-state values a message can end its life in."""

    COMPLETED = "completed"
    ERRORED = "errored"
    EXPIRED = "expired"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class MessageState:
    """A message's dispatch-relevant state, read from its Graph properties.

    A brand-new message -- one rah has never touched -- has None for the
    last three fields.
    """

    internet_message_id: str
    subject: str
    received_at: datetime
    retries_left: int | None
    retry_at: datetime | None
    terminal_state: str | None


def format_datetime(dt: datetime) -> str:
    """Render an aware datetime as the string form Graph property values use.

    Extended property values travel as plain strings; a small shared helper
    keeps writers and parse_message_state agreeing on the exact form.
    """
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    # fromisoformat has accepted a bare "Z" suffix since 3.11; normalizing to
    # UTC here (rather than trusting the offset) keeps every MessageState
    # field in the same timezone, so callers never have to think about it.
    return datetime.fromisoformat(value).astimezone(UTC)


def parse_message_state(raw: dict) -> MessageState:
    """Read a Graph message dict, fetched with $expand, into a MessageState.

    Property ids are matched case-insensitively -- Graph normalizes their
    casing unpredictably, and a strict match would silently treat a message
    rah has already claimed as brand-new.
    """
    properties = {
        prop["id"].casefold(): prop["value"]
        for prop in raw.get("singleValueExtendedProperties", [])
    }

    retries_raw = properties.get(PROP_RETRIES_LEFT.casefold())
    retry_at_raw = properties.get(PROP_RETRY_AT.casefold())
    state_raw = properties.get(PROP_STATE.casefold())

    retries_left = int(retries_raw) if retries_raw is not None else None
    retry_at = _parse_datetime(retry_at_raw) if retry_at_raw is not None else None

    # A value outside the four known states is corrupt or foreign, not
    # something to raise over. Treating it as in-flight is safe: handlers
    # claim a message before doing anything with side effects, so a
    # re-dispatch here costs at most one wasted attempt.
    terminal_state = state_raw if state_raw in set(TerminalState) else None

    return MessageState(
        internet_message_id=raw["internetMessageId"],
        subject=raw["subject"],
        received_at=_parse_datetime(raw["receivedDateTime"]),
        retries_left=retries_left,
        retry_at=retry_at,
        terminal_state=terminal_state,
    )


def match_slug(subject: str, slugs: Iterable[str]) -> str | None:
    """The route slug this subject belongs to, or None if nothing claims it.

    REDCap alert subjects look like "slug|payload"; the match is exact and
    case-sensitive against the part before the first "|", so at most one
    route can ever claim a message.
    """
    candidate = subject.split("|", 1)[0].strip()
    return candidate if candidate in slugs else None


# --- decisions --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dispatch:
    """Hand the message to its route's handler now."""


@dataclass(frozen=True, slots=True)
class WaitForRetry:
    """Not due yet; leave it alone until `until`."""

    until: datetime


@dataclass(frozen=True, slots=True)
class Expire:
    """Too old to dispatch, regardless of what retry state says."""


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """Send it to dead-letters. `reason` is "unroutable" or "exhausted"."""

    reason: str


@dataclass(frozen=True, slots=True)
class Finish:
    """A terminal marker is already written; only the move is left to do.

    This is the crashed-run case: rah patched rah-state and then died
    before the move. `terminal_state` says which move to finish.
    """

    terminal_state: str


type Decision = Dispatch | WaitForRetry | Expire | DeadLetter | Finish


def decide(state: MessageState, route: RouteConfig | None, now: datetime) -> Decision:
    """What to do next with a message. First matching rule wins.

    The order encodes priority, not just a set of exclusive conditions:
    - unroutable outranks everything else, including a terminal marker,
      because config can change out from under in-flight mail and the
      slug's own folders may no longer exist to move it into.
    - a terminal marker outranks aging or retry checks: the handler already
      ran, so there's nothing left to decide except finishing the move.
    - age outranks retry and exhaustion checks: expired mail never
      dispatches, even if a retry happens to be due right now.
    - exhaustion outranks a pending retry: retries_left reaching zero is
      final, so a stray future retry_at left over from an earlier decision
      doesn't get a chance to fire.
    """
    if route is None:
        return DeadLetter("unroutable")
    if state.terminal_state is not None:
        return Finish(state.terminal_state)
    if now - state.received_at > route.max_age:
        return Expire()
    if state.retries_left == 0:
        return DeadLetter("exhausted")
    if state.retry_at is not None and state.retry_at > now:
        return WaitForRetry(state.retry_at)
    return Dispatch()


# --- transition writers -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Patch:
    """One patch_message call: properties and categories in a single PATCH.

    Riding together in one request is what satisfies the brief's rule that
    the authoritative property write must land before the advisory
    category does -- there's no second request in which the category could
    arrive first.
    """

    properties: Mapping[str, str] | None
    categories: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Move:
    """Move the message to `path`, relative to the base folder.

    Matches the paths mailbox.required_folder_paths produces; step 7
    resolves this to a folder id before calling GraphClient.move_message.
    """

    path: str


def claim(retries_left: int | None, max_retries: int) -> tuple[Patch]:
    """Burn one attempt and mark the message as being worked.

    Decrementing retries_left before dispatch, not after, is the poison
    guard: if a handler wedges the process badly enough to take it down,
    the attempt is already spent, so restarting rah doesn't hand the same
    poisoned message the same unlimited number of tries. First claim writes
    max_retries -- the scheme counts attempts remaining, so max_retries = 3
    means one initial attempt plus 3 retries, four tries total.
    """
    remaining = max_retries if retries_left is None else retries_left - 1
    return (
        Patch(
            properties={PROP_RETRIES_LEFT: str(remaining)},
            categories=(CATEGORY_PROCESSING,),
        ),
    )


def completed(slug: str) -> tuple[Patch, Move]:
    """The handler finished cleanly; mark it done and file it."""
    return (
        Patch(properties={PROP_STATE: TerminalState.COMPLETED}, categories=()),
        Move(f"{slug}/completed"),
    )


def transient_failure(now: datetime, retry_backoff: timedelta) -> tuple[Patch]:
    """Set the next retry time. Backoff is fixed -- no curve, no jitter."""
    return (
        Patch(
            properties={PROP_RETRY_AT: format_datetime(now + retry_backoff)},
            categories=(),
        ),
    )


def permanent_failure(slug: str) -> tuple[Patch, Move]:
    """The handler raised PermanentError; mark it errored and file it."""
    return (
        Patch(properties={PROP_STATE: TerminalState.ERRORED}, categories=(CATEGORY_ERRORED,)),
        Move(f"{slug}/error"),
    )


def expire() -> tuple[Patch, Move]:
    """Too old to dispatch; mark it expired and file it in dead-letters."""
    return (
        Patch(properties={PROP_STATE: TerminalState.EXPIRED}, categories=(CATEGORY_EXPIRED,)),
        Move("dead-letters"),
    )


def dead_letter() -> tuple[Patch, Move]:
    """Unroutable or out of retries; mark it dead and file it in dead-letters."""
    return (
        Patch(properties={PROP_STATE: TerminalState.DEAD}, categories=(CATEGORY_DEAD,)),
        Move("dead-letters"),
    )


def finish(terminal_state: str, slug: str) -> tuple[Move]:
    """Finish a move a crashed run wrote the marker for but never made.

    The property write already happened, so this is move-only -- to
    whatever destination that state's own writer would have used.
    """
    if terminal_state == TerminalState.COMPLETED:
        return (Move(f"{slug}/completed"),)
    if terminal_state == TerminalState.ERRORED:
        return (Move(f"{slug}/error"),)
    return (Move("dead-letters"),)
