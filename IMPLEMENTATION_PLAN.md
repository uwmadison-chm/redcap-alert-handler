# Implementation Plan: `rah`

Companion to `DESIGN_BRIEF.md`. Steps are ordered so that every stage ends with something runnable, and the debugging tools (`rah auth`, `rah doctor`) exist before the machinery they'll debug. Development is red-green: each step's tests are written before its code. Throughout, CLI behavior follows [clig.dev](https://clig.dev/)

Each step lists a **Done when**; this delineates basic units of work.

## Language & Packages

* Python 3.14, `uv` for package management
* The package is `redcap_alert_handler` and the root CLI endpoint is `rah`
* `typer` for CLI handling
* `humanfriendly` for parsing quantities
* `msal` for Azure authentication
* `httpx` for requests to the MS Graph API
* stdlib `logging` for logging. `logger` object pattern. Don't pollute the root logger.
* `ruff` for linting and formatting
* `ty` for type checking
* `pytest` and `pytest-xdist` for testing (`pytest -n auto` for parallel testing)
* `rust-just` for simple `make`-like development commands (don't set up `just` recipes for `rah` CLI endpoints)

## Organization:

`src/redcap_alert_handler` -- source base, contains `__init__.py` and main modules
`src/redcap_alert_handler/cli` -- CLI endpoint files
`tests/` -- tests
`tests/data/` -- stored fixture data for testing; good / bad configurations should generally be in files rather than inline strings in test code

## Universal CLI Options and environment variables

* `--config`, `-c`, `$RAH_CONFIG` -- path to the configuration TOML file. No default path: a command that needs config and doesn't get one exits with a usage error.
* `--secrets`, `$RAH_SECRETS` -- path to the secrets TOML file; the flag wins if both are present. This path can't live in the config file because under systemd credentials it isn't known until the service starts; the unit bridges with `Environment=RAH_SECRETS=%d/secrets.toml` (`%d` is systemd's specifier for the credentials directory).
* `--verbose`, `-v`, `$RAH_DEBUG` -- set logger level to logging.DEBUG
* `--quiet`, `-q` -- set logger level to logging.ERROR (if both are specified, warn and use logging.DEBUG)
* `--no-color`, `$NO_COLOR`, `$RAH_NO_COLOR` -- turn off color in logging.

Most other configuation information should be in the configuration TOML file.

## Log message, interaction, and configuration conventions

* Use color in logs sparingly, for important effect. `typer` includes `rich` so you can use that syntax.
* Prefixing log lines with emoji is okay in moderation. Use a small set for INFO and above -- possibly ✅ and ❌ for INFO-level success / failure, 💥 for ERROR-level messages. DEBUG messages may choose from a larger, more expressive set.
* If emoji are used to flag message types, there should be something like `rah log-help` to print emoji and their meanings
* Do not include emoji as infix or suffix indicators
* CLI commands should gracefully handle "normal unix things" -- ^C and `kill` and being passed to `head`, for example. Don't write a BrokenPipeError or KeyboardInterrupt to INFO (they're acceptable on DEBUG, though).
* Dates & times should be specified in ISO format
* Durations and quantities (if needed) should be specifiable in human-readable format (3h -> 3 hours). Note `humanfriendly` parses sizes as decimal by default: 4K -> 4000, 4KiB -> 4096. Take the default.

## Comments and Docstrings

Comments should be concise and explain why a particular decision was made in a context.

Function docstrings should also be concise, in Google style (without typing), and intended for the authors of this package. The product here is the CLI; other package authors are very unlikely to import functions from this package. Particularly do not list parameter or return types; we have type hinting for that.

Exception: the handler contract (step 5) *is* imported by handler packages, so its docstrings are reference-grade -- especially `Raises:`, since the exception hierarchy is the handler API's control flow.

Every source file starts with this header (no year -- LICENSE holds the canonical notice):

```python
# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.
```

## 0. Scaffold and CLI framing

* `uv init` a Python 3.14 project; `pyproject.toml` with `[project.scripts] rah = "redcap_alert_handler.cli.main:main"`. Keep `cli/__init__.py` empty; the app and entry point live in `cli/main.py` so future endpoint modules can import `app` without a circular import through the package `__init__`.
* typer group with `--version` and `-h/--help`; subcommands stubbed as they arrive.
* CLI conventions module, shared across endpoints
  * data > stdout, messages/logs > stderr;
  * logging: human-readable to stderr, INFO default, `-v/--verbose` > DEBUG, `-q/--quiet` > ERROR
  * exit codes: 0 success, 1 runtime failure, 2 usage (typer's default);
  * color off when not a TTY or options / env requires
* `ruff`, `ty`, `pytest` configured and passing in a pre-commit-or-CI check.
* `rust-just` installed (`rust-just` in pypi) and a justfile with working recipes for common development tasks for testing, linting, formatting, and type checking
* Expect `just` to be available in $PATH, so `just test` and `just reformat` should... "just" work
* Bare `just` runs the default `check` recipe: the mutating local dev loop (sync, reformat, lint, typecheck, test). `safe-check` is the non-mutating variant for CI.

**Done when:** `uv run rah --help` renders sensible help; bare `just` and `uv run just safe-check` both pass. 

## 1. Config and secrets models

Most configuration is stored in the TOML-based config file, rather than passed as CLI options.

* `tomllib` + plain frozen dataclasses (no config-framework dependency).
* The file contains a global section and a routes section keyed on slug.
* Global configuration contains:
  * token cache path (absolute)
  * state base dir (absolute)
  * polling interval
  * handler timeout
  * retry / backoff settings
  * max_age (the default for routes that don't set their own)
* Route-specific config is a mapping keyed on slug and containing at least a handler reference; `max_age` may be set per-route, overriding the global default. Other keys are passed as part of the context to the handler, preserved opaquely per the brief. Global keys are included in the context; route-specific keys override. Handlers are responsible for handling their own validation and must ignore irrelevant context information.
* Slugs must look like ASCII Python identifiers (`[A-Za-z_][A-Za-z0-9_]*`) and match subjects case-sensitively. Enforce this in validation: slugs become mailbox folder names, and folder creation is a bad time to find out one contains a `/`.
* The secrets file path is *not* in the config file -- under systemd credentials it isn't known until the service starts. Resolution order is in "Universal CLI Options" above.
* Secrets file is TOML and contains info needed for auth: tenant ID, client ID, and client secret (we're a confidential client -- see `rah auth`)
* Token cache lives at the path given in the main config, in whatever format is easiest for `msal` to deal with

Validation errors are the project's first UX surface: report *all* problems, one per line, with the offending key path; exit 1

`rah doctor -v` should display a pretty-printed configuration as part of its output. `rah doctor -q` should display only errors.

**Done when:** table-driven tests cover good/bad configs; error output reads like advice, not a traceback.

## 2. `rah auth`

* `msal` delegated auth-code flow as a confidential client -- our tenant doesn't let us register public clients, which rules out device code and is why the secrets file carries a client secret.
* No localhost listener. `rah auth` prints the authorization URL; the operator opens it in a browser wherever convenient, signs in as the service account, and pastes the resulting redirect URL back into the prompt. This works over SSH, which is the expected case.
* The registered redirect URI never has to be served -- the browser's failed navigation to it still carries the auth code in the address bar. `msal`'s `initiate_auth_code_flow` / `acquire_token_by_auth_code_flow` handle the state check and code redemption.
* Writes the serialized token cache to the path from the main config, mode `0600`.
* On success, print the signed-in account and token expiry as INFO. Single-purpose: no health checks here (that's `doctor`).
* Silent-refresh path (`acquire_token_silent`) factored so `watch` reuses it later -- probably in `redcap_alert_handler/auth.py`

**Done when:** a real token is acquired against the tenant; a second run refreshes silently from the cache; unit tests cover cache read/write with msal mocked.

## 3. Graph client module

The one thin wrapper (`msal` + `httpx`) everything else calls. Small, boring, heavily tested.

* Operations: list messages in a folder, get message with `$expand`ed single-value extended properties, patch properties/categories, move, find/create folders, seed the master category list.
* 429/5xx retry honoring `Retry-After`.
* **Testing backbone built here:** a fake Graph via `httpx.MockTransport` plus canned JSON fixtures. Every later integration test rides on this -- treat it as a first-class deliverable, not test scaffolding.
* Once the fake Graph has its real shape, write a project skill (`.claude/skills/graph-testing/SKILL.md`) documenting how to test against it -- where fixtures live, how to register responses, common patterns. Steps 4-8 all lean on it, so capturing this at the end of step 3 pays for itself immediately.

**Done when:** all operations pass against the fake; a manual smoke run lists the real inbox.

## 4. `rah doctor`

* Checks, in order, reporting each: config parses; routes valid; every configured handler resolves to an installed entry point; secrets file readable; token cache present and refreshable; Graph reachable; mailbox folders (`{slug}/completed`, `{slug}/error`, `dead-letters`) exist; `rah:*` categories seeded.
* `--fix` idempotently provisions missing folders and categories (the answer to "who creates the folder layout": doctor, on demand -- not `watch` at startup).
* `rah init` -- an alias for `rah doctor --fix`, because "run `rah init`" documents better than a repair flag. Same code path; the alias is the documented first-run step after `rah auth`.
* Human-readable output; `--json` for machines; exit 0 only if all checks pass, so it can back a cron or monitoring probe.
* `-o`, `--output` to write to a file instead of stdout

**Done when:** against the real mailbox, `rah init` then `rah doctor` runs green; each failure mode has a test asserting a helpful message and nonzero exit.

## 5. Handler contract

The public, versioned, cross-repo API -- reviewed as such.

* `HandlerError` / `TransientError` / `PermanentError` hierarchy.
* `Message` frozen dataclass (`internet_message_id`, subject, text/HTML body, sender, received time) -- picklable, no Graph types.
* `Context` (slug, full route config entry, per-route state dir; exact extra fields resolve the brief's open question here).
* Entry-point loader: resolve *all* configured handlers at startup, fail fast with a message naming the missing entry point and the route that wanted it.
* A built-in example handler shipped by `rah` itself under `rah.handlers` (e.g. one that just logs the message) -- proves the discovery machinery and powers end-to-end tests without a private handler package.
* A "Handler API" doc section with the semver promise from the brief.

**Done when:** the example handler is discovered through real `importlib.metadata` entry points in tests, not by monkeypatching.

## 6. Dispatch core (pure logic, no I/O)

* Slug matcher: longest-match against configured slugs, optional `|` delimiter.
* Decision function: (message state, route config, now) > action -- dispatch, wait-for-retry, expire, dead-letter, route-error. Pure and exhaustively table-tested; this is where the brief's state model becomes code.
* Transition writer contracts: authoritative property first, then advisory category; retries-left decremented **before** dispatch.
* Retry policy: initial retries-left and backoff > retry-time computation.

**Done when:** the new/retrying/expired/unroutable/exhausted matrix is covered by table-driven tests with no mocks needed.

## 7. `rah watch`

The main event; everything above composes here.

* Foreground poll loop (interval from config, `--poll-interval` override): list inbox > decide > claim (decrement + `rah:processing`) > dispatch on a worker thread pool > apply outcome (property, category, move).
* Timeout = abandon, per the brief; abandoned-thread accounting in logs.
* Hourly proactive token refresh inside the loop; on auth failure, keep polling, log loudly, re-read the cache each cycle.
* Clean shutdown on SIGTERM/SIGINT: stop claiming, let in-flight handlers finish (bounded), exit 0.
* One log line per message state transition -- the mailbox-legibility story's stderr counterpart.
* Integration tests on the fake Graph: happy path, transient>retry>success, permanent>error folder, poison>dead-letters, and crash-recovery (side effect done but move not -- reprocess must no-op via the handler's claim).

**Done when:** against the real mailbox, a REDCap-style message sent by hand flows to `{slug}/completed` via the built-in handler, and the poison/retry tests pass on the fake.

## 8. `rah reprocess`

* Explicit selection required (no bare "replay everything by accident"): `--route SLUG` and/or `--folder dead-letters|error`, with `--all` as the deliberate big hammer. Settle the brief's open question here, including how replay interacts with max-age.
* Resets machine state (retries-left, retry-time), moves back to inbox; the watcher does the rest. No processing logic in this command.
* `--dry-run` prints what would move.

**Done when:** a dead-lettered message replays end-to-end through a running `watch`.

## 9. Deployment and docs

* systemd unit (foreground service, `StateDirectory`, `LoadCredentialEncrypted`) plus a documented bootstrap: create service user, encrypt secrets, `rah auth`, `rah init`, enable service.
* Interactive commands can't see the service's `$CREDENTIALS_DIRECTORY` -- it exists only inside the unit. The documented pattern for `rah auth` and `rah init` on the service box is `systemd-run --pty` with the same credential properties as the unit. If that proves too fiddly during rehearsal, fall back to a plain permissions-protected secrets file and skip the encryption.
* Example deployment project (`our-rah` skeleton) and example handler-package skeleton demonstrating the entry-point registration and git-pin pattern from the brief.
* README and handler-author guide.

**Done when:** a fresh-box bootstrap has been rehearsed start to finish from the docs alone.

## Cross-cutting

* Version `0.x` with semver discipline on the handler API from step 5 onward; keep a changelog.
* Nothing sensitive in logs at any level -- message subjects may carry participant-adjacent data, so DEBUG logs the `internet_message_id` and slug, not bodies.
