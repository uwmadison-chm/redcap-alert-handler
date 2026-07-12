# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The built-in handler: log that a message arrived, and nothing else.

Registered as `redcap-alert-handler:log_message`, so a fresh rah install has
a working handler to point a route at before any real one is written. It
does no claim-keeping of its own -- there's no side effect here to protect
against a re-run.
"""

from __future__ import annotations

from redcap_alert_handler.handlers.contract import Context, Message
from redcap_alert_handler.logs import get_logger

logger = get_logger(__name__)


def log_message(message: Message, context: Context) -> None:
    """Log one INFO line naming the route and the message, then return.

    Only `context.slug` and `message.internet_message_id` go to the log --
    never the subject, body, or sender. Subjects in particular can carry
    participant-adjacent data (a name, a record identifier), so nothing
    message-derived beyond the id shows up here, per the project's
    no-sensitive-logs rule.
    """
    logger.info("log_message: routes.%s received %s", context.slug, message.internet_message_id)
