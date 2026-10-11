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

Install those calls as agent-host session hooks instead of running them by
hand. Installation is opt-in because it edits agent configuration outside the
archive; every edited file is backed up to a timestamped `.bak` first and
rewritten atomically.

```bash
retro capture hooks status                 # per-host installation state
retro capture hooks install --dry-run      # report planned edits only
retro capture hooks install                # all supported hosts
retro capture hooks install --host claude  # one host (repeatable)
retro capture hooks uninstall              # remove only Retro's entries
retro setup --capture-hooks                # opt in during global setup
```

| Host | File | Events |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` (`CLAUDE_CONFIG_DIR`) | `SessionStart`, `SessionEnd` |
| Codex CLI | `~/.codex/hooks.json` (`CODEX_HOME`) | `SessionStart`, `SessionEnd` |
| GitHub Copilot CLI | `~/.copilot/hooks/retro-capture.json` (`COPILOT_HOME`) | `sessionStart`, `sessionEnd` |

Hook entries are merged into existing configuration, identified by the
`retro capture` command they run, and replaced rather than duplicated on
re-install. The installed command invokes the hidden `retro capture hook`
entry point, which reads the host's JSON payload on stdin, extracts the session
id and working directory, and writes the sidecar. It is fail-open: it never
exits non-zero, never writes to stdout (Claude Code injects `SessionStart`
stdout into the model context), skips directories outside a Git repository, and
appends failures to `capture-hooks.log` in the Retro log directory.

`retro doctor` reports the per-host hook state and the share of recent
Git-backed sessions that carry `repo_start.json`.

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

### Source kinds and sampling

`taskset select --sources sessions|commits|both` chooses where sources come
from. `sessions` (default) uses captured rollouts, which need clean-start proof.
`commits` mines non-merge commits from the repositories in your catalog (or the
repeatable `--commit-repo`): the first parent is an exact base, so commit
sources never fail with `NO_EXACT_BASE_SHA`. Mining is read-only — `rev-list`,
`show`, `diff` and `cat-file` only, never a checkout in your working tree.

```bash
retro benchmark taskset select --name personal-git-v1 \
  --sources both --commit-window-days 180 \
  --commit-max-files 50 --commit-max-lines 2000 \
  --strategy stratified --seed 0 --limit 40 --max-per-repo 10
```

Commit gates: a commit must change a non-test source file and must not be a
merge (`--commit-include-merges` opts in, anchored on the first parent), a
revert, a docs/lockfile/vendored-only change, or larger than
`--commit-max-files`/`--commit-max-lines`. Rejections use
`COMMIT_MERGE_COMMIT`, `COMMIT_REVERT_COMMIT`, `COMMIT_NO_SOURCE_CHANGE`,
`COMMIT_TOO_LARGE` and `OUTCOME_NOT_DURABLE`. When a captured session ran in the
same repository, overlaps or precedes the commit by at most
`--commit-link-window-hours` (default 6), and edited files the commit changed,
Retro links it and uses the user's real first prompt as prompt provenance;
otherwise the commit message is the provenance (`mode: commit_message`).

Selection is sampled, not sorted: `--strategy stratified` (default) walks
(repository, work kind, diff-size) strata round-robin with a seeded order that
puts informative outcomes — corrected and failed-then-fixed sources — ahead of
clean successes, which rank ahead of unlabeled ones. `--seed` makes that order
reproducible, `--max-per-repo` caps one repository's share (defaulting to 25% of
`--limit` when limiting), and `--limit` caps the selection. Sources dropped by a
cap are recorded as `SAMPLED_OUT` with their stratum. `--strategy
lexicographic` restores the original `source_id` ordering. With neither
`--limit` nor `--max-per-repo`, sampling only reorders and drops nothing.

Each selected source records `outcome_labels` with evidence: whether its outcome
commit survived in the branch history, pushback proxies (`friction.*` signals
and human turns after the first edit), and the heuristic `outcome.status`.
`selection.json` reports yield as counts by source kind, outcome label and
rejection code.

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
