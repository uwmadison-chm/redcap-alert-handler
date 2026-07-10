---
name: next-step
description: The working rhythm for implementing the next step of IMPLEMENTATION_PLAN.md. Load when starting a numbered step (config models, rah auth, graph client, ...).
---

Each step of IMPLEMENTATION_PLAN.md follows the same loop:

1. Read that step's section in full, including its **Done when**, plus the
   shared sections up top (Universal CLI Options; Log message conventions;
   Comments and Docstrings — the file-header template lives there). Skim the
   matching part of DESIGN_BRIEF.md. If the two documents disagree, the plan
   is usually more current — but raise the conflict with Nate before building
   on either version; he may want to recover the reasoning.

2. Development is red-green: write the step's tests first, watch them fail,
   then implement. Table-driven tests where the step calls for them; fixture
   files in `tests/data/`, not inline strings.

3. Build via a lower-power subagent (sonnet for routine work, opus for hard or
   failure-prone parts) with the plan section and conventions in its prompt,
   including the writing-style skill's file-reading requirement for any prose.
   Review the result yourself, file by file.

4. Verify per the `verify` skill: bare `just` green, and the changed behavior
   exercised through `uv run rah ...`.

5. Sync docs: if the step settled something the plan left open (it marks these),
   update IMPLEMENTATION_PLAN.md; touch DESIGN_BRIEF.md only where it directly
   contradicts.

6. Commit only when Nate asks; he always pushes himself.

Version facts worth keeping (checked 2026-07): typer >= 0.26 vendors Click —
never import click or add it as a dependency; ty is pre-1.0 and its config
surface moves; new runtime deps (msal, httpx, humanfriendly) get added in the
step that first imports them, not before.
