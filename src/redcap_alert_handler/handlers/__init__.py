# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""The handler contract: what a route's handler receives, and how it fails.

This package is the public, versioned API handler packages build against,
and these re-exports are exactly the names a handler author needs: `Message`
and `Context` to receive, the `HandlerError` hierarchy to raise. They're
re-exported once more at the package root, so
`from redcap_alert_handler import PermanentError` works.

Everything else in here is rah's own machinery, imported from its defining
module: entry-point resolution in `loader`, `build_context` in `contract`,
built-in handlers in modules like `log_message`.
"""

from redcap_alert_handler.handlers.contract import Context, Message
from redcap_alert_handler.handlers.errors import (
    HandlerError,
    PermanentError,
    TransientError,
)

__all__ = [
    "Context",
    "HandlerError",
    "Message",
    "PermanentError",
    "TransientError",
]
