# rah: the redcap alert handler
A flexible monitor / dispatcher to let you use REDCap email sent to an o365 mailbox as a general message queue

## Handler API

Handlers are plain functions, shipped in ordinary Python packages and found
through the `rah.handlers` entry-point group. A route's config names its
handler with a package-qualified reference:

```toml
[routes.consent]
handler = "study-acme-handlers:consent"
```

That's the distribution name and the entry-point name it registered, joined
with a colon. On the handler package's side, the registration lives in
`pyproject.toml`:

```toml
[project.entry-points."rah.handlers"]
consent = "study_acme_handlers.consent:handle"
```

The function itself takes a message and a context, and returns nothing:

```python
from redcap_alert_handler import Message, Context, TransientError, PermanentError

def handle(message: Message, context: Context) -> None:
    ...
```

`Message` is the email reduced to plain values: the RFC Message-ID, subject,
text and HTML bodies, sender, and received time. `Context` carries the route's
slug, a per-route state directory, and the route's config table merged over any
extra keys from `[global]` -- whatever the operator wrote there arrives as-is.
Document the keys your handler reads, and ignore ones you don't recognize; the
same `[global]` extras reach every route. The docstrings in
`redcap_alert_handler.handlers` are the full reference.

Outcomes are exceptions. Return normally and the message is done. Raise
`TransientError` and rah retries the message later, with backoff; after too
many transient failures it lands in the dead-letters folder. Raise
`PermanentError` when no retry would help, and the message goes straight to
the route's error folder. Anything else you raise is treated like a transient
failure, so a handler that crashes on some message can't loop on it forever.

Two rules for handler authors:

* Record a claim on `message.internet_message_id` in your own store (a sqlite
  file in `context.state_dir` works fine) *before* performing any side effect,
  and do nothing if the same id shows up again. A crash between your side
  effect and rah's bookkeeping means the message will come around again, and
  the recorded claim is the only thing that stops the effect from happening
  twice.
* Depend on `rah` with a wide version range (`>=0.5,<1` style), never an exact
  pin. Pinning is the deployment project's job, and two handler packages that
  pin different rah versions can't be installed into one environment.

It also helps to state your distribution name and handler names right in your
package's README, since the `package:name` string is exactly what an operator
copies into a route.

The contract -- `Message`, `Context`, the exception hierarchy, and the
entry-point group -- is versioned with semver discipline from here on out.
While rah is pre-1.0, any change that would break an installed handler package
bumps the minor version; after 1.0, breaking changes mean a major version, as
usual.
