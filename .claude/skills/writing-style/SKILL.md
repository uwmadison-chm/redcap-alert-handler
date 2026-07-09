---
name: writing-style
description: Load before writing any extended prose — README or docs copy, design documents, long docstrings, user-facing messages. Also reference when delegating writing tasks to subagents.
---

Before writing extended prose in this project:

1. Read `writing_style/TROPES.md` — a list of LLM-generated writing tropes to avoid.
2. Read `writing_style/nate_example_copy.md` — an example of Nate's own writing; match this voice.

When delegating a writing task to a subagent, include in its prompt that it must
read both files above before drafting anything.

This applies to prose a human will read. It does not apply to code, config,
or one-line comments.
