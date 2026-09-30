# Architecture

`retro` is a layered local artifact pipeline.

```text
raw/ -> normalized/ -> signals/ -> mined/ -> memories/ -> dashboard/
```

## Layers

| Layer | Purpose |
| --- | --- |
| `raw/` | Immutable or revision-preserved source copies from registered hosts. |
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

## Source Discovery

Built-in and installed source providers register through the `retro.sources`
Python entry-point group. Retro discovers entry points without importing their
modules, checks extension API compatibility when loaded, and routes list,
import, sync, analysis, and derived rebuilds through validated host IDs.

Imports execute against a staging `Layout`. Retro validates that a provider
wrote the expected raw directory and normalized JSONL, then replaces both
published artifacts atomically while holding the archive lock. Providers
cannot select arbitrary publication paths through their returned
`ImportResult`.

Built-in providers exclude synthetic Ghostlab evaluation traffic before it enters the
archive. Codex sessions are identified by `session_meta.originator: ghostlab`;
Copilot CLI sessions use Ghostlab's reserved UUID prefix. The marker is attached
to session metadata rather than prompts, so excluding it does not change the
recorded conversation.

## Key Modules

| Module | Role |
| --- | --- |
| `src/retro/schema.py` | Canonical event dataclasses and readers. |
| `src/retro/storage.py` | Filesystem layout conventions. |
| `src/retro/sdk/` | Stable extension API, source contracts, and lazy entry-point registry. |
| `src/retro/extensions.py` | Built-in adapters, diagnostics, staging, validation, and publication. |
| `src/retro/importers/` | Host-specific importers. |
| `src/retro/signals/` | Signal registry and evaluators. |
| `src/retro/mining/` | Mining method and filter registries. |
| `src/retro/memory_store.py` | SQLite schema, reindex, retrieval, authored import, utility updates, weave. |
| `src/retro/renderer.py` | Markdown transcript rendering. |
| `src/retro/cli.py` | Typer command surface. |

## Plugin Pattern

Source packages expose one adapter class through `retro.sources`. The class
accepts a `Layout`, returns `SessionDescriptor` objects from `discover()`, and
returns an `ImportResult` from `import_session()`. Extension packages declare
API version `1` and import public types from `retro.sdk`.

Signals and mining methods self-register with decorators.

Signals use `@register(...)` in `src/retro/signals/`.

Mining methods use `@register_method(...)` in `src/retro/mining/methods/` and must be imported in `src/retro/mining/methods/__init__.py`.

Mining filters use `@register_filter(...)` in `src/retro/mining/filters/` and must be imported in `src/retro/mining/filters/__init__.py`.
