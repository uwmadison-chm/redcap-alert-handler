# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from datetime import UTC, datetime, timedelta

import pytest

from redcap_alert_handler.config import RouteConfig
from redcap_alert_handler.dispatch import (
    CATEGORY_DEAD,
    CATEGORY_ERRORED,
    CATEGORY_EXPIRED,
    CATEGORY_PROCESSING,
    MESSAGE_PROPERTY_IDS,
    PROP_RETRIES_LEFT,
    PROP_RETRY_AT,
    PROP_STATE,
    DeadLetter,
    Dispatch,
    Expire,
    Finish,
    MessageState,
    Move,
    Patch,
    WaitForRetry,
    claim,
    completed,
    dead_letter,
    decide,
    expire,
    finish,
    format_datetime,
    match_slug,
    parse_message_state,
    permanent_failure,
    transient_failure,
)

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
MAX_AGE = timedelta(days=1)


def _route(slug="consent", max_age=MAX_AGE):
    return RouteConfig(slug=slug, handler="pkg:handler", max_age=max_age, dry_run=False, extra={})


def _state(**overrides):
    fields = {
        "internet_message_id": "<abc123@example.edu>",
        "subject": "consent|payload",
        "received_at": NOW,
        "retries_left": None,
        "retry_at": None,
        "terminal_state": None,
    }
    fields.update(overrides)
    return MessageState(**fields)


# --- MESSAGE_PROPERTY_IDS --------------------------------------------------


def test_message_property_ids_has_exactly_the_three_properties():
    assert MESSAGE_PROPERTY_IDS == (PROP_RETRIES_LEFT, PROP_RETRY_AT, PROP_STATE)


# --- match_slug --------------------------------------------------------

SLUGS = {"consent", "push_alert"}

SLUG_CASES = [
    pytest.param("consent|payload", "consent", id="slug-and-payload"),
    pytest.param("consent", "consent", id="bare-slug-no-pipe"),
    pytest.param("  consent  |payload", "consent", id="whitespace-around-slug"),
    pytest.param("consent followup", None, id="near-miss-no-pipe-no-match"),
    pytest.param("", None, id="empty-subject"),
    pytest.param("|payload", None, id="empty-slug-before-pipe"),
    pytest.param("Consent|payload", None, id="case-sensitive-no-match"),
    pytest.param("unknown|payload", None, id="slug-not-in-config"),
]


@pytest.mark.parametrize(("subject", "expected"), SLUG_CASES)
def test_match_slug(subject, expected):
    assert match_slug(subject, SLUGS) == expected


# --- parse_message_state ----------------------------------------------

FULL_PAYLOAD = {
    "internetMessageId": "<abc123@example.edu>",
    "subject": "consent|payload",
    "receivedDateTime": "2026-07-13T12:00:00Z",
    "singleValueExtendedProperties": [
        {"id": PROP_RETRIES_LEFT, "value": "2"},
        {"id": PROP_RETRY_AT, "value": "2026-07-13T13:00:00Z"},
        {"id": PROP_STATE, "value": "errored"},
    ],
}


def test_parse_message_state_full_payload_round_trips():
    assert parse_message_state(FULL_PAYLOAD) == MessageState(
        internet_message_id="<abc123@example.edu>",
        subject="consent|payload",
        received_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        retries_left=2,
        retry_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        terminal_state="errored",
    )


def test_parse_message_state_missing_extended_properties_is_all_none():
    raw = {
        "internetMessageId": "<abc123@example.edu>",
        "subject": "consent|payload",
        "receivedDateTime": "2026-07-13T12:00:00Z",
    }

    state = parse_message_state(raw)

    assert state.retries_left is None
    assert state.retry_at is None
    assert state.terminal_state is None


def test_parse_message_state_property_ids_match_case_insensitively():
    raw = {
        **FULL_PAYLOAD,
        "singleValueExtendedProperties": [{"id": PROP_STATE.upper(), "value": "completed"}],
    }

    assert parse_message_state(raw).terminal_state == "completed"


def test_parse_message_state_unknown_rah_state_value_is_none():
    raw = {
        **FULL_PAYLOAD,
        "singleValueExtendedProperties": [{"id": PROP_STATE, "value": "garbage"}],
    }

    assert parse_message_state(raw).terminal_state is None


def test_parse_message_state_trailing_z_is_aware_utc():
    raw = {
        "internetMessageId": "<abc123@example.edu>",
        "subject": "consent|payload",
        "receivedDateTime": "2026-07-13T12:00:00Z",
    }

    received_at = parse_message_state(raw).received_at

    assert received_at == datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    assert received_at.tzinfo is not None


def test_format_datetime_pins_the_exact_string_form():
    dt = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    assert format_datetime(dt) == "2026-07-13T12:00:00Z"


def test_format_datetime_round_trips_through_the_parser():
    dt = datetime(2026, 7, 13, 12, 34, 56, tzinfo=UTC)
    raw = {
        "internetMessageId": "<abc123@example.edu>",
        "subject": "consent|payload",
        "receivedDateTime": "2026-07-13T00:00:00Z",
        "singleValueExtendedProperties": [{"id": PROP_RETRY_AT, "value": format_datetime(dt)}],
    }

    assert parse_message_state(raw).retry_at == dt


# --- decide --------------------------------------------------------------

DECISION_CASES = [
    pytest.param(_state(), _route(), Dispatch(), id="new-message-dispatches"),
    pytest.param(
        _state(retry_at=NOW - timedelta(seconds=1)),
        _route(),
        Dispatch(),
        id="retry-due-dispatches",
    ),
    pytest.param(
        _state(retry_at=NOW + timedelta(hours=1)),
        _route(),
        WaitForRetry(NOW + timedelta(hours=1)),
        id="retry-not-yet-due-waits",
    ),
    pytest.param(
        _state(retry_at=NOW),
        _route(),
        Dispatch(),
        id="retry-exactly-now-is-due",
    ),
    pytest.param(
        _state(received_at=NOW - MAX_AGE - timedelta(seconds=1)),
        _route(),
        Expire(),
        id="expired-new-message",
    ),
    pytest.param(
        _state(
            received_at=NOW - MAX_AGE - timedelta(seconds=1),
            retry_at=NOW + timedelta(hours=1),
        ),
        _route(),
        Expire(),
        id="expiry-beats-a-pending-retry",
    ),
    pytest.param(
        _state(received_at=NOW - MAX_AGE - timedelta(seconds=1), retries_left=0),
        _route(),
        Expire(),
        id="expiry-beats-exhaustion",
    ),
    pytest.param(
        _state(received_at=NOW - MAX_AGE),
        _route(),
        Dispatch(),
        id="exactly-max-age-old-is-not-expired",
    ),
    pytest.param(
        _state(retries_left=0),
        _route(),
        DeadLetter("exhausted"),
        id="exhausted-dead-letters",
    ),
    pytest.param(
        _state(retries_left=0, retry_at=NOW + timedelta(hours=1)),
        _route(),
        DeadLetter("exhausted"),
        id="exhaustion-beats-a-pending-retry",
    ),
    pytest.param(
        _state(),
        None,
        DeadLetter("unroutable"),
        id="unroutable-dead-letters",
    ),
    pytest.param(
        _state(terminal_state="completed"),
        None,
        DeadLetter("unroutable"),
        id="unroutable-beats-a-terminal-marker",
    ),
    pytest.param(
        _state(terminal_state="completed"),
        _route(),
        Finish("completed"),
        id="finish-completed",
    ),
    pytest.param(
        _state(terminal_state="errored"),
        _route(),
        Finish("errored"),
        id="finish-errored",
    ),
    pytest.param(
        _state(terminal_state="expired"),
        _route(),
        Finish("expired"),
        id="finish-expired",
    ),
    pytest.param(
        _state(terminal_state="dead"),
        _route(),
        Finish("dead"),
        id="finish-dead",
    ),
]


@pytest.mark.parametrize(("state", "route", "expected"), DECISION_CASES)
def test_decide(state, route, expected):
    assert decide(state, route, NOW) == expected


# --- transition writers ----------------------------------------------------


def test_claim_first_attempt_writes_max_retries():
    assert claim(None, max_retries=3) == (
        Patch(properties={PROP_RETRIES_LEFT: "3"}, categories=(CATEGORY_PROCESSING,)),
    )


def test_claim_decrements_retries_left():
    assert claim(2, max_retries=3) == (
        Patch(properties={PROP_RETRIES_LEFT: "1"}, categories=(CATEGORY_PROCESSING,)),
    )


def test_completed_writer():
    assert completed("consent") == (
        Patch(properties={PROP_STATE: "completed"}, categories=()),
        Move("consent/completed"),
    )


def test_transient_failure_writer():
    backoff = timedelta(minutes=5)
    assert transient_failure(NOW, backoff) == (
        Patch(properties={PROP_RETRY_AT: format_datetime(NOW + backoff)}, categories=()),
    )


def test_permanent_failure_writer():
    assert permanent_failure("consent") == (
        Patch(properties={PROP_STATE: "errored"}, categories=(CATEGORY_ERRORED,)),
        Move("consent/error"),
    )


def test_expire_writer():
    assert expire() == (
        Patch(properties={PROP_STATE: "expired"}, categories=(CATEGORY_EXPIRED,)),
        Move("dead-letters"),
    )


def test_dead_letter_writer():
    assert dead_letter() == (
        Patch(properties={PROP_STATE: "dead"}, categories=(CATEGORY_DEAD,)),
        Move("dead-letters"),
    )


@pytest.mark.parametrize(
    "instructions",
    [
        pytest.param(completed("consent"), id="completed"),
        pytest.param(permanent_failure("consent"), id="permanent_failure"),
        pytest.param(expire(), id="expire"),
        pytest.param(dead_letter(), id="dead_letter"),
    ],
)
def test_patch_precedes_move(instructions):
    assert len(instructions) == 2
    assert isinstance(instructions[0], Patch)
    assert isinstance(instructions[1], Move)


FINISH_CASES = [
    pytest.param("completed", "consent/completed", id="finish-completed-matches-completed-move"),
    pytest.param("errored", "consent/error", id="finish-errored-matches-permanent-failure-move"),
    pytest.param("expired", "dead-letters", id="finish-expired-matches-expire-move"),
    pytest.param("dead", "dead-letters", id="finish-dead-matches-dead-letter-move"),
]


@pytest.mark.parametrize(("terminal_state", "expected_path"), FINISH_CASES)
def test_finish_agrees_with_its_writers_move(terminal_state, expected_path):
    assert finish(terminal_state, "consent") == (Move(expected_path),)
