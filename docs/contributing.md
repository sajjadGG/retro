# Contributing

## Development Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/ -q
.venv/bin/mypy src/retro/
.venv/bin/retro dashboard build
```

## Conventions

- Python 3.10+.
- Prefer `X | None` over `Optional[X]`.
- Keep raw captures immutable.
- Preserve unknown events as `event_type="unknown"` rather than dropping them.
- Use shared helpers from `src/retro/utils.py`.
- Add tests for new importers, signals, mining methods, and memory backend behavior.
- Keep cloud or native dependencies optional unless there is a strong local-first reason.

## Add A Signal

1. Add a function in `src/retro/signals/heuristics.py` or `external.py`.
2. Decorate it with `@register(...)`.
3. Add tests in `tests/test_signals.py`.

## Add A Mining Method

1. Create `src/retro/mining/methods/<name>.py`.
2. Decorate it with `@register_method(...)`.
3. Import it in `src/retro/mining/methods/__init__.py`.
4. Add tests in `tests/test_mining.py`.

## Add A Mining Filter

1. Create `src/retro/mining/filters/<name>.py`.
2. Decorate it with `@register_filter(...)`.
3. Import it in `src/retro/mining/filters/__init__.py`.
4. Add focused tests.

## Update Docs

When adding a public command or workflow, update:

- `README.md`;
- the matching page under `docs/`;
- any relevant design spec under `specs/`.
