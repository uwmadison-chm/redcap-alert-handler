# Implementation Plan: `rah`

Companion to `DESIGN_BRIEF.md`. Steps are ordered so that every stage ends with something runnable, and the debugging tools (`rah auth`, `rah doctor`) exist before the machinery they'll debug. Development is red-green: each step's tests are written before its code. Throughout, CLI behavior follows [clig.dev](https://clig.dev/) (see the brief's Style section).

Each step lists a **Done when** — don't move on until it holds.

## 0. Scaffold and CLI framing

* `uv init` a Python 3.14 project; `pyproject.toml` with `[project.scripts] rah = "rah.cli:main"`.
* Click group with `--version` and `-h/--help`; subcommands stubbed as they arrive.
* CLI conventions module, built once and used everywhere:
  * data → stdout, messages/logs → stderr;
  * logging: human-readable to stderr, INFO default, `-v/--verbose` → DEBUG, `-q/--quiet` → errors only;
  * exit codes: 0 success, 1 runtime failure, 2 usage (Click's default);
  * color off when not a TTY or `NO_COLOR` is set; never prompt when stdin isn't a TTY.
* `ruff`, `ty`, `pytest` configured and passing in a pre-commit-or-CI check.

**Done when:** `uv run rah --help` renders sensible help; lint, types, and an initial trivial test pass.

## 1. Config and secrets models

* `tomllib` + plain frozen dataclasses (no config-framework dependency).
* Main config: global table (poll interval, handler timeout, retry count/backoff, state base dir) and routes table — slug key, `max_age`, `handler` ref, with unknown keys preserved and passed through opaquely per the brief.
* Secrets file model: tenant ID, client ID, client secret, token-cache path. Resolution order: `$CREDENTIALS_DIRECTORY`, else `--secrets` flag.
* Validation errors are the project's first UX surface: report *all* problems, one per line, with the offending key path; exit 1.

**Done when:** table-driven tests cover good/bad configs; error output reads like advice, not a traceback.

## 2. `rah auth`

* `msal` delegated flow writing the serialized token cache to the path from the secrets file, mode `0600`.
* **Decision to confirm:** device-code flow is the recommendation — the service box has no browser, and running `rah auth` over SSH while entering the code on a laptop is exactly its use case. Auth-code-with-localhost-redirect is the fallback if the tenant blocks device code.
* On success, print the signed-in account and token expiry. Single-purpose: no health checks here (that's `doctor`).
* Silent-refresh path (`acquire_token_silent`) factored so `watch` reuses it later.

**Done when:** a real token is acquired against the tenant; a second run refreshes silently from the cache; unit tests cover cache read/write with msal mocked.

## 3. Graph client module

The one thin wrapper (`msal` + `httpx`) everything else calls. Small, boring, heavily tested.

* Operations: list messages in a folder, get message with `$expand`ed single-value extended properties, patch properties/categories, move, find/create folders, seed the master category list.
* 429/5xx retry honoring `Retry-After`.
* **Testing backbone built here:** a fake Graph via `httpx.MockTransport` plus canned JSON fixtures. Every later integration test rides on this — treat it as a first-class deliverable, not test scaffolding.

**Done when:** all operations pass against the fake; a manual smoke run lists the real inbox.

## 4. `rah doctor`

* Checks, in order, reporting each: config parses; routes valid; every configured handler resolves to an installed entry point; secrets file readable; token cache present and refreshable; Graph reachable; mailbox folders (`{slug}/completed`, `{slug}/error`, `dead-letters`) exist; `rah:*` categories seeded.
* `--fix` idempotently provisions missing folders and categories (the answer to "who creates the folder layout": doctor, on demand — not `watch` at startup).
* `rah init` — an alias for `rah doctor --fix`, because "run `rah init`" documents better than a repair flag. Same code path; the alias is the documented first-run step after `rah auth`.
* Human-readable output; `--json` for machines; exit 0 only if all checks pass, so it can back a cron or monitoring probe.

**Done when:** against the real mailbox, `rah init` then `rah doctor` runs green; each failure mode has a test asserting a helpful message and nonzero exit.

## 5. Handler contract

The public, versioned, cross-repo API — reviewed as such.

* `HandlerError` / `TransientError` / `PermanentError` hierarchy.
* `Message` frozen dataclass (`internet_message_id`, subject, text/HTML body, sender, received time) — picklable, no Graph types.
* `Context` (slug, full route config entry, per-route state dir; exact extra fields resolve the brief's open question here).
* Entry-point loader: resolve *all* configured handlers at startup, fail fast with a message naming the missing entry point and the route that wanted it.
* A built-in example handler shipped by `rah` itself under `rah.handlers` (e.g. one that just logs the message) — proves the discovery machinery and powers end-to-end tests without a private handler package.
* A "Handler API" doc section with the semver promise from the brief.

**Done when:** the example handler is discovered through real `importlib.metadata` entry points in tests, not by monkeypatching.

## 6. Dispatch core (pure logic, no I/O)

* Slug matcher: longest-match against configured slugs, optional `|` delimiter.
* Decision function: (message state, route config, now) → action — dispatch, wait-for-retry, expire, dead-letter, route-error. Pure and exhaustively table-tested; this is where the brief's state model becomes code.
* Transition writer contracts: authoritative property first, then advisory category; retries-left decremented **before** dispatch.
* Retry policy: initial retries-left and backoff → retry-time computation.

**Done when:** the new/retrying/expired/unroutable/exhausted matrix is covered by table-driven tests with no mocks needed.

## 7. `rah watch`

The main event; everything above composes here.

* Foreground poll loop (interval from config, `--poll-interval` override): list inbox → decide → claim (decrement + `rah:processing`) → dispatch on a worker thread pool → apply outcome (property, category, move).
* Timeout = abandon, per the brief; abandoned-thread accounting in logs.
* Hourly proactive token refresh inside the loop; on auth failure, keep polling, log loudly, re-read the cache each cycle.
* Clean shutdown on SIGTERM/SIGINT: stop claiming, let in-flight handlers finish (bounded), exit 0.
* One log line per message state transition — the mailbox-legibility story's stderr counterpart.
* Integration tests on the fake Graph: happy path, transient→retry→success, permanent→error folder, poison→dead-letters, and crash-recovery (side effect done but move not — reprocess must no-op via the handler's claim).

**Done when:** against the real mailbox, a REDCap-style message sent by hand flows to `{slug}/completed` via the built-in handler, and the poison/retry tests pass on the fake.

## 8. `rah reprocess`

* Explicit selection required (no bare "replay everything by accident"): `--route SLUG` and/or `--folder dead-letters|error`, with `--all` as the deliberate big hammer. Settle the brief's open question here, including how replay interacts with max-age.
* Resets machine state (retries-left, retry-time), moves back to inbox; the watcher does the rest. No processing logic in this command.
* `--dry-run` prints what would move.

**Done when:** a dead-lettered message replays end-to-end through a running `watch`.

## 9. Deployment and docs

* systemd unit (foreground service, `StateDirectory`, `LoadCredentialEncrypted`) plus a documented bootstrap: create service user, encrypt secrets, `rah auth`, `rah init`, enable service.
* Example deployment project (`our-rah` skeleton) and example handler-package skeleton demonstrating the entry-point registration and git-pin pattern from the brief.
* README and handler-author guide.

**Done when:** a fresh-box bootstrap has been rehearsed start to finish from the docs alone.

## Cross-cutting

* Version `0.x` with semver discipline on the handler API from step 5 onward; keep a changelog.
* Nothing sensitive in logs at any level — message subjects may carry participant-adjacent data, so DEBUG logs the `internet_message_id` and slug, not bodies.
