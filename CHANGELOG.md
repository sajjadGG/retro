# Changelog

## Unreleased

### Added

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
