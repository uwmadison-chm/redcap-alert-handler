# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""A worker pool built for handlers that might never return.

Handlers run under a timeout, but there's no way to kill a running Python
thread. So a timeout here doesn't stop the handler; it abandons it. The
caller stops waiting, counts the attempt as failed, and the thread keeps
going on its own. stdlib's ThreadPoolExecutor can't do that: its workers are
non-daemon and get joined when the interpreter exits, so one wedged handler
would keep `rah process` from ever shutting down, and its thread would sit on
a pool slot forever.

This pool runs daemon threads, so a handler still going at exit dies with the
process -- the same crash the handler contract already plans for, since a
handler claims its message before doing anything with side effects. And when
a worker is abandoned, the pool starts a fresh one in its place, so it keeps
its full count of workers ready for the next message.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from redcap_alert_handler.logs import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Job:
    """A unit of work and the box its result lands in.

    The Future is a plain thread-safe result box, not driven by any executor:
    the worker calls set_result or set_exception on it, and the submitter
    waits with `job.future.result(timeout=...)`. The `abandoned` and
    `finished` flags belong to the pool and are only ever touched under its
    lock.
    """

    fn: Callable[[], object]
    future: Future[object] = field(default_factory=Future)
    abandoned: bool = False
    finished: bool = False


class HandlerPool:
    """A fixed-size pool of daemon workers that can outlive a wedged job.

    Submit a callable, get back a Job; wait on its future with a timeout, and
    if the wait runs out, abandon the Job. Abandoning replaces the stuck
    worker so capacity never drops below max_workers.
    """

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be at least 1, got {max_workers}")

        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        # Workers still pulling from the queue. A replacement started on
        # abandon keeps this at max_workers; only close() and a sentinel-driven
        # exit bring it down.
        self._active_workers = 0
        self._abandoned_count = 0
        self._closed = False

        with self._lock:
            for _ in range(max_workers):
                self._start_worker()

    @property
    def abandoned_count(self) -> int:
        """How many jobs have been abandoned over the pool's life."""
        with self._lock:
            return self._abandoned_count

    @property
    def active_worker_count(self) -> int:
        """Workers currently able to pull from the queue."""
        with self._lock:
            return self._active_workers

    def submit(self, fn: Callable[[], object]) -> Job:
        """Queue fn to run on a worker and hand back its Job.

        Raises:
            RuntimeError: if the pool has been closed.
        """
        job = Job(fn=fn)
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed; can't submit new work")
            self._queue.put(job)
        return job

    def abandon(self, job: Job) -> bool:
        """Give up on a job whose wait timed out; report whether we did.

        Returns True when the job was still running and is now abandoned: its
        worker is marked to exit once its fn finally returns, and a fresh
        worker is started to hold the slot. Returns False when the job had
        already finished -- the caller lost the race and should read the real
        result off the future instead.

        The check and the mark both happen under the lock, and a worker sets
        `finished` under that same lock before it publishes to the future.
        That ordering settles the race: any caller that has seen a result has
        also, by then, made `finished` visible here, so a job that slipped in
        under the timeout can't be abandoned by mistake.
        """
        with self._lock:
            if job.finished:
                return False
            job.abandoned = True
            self._abandoned_count += 1
            # The stuck worker won't pull again, so drop it from the count and
            # start a replacement to keep capacity at max_workers.
            self._active_workers -= 1
            self._start_worker()
            logger.debug("abandoned a job; %d abandoned so far", self._abandoned_count)
            return True

    def close(self) -> None:
        """Stop taking work and let idle workers drain and exit.

        One sentinel per active worker; each idle worker takes one and stops.
        A worker still busy on a job finishes it, then takes its sentinel on
        the next pull. Abandoned workers are already gone from the count, so
        this never waits on one. Safe to call more than once.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sentinels = self._active_workers
        for _ in range(sentinels):
            self._queue.put(None)

    def _start_worker(self) -> None:
        # Caller holds self._lock.
        thread = threading.Thread(target=self._work, name="rah-handler-worker", daemon=True)
        self._threads.append(thread)
        self._active_workers += 1
        thread.start()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                with self._lock:
                    self._active_workers -= 1
                return

            exc: Exception | None = None
            result: object = None
            try:
                result = job.fn()
            except Exception as e:
                exc = e

            # Mark finished under the lock before touching the future, so a
            # caller that reads the result can't beat this write and abandon a
            # job that already landed. Read the abandoned flag in the same
            # critical section to settle the abandon/finish race.
            with self._lock:
                job.finished = True
                abandoned = job.abandoned

            if exc is not None:
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)

            if abandoned:
                # abandon() already dropped us from the count and started a
                # replacement, so leave without pulling more work.
                return
