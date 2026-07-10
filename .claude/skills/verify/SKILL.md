---
name: verify
description: How to check and exercise rah in this repo — run before calling any change done, and when asked to run or demo the app.
---

The whole gate is one command:

```
just
```

That's the default `check` recipe: `uv sync -q`, then reformat, lint, typecheck,
and tests (pytest, parallel). It mutates the tree (the formatter fixes files);
that's intended. The non-mutating variant is `just safe-check` — it's what CI
runs, so if you want to predict CI, run that.

A change isn't done until bare `just` is green AND you've exercised the changed
behavior through the real CLI, not just the tests:

```
uv run rah --help
uv run rah <subcommand> ...
```

Anything touching auth or the Graph API can only be fully verified against the
real tenant/mailbox by Nate; verify against the fake Graph transport and test
fixtures, then say plainly which parts ran for real and which didn't.

Conventions that trip up verification:

- Tests run under `pytest -n auto` (separate processes). Don't write tests that
  depend on shared mutable state or ordering.
- Config fixtures belong in `tests/data/` as files, not inline strings.
- Nothing sensitive in logs at any level; DEBUG logs message IDs and slugs,
  never bodies or subjects.
