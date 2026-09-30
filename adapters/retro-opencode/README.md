# retro-opencode

OpenCode source adapter for [Retro](https://github.com/sajjadGG/retro).

This package implements Retro extension API `1` and registers the `opencode`
host through the `retro.sources` entry-point group.

## Development install

Install Retro first, then this adapter:

```bash
python -m pip install -e ../..
python -m pip install -e ".[dev]"
retro extensions doctor
```

Set `OPENCODE_HOME` to one or more comma-separated OpenCode roots. When unset,
the adapter scans `~/.opencode`.

Supported fixture-backed layouts:

```text
<root>/sessions/**/*.jsonl
<root>/projects/<project>/sessions/**/*.jsonl
```

Then use the normal Retro commands:

```bash
retro list --host opencode
retro import opencode --latest
retro sync
```

Unknown and malformed source records are preserved as `unknown` normalized
events with raw evidence references. Existing raw captures are not replaced
unless the source has advanced or `--force` is supplied.
