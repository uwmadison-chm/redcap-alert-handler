# rah: the redcap alert handler
A flexible monitor / dispatcher to let you use REDCap email sent to an o365 mailbox as a general message queue

## What it's for

Sometimes you want something to happen based on a trigger from REDCap, but REDCap doesn't have the ability. Maybe you want to assign a participant to a group using a model while they're still taking survey. Or, based on survey responses, decide which
intervention message to text them. Or even send a push notification to someone's phone.

REDCap doesn't give you a good way to do any of those things. Data Entry Triggers are the
official answer, and they _suck_ -- one fires on every single data change, the
message isn't configurable, and if your server is slow it slows down every data change in your entire project.

Alerts and Automated Survey Invitations don't have those problems. You can target
them with project logic, so they only fire when you actually care. You write
their bodies, so they carry exactly the data you need. And they arrive as
email. And... email is fast! Tested with our campus O365 mail, alerts showed up in the mailbox two to
six seconds after the Alert was sent, which is fast enough for a lot of things
that we'd call "realtime."

So: rah treats an O365 mailbox as a work queue. An inbox is an... unusual... thing to use as a
queue, and the design goes to some trouble on that account, but it means there's
no public endpoint to host, no webhook to secure, and no extra infrastructure
between REDCap and your code.

## How it works

`rah process` polls the mailbox through the Microsoft Graph API, every few
seconds by default. For each message it finds, it takes the subject up to the
first `|` and looks for a route with that exact slug. The route names a handler
-- a plain Python function you wrote, installed from its own package -- and rah
calls it with the message and the route's config.

What happens next depends on what the handler does. Returns normally and the
message moves to that route's `completed` folder. Raises a transient error and
rah leaves it alone for `retry_backoff` and tries again on a later pass, until
it either succeeds or burns through `max_retries` and dead-letters. Raises a permanent error and it goes to the
route's `error` folder. A subject that matches no route dead-letters
immediately, and so does anything that arrived longer ago than the route's
`max_age`, so a weekend-long outage doesn't end with you acting on Friday's
mail on Monday morning.

Because every processed message moves out of the inbox, the inbox is the
queue -- there's no cursor or delta token to keep, and a poll against a
caught-up mailbox is one cheap request. The flip side is that exactly one rah
may claim messages from a mailbox at a time. Run the watcher or run it from
cron, not both.

`rah process` runs in the foreground and logs to stdout, so systemd (or your
terminal, while you're working) does the process management. Handlers run in
threads inside that one process, which is worth knowing when you write one:
they need to be idempotent, since a crash at the wrong moment means a message
comes back around.

## Setting up a deployment

rah by itself doesn't do anything interesting -- the handlers do the actual
work, and they live in their own packages. So a deployment is a small uv
project with no code in it at all. Its job is to pin a version of rah together
with the handler packages this box should run, and to hold the config that says
which alerts go to which handler.

Name it whatever makes sense; ours is `our-rah`. Say you want to run
[rah-random-forest](https://github.com/uwmadison-chm/rah-random-forest), which
scores an alert through a scikit-learn model and writes the predictions back to
REDCap:

```
uv init --bare our-rah
cd our-rah
uv add "redcap-alert-handler @ git+https://github.com/uwmadison-chm/redcap-alert-handler@v0.1.0"
uv add "rah-random-forest @ git+ssh://git@github.com/uwmadison-chm/rah-random-forest@v0.1.0"
```

You end up with a `pyproject.toml` that's almost entirely dependencies:

```toml
[project]
name = "our-rah"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "redcap-alert-handler",
    "rah-random-forest",
]

[tool.uv.sources]
redcap-alert-handler = { git = "https://github.com/uwmadison-chm/redcap-alert-handler", tag = "v0.1.0" }
rah-random-forest = { git = "ssh://git@github.com/uwmadison-chm/rah-random-forest", tag = "v0.1.0" }
```

rah's repo is public, so it clones over HTTPS with no credentials. Handler
packages are often private, and an `ssh://` pin means uv authenticates with the
box's deploy key or your ssh agent -- no package index to run, no tokens to
rotate. Pin tags rather than branches, and commit `uv.lock`: that lockfile is
the deploy manifest, and reverting it plus `uv sync` and a restart is the whole
rollback story.

### Wiring a route

Installing the package makes its handlers available; the config decides whether
any of them run. A handler package's README states its distribution name and
the handler names it registers -- rah-random-forest registers `predict` -- and
`package:name` is the string you copy into a route:

```toml
[global]
mailbox = "svc-rah@example.edu"
base_folder = "inbox"
token_cache_path = "/var/lib/rah/token-cache.json"
state_base_dir = "/var/lib/rah/state"
max_retries = 5
retry_backoff = "5m"
max_age = "1d"

[routes.rf_predict]
handler = "rah-random-forest:predict"

# Everything below here belongs to the handler, not to rah.
redcap_info_file = "/var/lib/rah/secrets/redcap.toml"
redcap_id_field = "record_id"
model_file = "/var/lib/rah/models/panas_rf.joblib"

[routes.rf_predict.input_fields]
panas20_q01 = "q1"
panas20_q02 = "q2"

[routes.rf_predict.target_fields]
pa = "pa_zscore"
na = "na_zscore"
```

The slug (`rf_predict`) is the route's name everywhere. REDCap alert subjects
look like `slug|whatever else you want`, and the part before the first `|` is
matched against your slugs exactly; the same slug names the mailbox folder the
route's messages land in and the directory under `state_base_dir` the handler
gets to write in. Only `handler` means anything to
rah. The rest of the table is passed through to whatever the handler asks for,
so what goes there comes from the handler package's docs, not from these.

### First run

Credentials -- tenant id, client id, client secret -- go in a separate TOML file
you don't commit, and get passed with `--secrets`. Then, on a fresh mailbox:

```
uv run rah auth --config rah.toml --secrets secrets.toml
uv run rah init --config rah.toml --secrets secrets.toml
uv run rah doctor --config rah.toml --secrets secrets.toml
uv run rah process --config rah.toml --secrets secrets.toml --watch
```

`auth` signs in as the service account and caches the token; `init` creates the
mailbox folders and categories rah needs; `doctor` reports on all of it plus
your routes, including whether each handler resolves and what its checkup says
about the config. Run `doctor` again after any config change -- catching a typo
in `model_file` there beats catching it one message at a time in the error
folder.

Adding a second handler package is one `uv add` and one `[routes.*]` table.
Dropping one is a deleted dependency and a deleted table. Neither touches rah or
any of the other handler repos.

### Working on a handler

Entry points are read once at startup, so handlers aren't hot-reloadable and
edits need a restart. While you're actively developing one, swap the git pin for
a path install so you don't have to reinstall on every change:

```
uv add --editable ../rah-random-forest
```

Put the git pin back before you deploy.

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

### Checkups

rah has no opinion about your config keys. It doesn't know what `model_file`
means, or that `input_fields` has to line up with something, so it can't tell
you when a route's config is broken -- it just hands the table over and lets
your handler discover the problem, one message at a time, into an error folder.

If you'd rather find out sooner, attach a `checkup` to your handler:

```python
def _checkup(context: Context) -> list[str]:
    problems = []
    model_file = context.config.get("model_file")
    if not model_file:
        problems.append("no model_file set")
    elif not Path(model_file).exists():
        problems.append(f"model_file {model_file} doesn't exist")
    return problems

handle.checkup = _checkup
```

`rah doctor` calls it with the same `Context` a message would arrive with, and
`rah process` calls it at startup. Return one string per problem, worded for
whoever has to fix the config; an empty list means the route looks healthy.
Collecting problems beats raising on the first one, since the operator gets the
whole list in one run -- rah catches a raise, but all it can report is that one
exception.

A checkup should be read-only and reasonably quick. It runs on demand, possibly
on a box that isn't processing mail at all, and possibly several times in a row
while someone edits a config and re-runs doctor.

Problems fail `rah doctor` with a nonzero exit. They don't stop `rah process`:
the route still gets its messages, and they fail loudly one at a time, which is
easier to notice than a service that won't come up.

Type checkers grumble about attributes on functions; ty wants a
`# ty: ignore[unresolved-attribute]` comment on that last line.

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
