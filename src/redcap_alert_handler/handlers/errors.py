# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""How a handler tells the engine what happened: raise, or don't.

These are the exceptions handler packages import, usually through the
package-root shorthand (`from redcap_alert_handler import PermanentError`).
The return value of a handler is ignored; this hierarchy is the whole
outcome vocabulary.
"""

from __future__ import annotations


class HandlerError(Exception):
    """Base of the handler outcome hierarchy -- raise a subclass, not this.

    A handler tells the engine what happened by raising, or not; the return
    value is ignored. Anything that isn't a `TransientError` or a
    `PermanentError`, including a bare `HandlerError`, counts as an
    unexpected failure and is treated exactly like `TransientError`: the
    retries-left counter is what stops a broken handler from looping
    forever, not the exception's type.
    """


class TransientError(HandlerError):
    """Raise to ask for a retry later, with backoff.

    The engine decrements retries-left when it claims a message, before the
    handler runs, so raising this costs nothing further -- the message waits
    out the backoff and comes around again. When retries-left hits zero, the
    message moves to the global `dead-letters` folder instead of being
    dispatched (the route's own error folder is reserved for
    `PermanentError`).

    A handler timeout is not a raise: the watcher abandons the thread rather
    than killing it, so a timed-out handler may keep running and finish on
    its own, side effects and all, after the engine has already counted the
    attempt as a transient failure. This is why a handler must record a
    claim on `Message.internet_message_id` in its own store before doing
    anything with side effects, and no-op if the same id turns up again.
    """


class PermanentError(HandlerError):
    """Raise when no retry will help: the message goes straight to error.

    Use this for a failure another attempt can't fix -- a message that will
    never parse, a REDCap record that doesn't exist. Retrying it would only
    spend the retry budget on something that was never going to work. The
    message moves to the route's error folder without decrementing anything
    further.
    """
