# Changelog

## Unreleased

### Added

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

### Changed

- `retro dashboard build` takes `--mode auto|full` (shard reuse) instead of the old
  cost modes, plus `--no-publish`; `retro dashboard view` reads the same inputs and
  accepts `--root`, `--limit` and `--interactive/--no-interactive`.

### Removed

- `retro.dashboard_build`, `retro.dashboard_experiments`, `retro.dashboard_publish`,
  `retro.dashboard_terminal` and `retro dashboard experiments`: the embedded-HTML
  builder re-read every normalized event and emitted gigabyte-scale pages.
- `retro quest` and `retro.quest`: the streak and XP state duplicated metrics by
  re-reading normalized events, required an LLM call to generate quests, and ran on
  every CLI invocation. Streaks and active days are now computed from the catalog and
  shown on the dashboard Overview.

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
