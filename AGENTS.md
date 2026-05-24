# AGENTS.md

Instructions for AI coding agents working on this project.

## Project overview

`retro` is a local-first CLI tool that captures Codex and Claude Code agent sessions, normalizes them into a common event schema, evaluates them with signals, mines them into reusable prompt-time memory, and generates a static HTML dashboard. Published on PyPI as `retro-agent-memory`.

## Architecture

The pipeline is layered and composable — each stage reads from disk and can be re-run independently:

```
raw/ (immutable source copy)
  → normalized/ (NormalizedEvent JSONL)
    → signals/ (readings + aggregates)
    → mined/ (memory candidates per method)
    → rendered/ (markdown views)
    → dashboard/ (static HTML)
```

Key packages under `src/retro/`:

| Package | Purpose |
|---------|---------|
| `schema.py` | `NormalizedEvent` dataclass — the canonical event type everything consumes |
| `storage.py` | `Layout` — filesystem path conventions for `rollout-memory/` |
| `utils.py` | Shared helpers: `iter_jsonl`, `event_text`, `iter_messages`, `truncate` |
| `importers/` | Host-specific importers (Claude Code, Codex) that produce normalized events |
| `signals/` | Evaluators that emit readings (numeric/boolean/categorical) about a session |
| `mining/` | Methods that extract reusable memory candidates from normalized events |
| `renderer.py` | Markdown transcript renderer |
| `cli.py` | Typer CLI entry point |

## Plugin system

Signals and mining methods use decorator-based registration. To add a new one:

- **Signal:** Create a function in `signals/heuristics.py` (or a new file) decorated with `@register(name, group=..., kind=..., ...)`. It auto-registers on import.
- **Mining method:** Create `mining/methods/your_method.py` with `@register_method(name, ...)`. Import it in `mining/methods/__init__.py`.
- **Mining filter:** Create `mining/filters/your_filter.py` with `@register_filter(name, ...)`. Import it in `mining/filters/__init__.py`.

## How to build and test

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run tests
.venv/bin/pytest tests/ -v

# Lint
.venv/bin/ruff check src/retro/ tests/

# Type check
.venv/bin/mypy src/retro/

# Smoke test CLI
.venv/bin/retro --help
.venv/bin/retro methods
.venv/bin/retro signal list
```

## Conventions

- **Python ≥ 3.10.** Use `X | None` not `Optional[X]`.
- **No comments unless the why is non-obvious.** Code should be self-documenting.
- **Tests required.** New signals, importers, and mining methods must have test coverage. Tests live in `tests/` and use pytest. Fixture JSONL files are in `tests/fixtures/`.
- **Shared helpers live in `utils.py`.** Don't duplicate `event_text`, `iter_jsonl`, `truncate`, or `iter_messages` locally.
- **`Host` type is defined once** in `schema.py`. Import it from there.
- **`raw/` is immutable.** Re-imports refuse to overwrite unless `--force` is passed.
- **Unknown events are preserved, not dropped.** Importers emit `event_type="unknown"` with the original payload so nothing is silently lost.
- **Everything is evidence-linked.** Signal readings and mined memories carry `event_id` references back to source events.

## Common tasks

### Adding a new event type to an importer

1. Add the type string to `EventType` in `schema.py`.
2. Handle it in the relevant importer's `_normalize()` method.
3. Add a test case with a fixture JSONL line in `tests/fixtures/`.

### Adding a new signal

1. Write the signal function in `signals/heuristics.py` (or `external.py` if it touches the filesystem).
2. Decorate with `@register(name, group=..., kind=..., method=..., description=...)`.
3. Add a test in `tests/test_signals.py`.

### Adding a new mining method

1. Create `mining/methods/your_method.py`.
2. Use `@register_method(name, description=...)`.
3. Import in `mining/methods/__init__.py`.
4. Add tests in `tests/test_mining.py`.

## What to avoid

- Don't add cloud dependencies. This is a local-first tool.
- Don't mutate files in `raw/` after import.
- Don't add `event_text` / `iter_jsonl` / `truncate` helpers locally — use `utils.py`.
- Don't skip tests for new functionality.
- Don't hardcode model pricing — add entries to `dashboard/pricing/litellm-pricing.json` and run `refresh.py`.

## Specs

Design documents live in `specs/`. Read these for deeper context:

- `full_rollout_capture_feature_spec.md` — the capture contract
- `rollout_signals_spec.md` — signal taxonomy and aggregation
- `rollout_mining_methods.md` — mining method catalog
- `rollout_dashboard_spec.md` — dashboard design
- `ccusage_comparison_spec.md` — how retro compares to ccusage
