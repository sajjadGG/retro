# Architecture

`retro` is a layered local artifact pipeline.

```text
raw/ -> normalized/ -> signals/ -> mined/ -> memories/ -> dashboard/
```

## Layers

| Layer | Purpose |
| --- | --- |
| `raw/` | Immutable source copies of Claude Code and Codex session logs. |
| `normalized/` | Common `NormalizedEvent` JSONL stream. |
| `signals/` | Evidence-linked readings and aggregates. |
| `mined/` | Prompt-time memory candidates per method. |
| `memories/` | Canonical memory records, lifecycle events, and derived SQLite index. |
| `rendered/` | Human-readable markdown transcripts. |
| `dashboard/` | Static HTML dashboard and generated data JSON. |

## Source Of Truth

Flat files are canonical:

- raw source captures are immutable;
- normalized events are replayable;
- mined artifacts remain inspectable;
- `memories/items.jsonl` and `memories/events.jsonl` own memory state.

Derived artifacts can be rebuilt:

- `memories/index.sqlite`;
- `dashboard/data/rollouts.json`;
- `dashboard/index.html`.

## Key Modules

| Module | Role |
| --- | --- |
| `src/retro/schema.py` | Canonical event dataclasses and readers. |
| `src/retro/storage.py` | Filesystem layout conventions. |
| `src/retro/importers/` | Host-specific importers. |
| `src/retro/signals/` | Signal registry and evaluators. |
| `src/retro/mining/` | Mining method and filter registries. |
| `src/retro/memory_store.py` | SQLite schema, reindex, retrieval, authored import, utility updates, weave. |
| `src/retro/renderer.py` | Markdown transcript rendering. |
| `src/retro/cli.py` | Typer command surface. |

## Plugin Pattern

Signals and mining methods self-register with decorators.

Signals use `@register(...)` in `src/retro/signals/`.

Mining methods use `@register_method(...)` in `src/retro/mining/methods/` and must be imported in `src/retro/mining/methods/__init__.py`.

Mining filters use `@register_filter(...)` in `src/retro/mining/filters/` and must be imported in `src/retro/mining/filters/__init__.py`.
