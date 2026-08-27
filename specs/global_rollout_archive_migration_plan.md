# Global Rollout Archive Migration Plan

## Status

Implemented and executed on 2026-08-27.

- Migration ID: `20260827T064109Z`
- Initial verified merge: 727 sessions, 184,554 events, zero invalid JSONL,
  zero unresolved raw references
- First machine-wide sync: 989 sessions (903 Codex, 33 Claude Code, 53 VS Code
  Copilot)
- Mem compatibility path now links to the global archive
- Original Mem archive retained at
  `~/Library/Application Support/retro/source-backups/20260827T064109Z/rollout-memory.pre-migration`
- Copilot source worktree remains locked and retained
- Scheduler installation is performed only after the implementation is merged
  and installed into a stable per-user runtime

## Goal

Make Retro use one durable archive for the current macOS user from every working
directory, merge all existing rollout data into it without loss, and keep it
updated periodically after Retro is installed.

## Decisions and assumptions

1. `/Users/sajad/Dev/repos/retro` remains the code checkout.
2. `~/Library/Application Support/retro/rollout-memory` becomes the physical
   canonical archive.
3. `/Users/sajad/Dev/repos/Mem/dashboard` remains the canonical generated
   dashboard directory.
4. After verified cutover, `/Users/sajad/Dev/repos/Mem/rollout-memory` becomes
   a compatibility symlink to the physical canonical archive. The folder
   remains available at the path already used for this work, but its data is
   protected from Git worktree cleanup.
5. "Computer-wide" means all supported agent data available to the current
   macOS user. It does not read other login accounts or require root.
6. `pip install` must not silently install a background service. Retro will
   provide one explicit setup command that configures the archive and installs
   a per-user periodic job.
7. "Sync" means local agent stores to the canonical archive, followed by
   incremental derived-artifact refresh. Raw rollouts are not pushed to GitHub
   or any cloud service by default.

The archive must not physically remain inside a Git checkout: `rollout-memory/`
is ignored and could be destroyed by `git clean -fdx`. The compatibility
symlink keeps the requested Mem path while the durable data lives in the
current user's platform data directory.

## Read-only inventory

### Existing archives

| Archive | Hosts | Sessions | Normalized events | Size |
| --- | --- | ---: | ---: | ---: |
| `/Users/sajad/Dev/repos/Mem/rollout-memory` | 648 Codex, 26 Claude Code | 674 | 48,551 | 677 MB |
| Current Copilot worktree archive | 53 VS Code Copilot | 53 | 123,193 | 844 MB |
| `/Users/sajad/Dev/repos/retro/rollout-memory` | none | 0 | 0 | absent |
| Projected canonical archive | all three hosts | 727 | 171,744 | about 1.5 GB |

The two populated archives have zero `(host, session_id)` overlap.

### Mem artifact inventory

| Artifact | Count / size | Migration treatment |
| --- | --- | --- |
| Raw sessions | 674 sessions, 149,713,348 bytes | Authoritative; preserve byte-for-byte |
| Normalized streams | 674 files, 126,258,678 bytes | Preserve events; make raw references portable |
| Rendered transcripts | 674 files, 36,450,877 bytes | Preserve initially; regenerate after verification |
| Mined artifacts | 84 JSON + 84 prompt files, 516,221 bytes | Authoritative derived research output; preserve |
| Memory database | 58 memories, 731 evidence links | Back up exact DB, then rebuild from mined artifacts |
| Signals | 3 files, 822,750 bytes | Back up, then recompute for all 727 sessions |
| Quest state | 1 file, 1,728 bytes | Preserve exactly |
| Headless artifacts | 3 files, 17,009 bytes | Preserve exactly |
| Training artifacts | 88 substantive files, 389,257,494 bytes | Preserve exactly; exclude 4 rebuildable `.pyc` files |
| SWE-bench logs outside archive | 1,228 files, 39,446,315 bytes | Register and migrate under experiments |
| Root evaluation JSON files | 3 files, 71,963 bytes | Register and migrate under experiments |
| Existing generated dashboard | 181 MB | Back up once, then rebuild atomically |

### Integrity findings

- All 674 Mem raw JSONL files parse successfully.
- All 53 Copilot raw JSONL files parse successfully.
- All 727 normalized files are non-empty and internally match their host and
  session identifiers.
- No normalized event streams overlap between the archives.
- The Mem memory database passes `PRAGMA quick_check`.
- The 84 mined JSON files contain 149 candidates. Their 58 unique content
  hashes exactly match the database's 58 memory rows; the other 84 mined files
  are prompt-rendered views.
- The database contains no utility updates, memory lifecycle events, vectors,
  or links that exist only in SQLite.
- Every normalized `raw_ref.path` is currently absolute. Copilot references
  point into the temporary worktree and must be rebased during migration.
- The Mem checkout has uncommitted training code and experiment data. The
  migration must not reset, overwrite, clean, or otherwise modify that work.
- The disk currently has about 12 GiB free. That is sufficient, but migration
  must refuse to start below a 4 GiB safety threshold.

## Target layout

The physical data path remains compatible with the existing pipeline:

```text
~/Library/Application Support/retro/
  rollout-memory/
    raw/
    normalized/
    rendered/
    mined/
    signals/
    memories/
    quests/
    headless/
    training/
    experiments/
    migrations/
    sync/
  state/
    archive.lock
    last-sync.json
    source-fingerprints.json

/Users/sajad/Dev/repos/Mem/
  rollout-memory -> ~/Library/Application Support/retro/rollout-memory
  dashboard/
```

Retro's logs and scheduler definition are kept out of the archive:

```text
~/Library/Application Support/retro/
  config.json

~/Library/Logs/retro/
  sync.log
  sync.error.log

~/Library/LaunchAgents/
  io.retro.sync.plist
```

The archive can therefore be moved or backed up independently of scheduler
state.

## Configuration and root resolution

Add a single root resolver and use it everywhere, including the CLI, dashboard
builders, terminal dashboard, mining helpers, and periodic runner.

Precedence:

1. Command `--root`
2. `RETRO_ROOT`
3. Existing `RETRO_ARTIFACT_ROOT` compatibility alias
4. Per-user `config.json`
5. Platform default from `platformdirs`

The configured values for this machine will be:

```json
{
  "archive_root": "/Users/sajad/Library/Application Support/retro/rollout-memory",
  "dashboard_dir": "/Users/sajad/Dev/repos/Mem/dashboard",
  "sync_interval_seconds": 900,
  "sync_on_login": true
}
```

New commands:

```bash
retro config show
retro config set archive-root "/Users/sajad/Library/Application Support/retro/rollout-memory"
retro config set dashboard-dir /Users/sajad/Dev/repos/Mem/dashboard
retro doctor
```

`retro doctor` will show the effective config, writable paths, free space,
source roots, scheduler state, lock state, last sync, and archive counts.

## Migration command design

Add:

```bash
retro archive plan --from <source-a> --from <source-b> --into <canonical-root>
retro archive migrate --plan <plan.json>
retro archive verify --migration <migration-id>
```

Every plan writes:

```text
rollout-memory/migrations/<migration-id>/
  plan.json
  files.jsonl
  source-inventory.json
  pre-migration-counts.json
  backups/
  conflicts/
  verification.json
```

Each file manifest row records:

- source and target paths,
- category,
- byte size,
- modification time,
- SHA-256,
- session host/id when applicable,
- planned disposition: adopt, copy, rebuild, skip-identical, or conflict.

### Conflict policy

- Missing target: copy to a staging path, verify size and SHA-256, then rename
  atomically.
- Identical target: skip and record deduplication.
- Same logical key with different bytes: never overwrite. Copy both versions
  under `migrations/<id>/conflicts/`, mark the migration incomplete, and require
  explicit resolution.
- Interrupted migration: resume from the manifest; never restart blindly.
- Source archives remain untouched until final verification and a separate
  user-approved cleanup.

## Migration phases

### Phase 0: Protect the Mem working tree

1. Record `git status`, HEAD, branch, and untracked paths in the migration
   manifest.
2. Save a binary Git diff for tracked changes.
3. Archive every untracked code/config path with a SHA-256 manifest. A plain
   Git patch is insufficient because it omits untracked files.
4. Keep ignored archive/experiment data under the migration file manifest
   rather than relying on Git.
5. Do not run `git clean`, reset, checkout, or merge in the Mem checkout during
   data migration.

This protects code work independently from archive data.

### Phase 1: Protect the temporary Copilot archive

The 844 MB Copilot source currently exists only inside the session worktree.
Before any lower-priority implementation work:

1. Lock the worktree with `git worktree lock` so automated prune cannot remove
   it.
2. Hash its authoritative raw/normalized/rendered files.
3. Copy its raw namespace to canonical migration staging first.
4. Keep the worktree locked until all 53 sessions pass final verification.

### Phase 2: Copy and inventory the existing Mem archive

Copy the 674-session Mem archive into the new physical canonical root.

1. Hash authoritative source artifacts.
2. Validate raw and normalized JSONL.
3. Use SQLite's backup API to snapshot `memories/index.sqlite`.
4. Copy the current signals, quest state, dashboard, and manifests into the
   migration backup area.
5. Record training, headless, and experiment artifacts in the manifest.

### Phase 3: Merge the 53 Copilot sessions

Copy from:

```text
/Users/sajad/Dev/repos/retro.worktrees/capture-vscode-copilot-rollout-data/rollout-memory
```

Copy these namespaces:

- `raw/vscode-copilot/`
- `normalized/vscode-copilot/`
- `rendered/vscode-copilot/`

Do not copy source-local aggregate signals; they will be recomputed globally.

Expected post-copy session counts:

```text
codex            648
claude-code       26
vscode-copilot    53
total            727
```

### Phase 4: Make evidence references portable

Change future importers to store archive-relative raw references:

```text
raw/<host>/<session-id>/<file>
```

Readers must support both legacy absolute paths and new relative paths.

During migration:

1. Rewrite only `raw_ref.path`.
2. Preserve event IDs, sequence, timestamps, parent links, payloads, and
   summaries exactly.
3. Write transformed streams to staging.
4. Verify event count and all non-path fields before atomic replacement.
5. Preserve original normalized streams in the migration backup.

This removes references to the temporary Copilot worktree and makes the archive
relocatable.

### Phase 5: Preserve non-session artifacts

- Keep `mined/`, `quests/`, `headless/`, and `training/` byte-for-byte.
- Move copies of `Mem/logs/` and the three root model-evaluation JSON files to
  `rollout-memory/experiments/swebench/legacy-mem/`.
- Retain the originals until verification completes.
- Rewrite absolute experiment paths only in copied JSON config/manifest files;
  preserve original values in migration metadata.
- Exclude caches, `.venv`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`,
  `__pycache__`, and `.DS_Store`.

### Phase 6: Rebuild databases and derived portfolio data

1. Rebuild `memories/index.sqlite` from the migrated mined artifacts.
2. Assert exactly 58 unique memory hashes and 731 evidence references.
3. Recompute signal readings for all 727 sessions. Do not concatenate old
   readings or aggregates.
4. Regenerate rendered transcripts after normalized-path verification.
5. Build the dashboard to a staging directory.
6. Validate it, then atomically swap it into
   `/Users/sajad/Dev/repos/Mem/dashboard`.

The old memory DB, signals, rendered files, and dashboard remain available in
the migration backup until acceptance.

### Phase 7: Cut over the Mem compatibility path

Only after all acceptance checks pass:

1. Move the original Mem `rollout-memory` directory to the migration's
   source-backup area. Do not delete it.
2. Create `/Users/sajad/Dev/repos/Mem/rollout-memory` as a symlink to the
   physical canonical archive.
3. Verify commands run through both the global resolver and the compatibility
   path.
4. Keep the original source backup until explicit cleanup approval.

## Periodic sync design

Add one orchestrator:

```bash
retro sync
```

It performs, under a per-user exclusive lock:

1. Discover all Claude Code, Codex, VS Code chat, Copilot CLI, and Agent Host
   sources for the current user.
2. Import only new or changed sessions.
3. Render only new or changed normalized streams.
4. Upsert signal readings for changed sessions and recompute aggregates.
5. Rebuild the memory index only if mined files, authored memory logs, or
   utility events changed.
6. Incrementally update cached dashboard session summaries.
7. Atomically publish the dashboard.
8. Write a structured run report and update `last-sync.json`.

### Raw update semantics

Periodic capture must not silently destroy earlier raw states:

- If a source is append-only and the new bytes contain the old capture as a
  prefix, replace the current snapshot atomically because it is a strict
  superset.
- If a source was compacted, truncated, or rewritten, retain the prior capture
  under `raw-revisions/<host>/<session>/<content-hash>/` before publishing the
  new snapshot.
- Database snapshots use SQLite's online backup API, never a direct copy of a
  live WAL database.

### Incremental state

Track source fingerprints by:

```text
(source kind, canonical source path, size, mtime_ns, content hash when needed)
```

Track derived fingerprints by normalized file SHA-256 and signal/dashboard
schema version. A no-op sync must not rewrite aggregate files or the dashboard.

### Error handling

- Continue across independent source sessions.
- Return nonzero when any capture fails.
- Never emit success-shaped output for a partial run.
- Record failures with source, exception type, and remediation.
- Keep the previous dashboard and indexes when a rebuild fails.
- Retain the last 30 structured run reports and rotate text logs.
- Warn when free space falls below 5 GiB.
- Refuse to start a scheduled sync below 2 GiB free, record a visible failure,
  and preserve the last successful archive/dashboard state.
- Apply configured retention to raw revisions and logs, but never delete a
  session's only raw capture automatically.

## Periodic installation

Provide:

```bash
retro setup \
  --archive-root "/Users/sajad/Library/Application Support/retro/rollout-memory" \
  --dashboard-dir /Users/sajad/Dev/repos/Mem/dashboard \
  --periodic 15m

retro schedule install --every 15m
retro schedule status
retro schedule run-now
retro schedule uninstall
```

On macOS, `schedule install` creates a user LaunchAgent:

- `RunAtLoad = true`
- `StartInterval = 900`
- absolute path to the installed Retro executable,
- no sudo,
- no shell interpolation,
- standard output/error redirected to `~/Library/Logs/retro/`,
- job runs `retro sync --scheduled`.

The installer validates that the executable and configured archive remain
reachable after the current shell exits. Documentation should recommend
`pipx install retro-ai` or another stable tool environment rather than an
ephemeral project virtualenv.

Linux can use a systemd user timer and Windows can use a per-user Task Scheduler
job behind the same CLI interface. The first implementation only enables the
native scheduler for the current platform and fails clearly elsewhere.

## Dashboard scalability changes

A 727-session dashboard should not rescan and re-embed every full transcript
every 15 minutes.

1. Cache per-session analysis keyed by normalized SHA-256 and dashboard schema
   version.
2. Recompute only changed sessions.
3. Keep portfolio summary data in the main JSON payload.
4. Cap embedded transcript previews and link to full rendered artifacts.
5. Publish with staging plus atomic rename so readers never see a partial
   dashboard.
6. Show source kind, capture freshness, active snapshot state, last successful
   sync, and any failed sources.

## Local backup and remote sync

The existing `.gitignore` excludes `rollout-memory/` and generated dashboards,
so `git push` does not back up this archive.

The physical canonical archive is outside the Git worktree, which protects it
from repository cleanup but is not itself a backup. Initial periodic sync
remains local-only. Add optional backup configuration as a separate, explicit
phase:

```bash
retro backup add /Volumes/EncryptedBackup/retro
retro backup run
```

Backup targets must use checksummed, resumable copies and must never be enabled
implicitly. Cloud/Git remote upload is out of scope until encryption and
redaction policies are explicitly configured because raw rollouts may contain
credentials, source code, and command output.

## Test plan

### Root/configuration

- CLI override, environment, config, and platform-default precedence.
- All commands resolve the same archive and dashboard directories.
- Legacy `RETRO_ARTIFACT_ROOT` remains compatible.
- Python 3.9 through 3.13.

### Migration

- Dry-run has no filesystem side effects beyond its requested plan output.
- Missing, identical, and conflicting file behavior.
- Resume after interruption.
- Insufficient disk-space refusal.
- SHA-256 verification and atomic staging.
- Absolute-to-relative `raw_ref` conversion with all other event fields equal.
- Exact counts for raw, normalized, rendered, mined, training, headless, quest,
  memory, and experiment artifacts.
- SQLite backup and deterministic memory reindex.

### Sync

- First run imports all available sessions.
- Second run is a true no-op.
- A growing active session updates exactly that session.
- A rewritten source creates a raw revision.
- One source failure does not corrupt other sessions or publish partial
  aggregate artifacts.
- Lock prevents manual and scheduled overlap.
- Low-space warning and hard-stop behavior.
- Signal upsert does not discard readings from unchanged hosts.
- Dashboard cache invalidates on schema changes.

### Scheduler

- Generated LaunchAgent uses absolute paths and valid XML.
- Install is idempotent.
- Status distinguishes loaded, installed-but-not-loaded, failed, and absent.
- Run-now writes a structured report.
- Uninstall removes only Retro's own LaunchAgent.

## Acceptance criteria

Migration is complete only when:

1. The canonical archive contains 727 session keys:
   648 Codex, 26 Claude Code, and 53 VS Code Copilot.
2. It contains 727 raw session directories, 727 normalized streams, and 727
   rendered transcripts.
3. The normalized event total is 171,744 before any newly captured activity.
4. Every authoritative copied file matches its source SHA-256.
5. Every normalized raw reference resolves inside the canonical archive.
6. Raw and normalized JSONL validation reports zero malformed records.
7. The rebuilt memory index has 58 memories and 731 evidence references.
8. Training has 88 substantive files totaling 389,257,494 bytes; four
   `__pycache__/*.pyc` files are intentionally excluded.
9. Headless artifacts have 3 files totaling 17,009 bytes.
10. The dashboard lists all 727 sessions and opens from the configured output.
11. `retro` run from an unrelated directory resolves the global archive. The
    Mem compatibility path resolves to the same physical location.
12. A scheduled no-op run succeeds without rewriting unchanged artifacts.
13. Appending to an active Copilot session is captured on the next interval.
14. The original Mem and Copilot source archives remain available until
    explicit cleanup approval.

## Rollback

1. Disable the LaunchAgent with `retro schedule uninstall`.
2. Restore the prior global config or use `--root` to point at either original
   archive.
3. Restore rebuilt DB/signals/dashboard files from
   `migrations/<id>/backups/`.
4. Remove only files listed as newly created by the migration manifest.
5. Never delete the original source archives as part of automatic rollback.

## Execution order after approval

1. Lock and checksum the temporary Copilot worktree archive.
2. Protect tracked and untracked Mem training work.
3. Implement and test global root/config resolution.
4. Implement manifest-based migration and portable raw references.
5. Run and review `retro archive plan`.
6. Copy both source archives to the per-user canonical root.
7. Verify counts, hashes, database reconstruction, and dashboard output.
8. Cut over the Mem path to a compatibility symlink.
9. Implement incremental `retro sync`.
10. Install and test the per-user LaunchAgent.
11. Observe one scheduled no-op run and one active-session update.
12. Only then consider deleting source backups or configuring an encrypted
    backup target.
