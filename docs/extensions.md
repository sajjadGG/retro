# Source extensions

Retro extension API `1` supports installable source adapters. A source adapter
owns host-specific discovery, raw capture, and normalization. Retro core owns
artifact paths, staged publication, locking, downstream processing, and the
CLI.

## Inspect providers

```bash
retro extensions list
retro extensions doctor
```

`list` reads entry-point metadata without importing optional packages.
`doctor` loads each provider and verifies:

- the package declares a supported `RETRO_EXTENSION_API`;
- the entry-point name matches the provider's validated host ID;
- the provider has a display name, `discover()`, and `import_session()`;
- no built-in or installed package claims the same host name.

One broken extension does not prevent unrelated providers from loading.
Collisions fail closed instead of choosing a package by installation order.

## Install the OpenCode reference adapter

The repository includes a fixture-backed reference package:

```bash
python -m pip install -e "./adapters/retro-opencode"
retro extensions doctor
```

Set `OPENCODE_HOME` when the source is not under `~/.opencode`:

```bash
export OPENCODE_HOME=/path/to/opencode-home
retro list --host opencode
retro import opencode --latest
```

See the adapter's `COMPATIBILITY.md` for supported layouts and known gaps.

## Implement a provider

An adapter class accepts a `retro.sdk.Layout` and implements the public source
protocol:

```python
from retro.sdk import ImportResult, Layout, SessionDescriptor

RETRO_EXTENSION_API = "1"


class ExampleSource:
    host = "example"
    display_name = "Example Agent"

    def __init__(self, layout: Layout):
        self.layout = layout

    def discover(self) -> list[SessionDescriptor]:
        ...

    def import_session(
        self,
        *,
        identifier: str,
        force: bool = False,
    ) -> ImportResult:
        ...
```

Register it in the package's `pyproject.toml`:

```toml
[project.entry-points."retro.sources"]
example = "example_retro:ExampleSource"
```

Host IDs must match `^[a-z][a-z0-9-]*$`. Session IDs must be path-safe. The
entry-point name and `ExampleSource.host` must match.

## Publication contract

The provider receives a staging `Layout`, not the live archive layout. It must:

1. Copy source evidence into `layout.raw_dir(host, session_id)`.
2. Preserve unsupported records as `event_type=\"unknown\"`.
3. Write `NormalizedEvent` rows to
   `layout.normalized_path(host, session_id)`.
4. Return those exact paths in `ImportResult`.
5. Use artifact-relative `RawRef.path` values such as
   `raw/example/<session-id>/session.jsonl`.
6. Refuse an unchanged existing capture unless `force=True`.

Retro validates the returned host, session, and paths. It then atomically
replaces the public raw directory and normalized JSONL as one managed
publication operation. If capture or normalization fails, the existing public
artifacts remain unchanged.

Provider code must not write signals, mined memories, dashboards, global
configuration, or files outside the supplied layout.

## Compatibility

The API version is independent from the `retro-ai` package version. Providers
should constrain the core release range they test, for example:

```toml
dependencies = ["retro-ai>=0.4.0,<0.5"]
```

Retro supports Python 3.9 and newer. Extension annotations should use
`from __future__ import annotations` when using modern union syntax.
