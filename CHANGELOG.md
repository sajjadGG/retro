# Changelog

## 0.5.0 - Unreleased

### Added

- Frozen trajectory-derived benchmark suites with evidence-gated curation,
  related-task/repository grouping, explicit train/validation/test splits,
  exact-replay versus opted-in anchored-reconstruction provenance, and
  immutable evaluation cohorts.
- `retro benchmark taskset curate|export|reset|reward`, Git-free exact-tree
  repository episodes, source-balanced training sampling, separately exported
  hidden verifiers and attested terminal rewards that keep infrastructure
  errors non-numeric. Exports reference pinned images; they do not train models.
- Benchmark dashboard views for selection/build/curation yield, provenance,
  mutation validation, audits, cohort-bound agent comparisons, reward heatmaps,
  per-seed errors, conditional pass@k and source-bootstrap score intervals.
- Separate Tasks, Evals datasets and Experiments pages, dismissible verification
  warnings, and opt-in localhost job controls/private grader input-code-output
  inspection. Static exports remain read-only and omit hidden grader code.
- `retro experiment create|list|show|run|cleanup`, immutable pinned job setup,
  per-trial progress and grades, default experiment-trace exclusion, and exact
  owned-artifact cleanup without pruning task bundles or global Docker data.
- Explicit relocated-checkout support for task selection and bundling without
  mutating historical captures or inventing missing start-state proof.

- Session catalog (`catalog/sessions.jsonl`): one record per archived session with
  agent, client surface (e.g. `codex-exec`, `copilot-vscode-agent`, `claude-cli`),
  interactive vs automated, subagent parent links, first prompt, timing, Git remote,
  and a consolidated project id. The same repository reached through clones,
  worktrees, symlinked prefixes, or different agents now shares one project
  (`github.com/<owner>/<repo>`); temp-dir sessions collapse into `scratch:temp`.
- Ignoring noise: `#retro:ignore`, `#retro:keep`, `#retro:tag=<name>` and
  `#retro:project=<id>` markers in your own prompts, `.retroignore` files in a
  working directory or its parents, user rules matching host/client/cwd/project/
  remote/branch/model/title/prompt/counts/dates, built-in rules for temp dirs,
  empty sessions, automation, and subagents, plus per-session annotations.
  `retro sessions`, `retro rules` (with dry-run `test`), and `retro projects`
  explain and manage every decision; excluded sessions stay archived but never
  feed signals, rendering, mining, memory, benchmarks, or dashboards.
- Signals v2: a single-pass `SessionProfile` feeding 50 registered signals in nine
  categories (usage, activity, outcome, friction, efficiency, collaboration, risk,
  quality, work) with titles, units, polarity, method, and versions; token and
  cost accounting for every host; an incremental store keyed by catalog
  fingerprint, signal-set version, and pricing snapshot; and summary helpers for
  rates, medians, p90, weekly trends, and guarded comparisons.
- Rebuilt the dashboard as `retro.dashboard`: a static, offline site driven by the
  session catalog and the signal store, with Overview, Projects, Sessions explorer,
  Session detail, Signals and Data-quality pages, URL-hash deep links, light/dark
  themes and keyboard navigation. Session rows ship as a compact fact table in
  `data/*.js` shards that the browser aggregates, per-session detail loads on demand
  from fingerprint-keyed buckets, and `index.html` is a two-kilobyte shell. Excluded
  sessions — 98% of a real archive — ship as a separate shard set that loads only when
  asked for, so the first load stays around 465 KB and the whole directory 28 MB.
  Sessions without a `usage.cost_usd` signal render as "—" with the reason from
  `quality.cost_basis` instead of "$0.00", and never count as zero in averages.
- Normalization v3: `usage`, `lifecycle`, and `metadata` event types, zero `unknown`
  events for every known Codex, Claude Code, and Copilot record kind, complete
  tool call/result pairing with normalized success, error, and exit codes,
  inherited-and-flagged timestamps, de-duplicated payloads (13% smaller than
  before), and `retro import renormalize` to rebuild an archive from `raw/`.
- Staged `retro sync`: `capture → catalog → render → signals → memory → dashboard`
  with per-stage status, timing, and pending work persisted in
  `<archive>/state/sync.json`.
- External-volume support: unmounted archives are skipped without creating
  directories, macOS privacy (TCC) denials name the `Python.app` to grant Full
  Disk Access, and `retro doctor` reports volume, access, lock, stage, legacy
  data, and stale staging health.
- `retro archive repair-refs` rewrites absolute archive-internal references.

### Changed

- `retro dashboard build` takes `--mode auto|full` (shard reuse) instead of the old
  cost modes, plus `--no-publish`; `retro dashboard view` reads the same inputs and
  accepts `--root`, `--limit` and `--interactive/--no-interactive`.

- The archive lock and sync state live in `<archive>/state/`; legacy
  `last-sync.json` is migrated once. Config edits use a separate config lock.
- Signals are stored as `signals/definitions.json` plus one compact line per
  included session in `signals/sessions.jsonl` (600 MB → 1.2 MB on a 31k-session
  archive), replacing `readings.jsonl` and `aggregates.json`.
- Capture skips staging for unchanged sessions (no-change capture went from
  hours to under a minute on a 31k-session archive), recovers publications
  interrupted between the raw and normalized swaps, and only renders included
  sessions.
- Scheduled syncs keep earlier stages' progress when a later stage fails and
  respect the derived interval after clean passes.
- The LaunchAgent forwards archive, dashboard, and source-root overrides.
- Rule regexes with nested unbounded repetition are rejected.

### Removed

- `retro.dashboard_build`, `retro.dashboard_experiments`, `retro.dashboard_publish`,
  `retro.dashboard_terminal` and `retro dashboard experiments`: the embedded-HTML
  builder re-read every normalized event and emitted gigabyte-scale pages.
- `retro quest` and `retro.quest`: the streak and XP state duplicated metrics by
  re-reading normalized events, required an LLM call to generate quests, and ran on
  every CLI invocation. Streaks and active days are now computed from the catalog and
  shown on the dashboard Overview.

- `retro.analyzer`, `retro.trajectory`, and the v1 signal modules
  (`signals/heuristics.py`, `trajectory.py`, `external.py`, `runner.py`, `base.py`);
  their useful ideas are folded into signals v2.

### Fixed

- Scheduled syncs that re-computed every signal for hours after a dashboard
  failure and never saved their state.
- Capture re-discovering every source session for each import (quadratic I/O).
- Absolute archive paths in Copilot execution references and task-scorer
  manifests, which broke after moving the archive.
- Codex messages repeated across turns being collapsed into one.

## 0.4.0 - 2026-09-30

### Added

- Versioned `retro.sources` extension API with lazy entry-point discovery,
  `retro extensions list/doctor`, managed staged imports, and atomic raw plus
  normalized publication.
- Dynamic extension-host support across periodic sync, signals, mining,
  archive rebuilds, analysis, and static/terminal dashboards.
- Fixture-backed `retro-opencode` reference adapter with Python 3.9-3.13 CI
  coverage.
- Bounded dashboard detail payloads with exact full-archive aggregates,
  on-demand transcript loading, and two-generation atomic retention, avoiding
  multi-gigabyte dashboard copies for large archives.
- Git-backed rollout tasksets with exact repository-state provenance,
  content-addressed source bundles, hidden executable scorers, resumable
  Ghostlab agent runs, and source-normalized numeric reports.
- GhostLab/OpenShell execution for time-consistent benchmark tasks, with
  independent sandboxes, exact repository snapshots, constrained Copilot tools
  and egress, a digest-pinned base image, private runner traces, and immutable
  scoring.

## 0.3.0 - 2026-08-27

### Added

- Capture for VS Code GitHub Copilot Chat, Copilot CLI, and Agent Host rollouts.
- Reconstruction of VS Code JSONL mutation logs and preservation of Copilot
  tools, subagents, hooks, permissions, usage, and editing sidecars.
- Per-user global archive configuration and portable evidence references.
- Checksummed, resumable archive migration with conflict quarantine and
  rollback-safe compatibility links.
- Incremental `retro sync` and explicit macOS LaunchAgent management.
- Atomic dashboard generations, raw revisions, archive locking, and low-disk
  safeguards.
- Python 3.9 support and CI coverage through Python 3.13.

### Changed

- Default storage is the platform per-user archive rather than a
  current-working-directory `rollout-memory/`.
- Scheduled capture runs independently from expensive derived processing.
  Capture defaults to every 15 minutes and derived refreshes to every 6 hours.
- Large transcript rendering and trajectory signal evaluation are substantially
  faster.

### Installation

```bash
python3 -m pip install --upgrade retro-ai
retro --help
```

The PyPI distribution is `retro-ai`; it installs the `retro` console command.
