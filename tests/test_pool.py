# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

from __future__ import annotations

import threading

import pytest

from redcap_alert_handler.pool import HandlerPool


def test_submit_returns_result():
    pool = HandlerPool(max_workers=2)
    try:
        job = pool.submit(lambda: 21 * 2)
        assert job.future.result(timeout=2) == 42
    finally:
        pool.close()


def test_many_jobs_run_across_workers():
    pool = HandlerPool(max_workers=3)
    try:
        jobs = [pool.submit(lambda n=n: n * n) for n in range(12)]
        results = sorted(job.future.result(timeout=2) for job in jobs)
        assert results == sorted(n * n for n in range(12))
    finally:
        pool.close()


def test_exception_propagates_through_future():
    pool = HandlerPool(max_workers=1)
    try:

        def boom():
            raise ValueError("nope")

        job = pool.submit(boom)
        with pytest.raises(ValueError, match="nope"):
            job.future.result(timeout=2)
    finally:
        pool.close()


def test_abandon_frees_the_slot_and_stuck_worker_does_not_rejoin():
    # One slot makes the point sharp: if the wedged worker ever came back to
    # pull work, the follow-up job below could only run once it did.
    pool = HandlerPool(max_workers=1)
    try:
        started = threading.Event()
        release = threading.Event()
        captured: dict[str, threading.Thread] = {}

        def wedged():
            captured["thread"] = threading.current_thread()
            started.set()
            # A bounded guard so a bug in the pool can't hang the suite.
            release.wait(timeout=5)

        job = pool.submit(wedged)
        assert started.wait(timeout=2)

        # The caller's wait times out; the handler is still running.
        with pytest.raises(TimeoutError):
            job.future.result(timeout=0.05)

        assert pool.abandon(job) is True
        assert pool.abandoned_count == 1

        # The replacement worker should take the slot and run new work now,
        # while the wedged handler is still blocked.
        follow = pool.submit(lambda: "ok")
        assert follow.future.result(timeout=2) == "ok"

        # Let the wedged handler finish. It must exit rather than pull more
        # work; joining its thread proves it (a rejoined worker would be
        # blocked on the queue and still alive).
        release.set()
        stuck = captured["thread"]
        stuck.join(timeout=2)
        assert not stuck.is_alive()

        # Capacity held at max_workers throughout: no slot leaked, none doubled.
        assert pool.active_worker_count == 1
    finally:
        release.set()
        pool.close()


def test_abandon_after_finish_is_a_noop():
    pool = HandlerPool(max_workers=1)
    try:
        job = pool.submit(lambda: 7)
        assert job.future.result(timeout=2) == 7

        # The job is done, so there's nothing to abandon and no replacement
        # to spawn; the roster stays exactly as it was.
        assert pool.abandon(job) is False
        assert pool.abandoned_count == 0
        assert pool.active_worker_count == 1
    finally:
        pool.close()


def test_close_winds_down_a_quiet_pool():
    pool = HandlerPool(max_workers=3)
    roster = list(pool._threads)
    pool.close()

    for thread in roster:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert pool.active_worker_count == 0
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


def test_close_is_idempotent():
    pool = HandlerPool(max_workers=2)
    roster = list(pool._threads)
    pool.close()
    pool.close()
    for thread in roster:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert pool.active_worker_count == 0


def test_max_workers_must_be_positive():
    with pytest.raises(ValueError, match="max_workers"):
        HandlerPool(max_workers=0)
