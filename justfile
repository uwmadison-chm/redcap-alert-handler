set shell := ["bash", "-euo", "pipefail", "-c"]

default: check

sync:
    uv sync -q

test:
    uv run pytest -n auto -q

lint:
    uv run ruff check -q .

reformat:
    uv run ruff format -q .

format-check:
    uv run ruff format --check -q .

typecheck:
    uv run ty check -q

safe-check: format-check lint typecheck test

check: sync reformat lint typecheck test
