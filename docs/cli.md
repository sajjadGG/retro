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

Selection and construction write explicit rejection records. A rollout with no
replayable goal is a valid zero-task result. Scorer or harness failures remain
invalid attempts and are never converted into numeric zeroes.

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
