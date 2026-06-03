# CLI Reference

## Discovery And Import

```bash
retro list [--host claude|codex] [--limit 50]
retro import claude --session-id <id>
retro import claude --latest
retro import claude --all
retro import codex --thread-id <id>
retro import codex --latest
retro import codex --all
retro import all [--limit-per-host 20]
```

## Rendering And Inspection

```bash
retro render claude <session-id>
retro render codex <thread-id>
retro show claude <session-id>
retro show codex <thread-id>
retro analyze
```

## Signals

```bash
retro signal list [--group activity|outcome|cost|risk]
retro signal run [--host claude|codex] [--session-id <id>] [--signal <name,name>]
retro signal show claude <session-id>
retro signal show codex <thread-id>
```

## Mining

```bash
retro methods
retro mine codex <thread-id>
retro mine claude <session-id> --method skill_pro
retro mine codex <thread-id> --method all --filter risk_aware
retro mine '*' '*' --method all --filter risk_aware
```

Registered methods:

- `reme_refine_poc`
- `skill_pro`
- `memp_procedural`
- `codex_headless`

Registered filters:

- `risk_aware`

## Memory

```bash
retro memory init
retro memory reindex
retro memory doctor
retro memory import-authored <dir>
retro memory retrieve --query "..." --cwd /path/to/repo
retro memory weave --query "..." --cwd /path/to/repo
retro memory update-utility --memory-id <id> --reward 0.8 --session-id <session-id>
```

## Dashboard

```bash
retro dashboard build
retro dashboard build --mode calculate
retro dashboard build --mode display
retro dashboard view
```

Cost modes:

- `auto`: use embedded provider cost when present, otherwise calculate from tokens.
- `calculate`: always calculate from token counts.
- `display`: only display embedded provider cost.
