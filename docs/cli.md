# CLI Reference

## Discovery And Import

```bash
retro list [--host <registered-host>] [--limit 50]
retro extensions list
retro extensions doctor
retro import claude --session-id <id>
retro import claude --latest
retro import claude --all
retro import codex --thread-id <id>
retro import codex --latest
retro import codex --all
retro import copilot --latest
retro import <extension-host> --latest
retro import all [--limit-per-host 20]
retro import renormalize [--host <host>] [--session <id>] [--since <iso>] [--dry-run] [--no-render]
```

`retro import renormalize` rebuilds `normalized/` (and rendered transcripts)
from the archived `raw/` copies when a host's normalizer version changes. It
never reads the original agent folders and never modifies `raw/`.

`retro import all` and `retro sync` include every healthy registered source
provider. Extension host commands are added from installed `retro.sources`
entry points. Provider collisions and incompatible API versions are reported
by `retro extensions doctor`.

## Rendering And Inspection

```bash
retro render claude <session-id>
retro render codex <thread-id>
retro show claude <session-id>
retro show codex <thread-id>
retro analyze
```

## Session Catalog

Read commands refresh changed catalog records before displaying them:

```bash
retro sessions list \
  [--host <host>] [--agent <agent>] [--client <client>] \
  [--project <id-or-name>] [--tag <tag>] \
  [--excluded|--included] [--automated|--interactive] \
  [--since <ISO-8601>] [--search <text>] [--limit 100] [--json]
retro sessions show <host/session-id> [--json]
retro sessions explain <host/session-id> [--json]
retro sessions tag <host/session-id> <tag>
retro sessions untag <host/session-id> <tag>
retro sessions set-project <host/session-id> <project>
retro sessions set-project <host/session-id> --clear
```

`explain` reports the configured exclusion tags, their intersection with the
session's tags, every tag source, markers and annotations, and the one project
decision/evidence that won.

Manage classification rules with a JSON object or JSON file:

```bash
retro rules list [--json]
retro rules add '{"id":"aime","match":{"prompt":"(?i)^below is a math problem"},"tags":["ignore"]}'
retro rules remove aime
retro rules enable builtin.empty
retro rules disable builtin.empty
retro rules test [<json-or-file>] [--samples 5] [--json]
```

`rules test` reads the existing catalog and never writes. Manage cross-path and
cross-agent project identity:

```bash
retro projects list [--json]
retro projects show <project-id> [--json]
retro projects merge <source-id> <target-id>
retro projects rename <project-id> <display-name>
```

All catalog/config write commands use the per-user archive lock. Project merges
persist in `projects.aliases`; renames persist in `projects.names`.

## Signals

```bash
retro signal list [--category usage|activity|outcome|friction|efficiency|collaboration|risk|quality|work] [--describe]
retro signal run [--host <host>] [--session-id <id,id>] [--force] [--include-scratch]
retro signal summary [--signal <id,id>] [--json]
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
retro mine codex <thread-id> --include-excluded
```

The `*` mining selector scans every host represented in normalized artifacts,
including extension hosts. Catalog-excluded sessions are skipped unless
`--include-excluded` is explicit.

Registered methods:

- `reme_refine_poc`
- `skill_pro`
- `memp_procedural`
- `codex_headless`

Registered filters:

- `risk_aware`

## Git-Backed Task And Scorer Benchmarks

Capture the exact clean commit state from the coding host's lifecycle hook.
These commands must run before the first agent action and after the final one;
Retro never reconstructs a missing base from commit timestamps.

```bash
retro capture start --host codex --session-id <id> --cwd /path/to/repo
retro capture end --host codex --session-id <id> --cwd /path/to/repo
```

Build and evaluate a taskset:

```bash
retro benchmark taskset select --name personal-git-v1 --host codex --session-file sessions.txt
retro benchmark taskset bundle --name personal-git-v1 --selected-only
retro benchmark taskset build --name personal-git-v1 \
  --ghostlab-bin /path/to/ghostlab \
  --task-definer-agent agents/task-definer.json \
  --scorer-builder-agent agents/scorer-builder.json \
  --scorer-auditor-agent agents/scorer-auditor.json
retro benchmark taskset run --name personal-git-v1 \
  --agent candidate-agents/codex.json \
  --seeds 0,1,2 \
  --ghostlab-bin /path/to/ghostlab
retro benchmark taskset report --name personal-git-v1 --eval latest
```

Selection requires a validated project environment. Provide a prevalidated
`retro-project-environment-v1` contract with `--environment-file`, or let Retro
resolve and validate one with `--environment-config`, repository container
metadata, CI commands plus `--ci-base-image`, or an explicit
`--repolaunch-bin`. Container validation runs against fresh base and outcome
trees twice with network disabled. Image builds use no network unless both a
destination `--build-network-allowlist` and an externally egress-filtered
`--build-network-name` are supplied.

During `taskset run`, Retro passes the published digest-pinned image and setup
argument array to Ghostlab. A candidate agent without its own runtime image runs
directly in that image. A local candidate-runtime Dockerfile may add the pinned
agent CLI only when its first `FROM` exactly equals the published image; Retro
requires a single stage, hashes the complete build context and every resolved
runtime input into the attempt identity, and executes from a verified temporary
snapshot. All other candidate image declarations are overridden by the
published image.

Selection and construction write explicit rejection records. Catalog-excluded
sources receive `EXCLUDED_BY_CATALOG` with reason `excluded by catalog`. A
rollout with no replayable goal is a valid zero-task result. Scorer or harness
failures remain invalid attempts and are never converted into numeric zeroes.

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
