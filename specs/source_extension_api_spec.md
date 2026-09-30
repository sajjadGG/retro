# Source Extension API Specification

Status: implemented
Extension API: `1`

## Goal

Allow a tool-specific source package to participate in Retro's local pipeline
without editing core, while preserving canonical schemas, evidence links,
archive locking, atomic publication, and downstream rebuildability.

## Scope

Version `1` supports source adapters only:

```text
discover -> capture + normalize -> raw/ + normalized/ -> core pipeline
```

Signals, mining methods, dashboards, and exporters are not external extension
points in version `1`. Existing in-process signal and mining registries remain
unchanged.

## Package discovery

Source packages register one class per host:

```toml
[project.entry-points."retro.sources"]
opencode = "retro_opencode:OpenCodeSource"
```

Entry points are enumerated lazily. Listing providers does not import optional
packages. Loading occurs when a host command or diagnostics requires it.

Each package declares:

```python
RETRO_EXTENSION_API = "1"
```

The declaration may live on the provider class or its defining module.

## Public types

Extensions import from `retro.sdk`:

- `Layout`
- `SessionDescriptor`
- `ImportResult`
- `SourceAdapter`
- `NormalizedEvent`
- `RawRef`
- `Host`
- `Actor`
- `EventType`
- `write_events`
- `read_events`
- host and session ID validators

Internal importer, sync, dashboard, memory, and configuration modules are not
part of the extension compatibility promise.

## Adapter contract

```python
class SourceAdapter(Protocol):
    host: str
    display_name: str

    def discover(self) -> list[SessionDescriptor]: ...

    def import_session(
        self,
        *,
        identifier: str,
        force: bool = False,
    ) -> ImportResult: ...
```

The adapter constructor accepts a `Layout` as its first argument.

`discover()` returns newest-first `SessionDescriptor` values. `host` must
match the entry-point name. `source_path` identifies the primary local source
when one exists. `metadata[\"raw_filename\"]` identifies the corresponding
primary raw filename so incremental sync can preserve rewritten captures.

`import_session()` writes only below the supplied layout. It copies source
evidence into `raw/<host>/<session-id>/`, writes normalized events to
`normalized/<host>/<session-id>.events.jsonl`, and returns those exact paths.

## Identifiers

Host IDs match:

```text
^[a-z][a-z0-9-]*$
```

Session IDs match:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$
```

The restrictions keep all artifact paths single-component and portable.

## Managed publication

Retro creates a staging layout inside the target archive. If a public raw
capture already exists, Retro copies it into staging first so providers can
preserve sidecars and decide whether the live source advanced.

The selected provider runs only against staging. Retro then verifies:

- returned host and session ID equal the request;
- returned raw and normalized paths equal the staging layout paths;
- the raw directory and normalized JSONL exist.

Publication backs up any current raw and normalized targets, replaces both
with staged artifacts using same-filesystem atomic renames, and removes the
backups only after both replacements succeed. A failed second replacement
rolls the first replacement back.

Manual imports acquire the archive lock. Periodic sync already owns the lock
and invokes managed imports without reacquiring it.

## Collisions and failures

No precedence rule exists. If multiple providers claim one host, loading that
host fails and `retro extensions doctor` reports every colliding record.

An import failure is scoped to its provider and session. Periodic sync records
the failure and continues with other providers. Optional provider import
failures do not prevent built-ins from operating.

## Dynamic downstream hosts

General pipeline stages enumerate normalized artifact host directories rather
than a closed host literal:

- render and archive-derived rebuilds;
- signals;
- mining with the `*` selector;
- command/tool analysis;
- static and terminal dashboards.

Git-backed benchmark and task-scorer inputs remain explicitly restricted to
the built-in host set until their provenance contracts are extended.

## Reference provider

`adapters/retro-opencode` is the API `1` reference. It supports fixture-backed
OpenCode JSONL layouts and preserves unknown or malformed records. It is not a
claim of compatibility with untested OpenCode database or sidecar formats.

## Acceptance criteria

- Built-in Claude Code, Codex, and VS Code Copilot behavior remains compatible.
- A fake provider can be discovered without import, diagnosed, staged, and
  published through the public contract.
- The installed OpenCode package can list and import a fixture through the
  generated `retro import opencode` command.
- Extension sessions participate in sync, analysis, signals, mining, archive
  rebuilds, and dashboards.
- Existing public artifacts survive a provider or publication failure.
- Core and adapter tests, Ruff, mypy, package builds, and Python 3.9-3.13 CI
  pass.
