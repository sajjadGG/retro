# Durable Rollout Capture Spec

## Problem

Codex and Claude Code session storage is a source cache, not a durable research archive.

Claude Code stores transcripts under `~/.claude/projects/`, but local transcript files are automatically swept after `cleanupPeriodDays` days. Codex stores rollout JSONL under `~/.codex/sessions/`, but those files are still local application state and can be affected by cleanup, app migrations, profile changes, or accidental deletion.

For this project, losing those files means losing the highest-value data: user messages, assistant messages, tool calls, tool results, file edits, errors, costs, token usage, and working style.

## Goal

Make `rollout-memory/` the durable archive of every available Codex and Claude Code rollout.

The archive should be populated continuously, before upstream tools can delete or move their source files, and should expose capture coverage and secret-risk signals in the dashboard.

## Non-Goals

- Do not depend on Claude or Codex UI history as the source of truth.
- Do not assume their local storage retention policies are stable.
- Do not upload transcripts to a hosted service by default.
- Do not print or display detected secrets in cleartext.

## Source Locations

Claude Code:

```text
~/.claude/projects/<project>/<session>.jsonl
~/.claude/projects/<project>/<session>/subagents/*.jsonl
```

Codex:

```text
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
~/.codex/state_5.sqlite
```

`state_5.sqlite` helps discover Codex threads and metadata, but the rollout JSONL is the durable content to copy.

## Capture Strategy

### Manual Backfill

Run:

```bash
retro import all
```

This imports all currently discoverable Claude Code sessions and Codex threads into:

```text
rollout-memory/raw/<host>/<id>/
rollout-memory/normalized/<host>/<id>.events.jsonl
rollout-memory/rendered/<host>/<id>.md
```

Existing raw captures are skipped unless `--force` is passed.

### Continuous Capture

Run `retro import all` on a schedule. Recommended minimum:

- hourly while actively using Codex or Claude Code,
- daily on machines where the tools are used occasionally,
- immediately before upgrading Codex, Claude Code, or their desktop apps.

The scheduled command should be idempotent. It must skip already-imported sessions and keep going when one source session fails.

### Retention Hardening

Set Claude retention explicitly in `~/.claude/settings.json`:

```json
{
  "cleanupPeriodDays": 3650
}
```

This reduces the chance of losing source transcripts before `retro` imports them. It is not a substitute for importing into `rollout-memory/`.

Codex does not currently have an equivalent retention setting in this project. Treat `~/.codex` as local app state and copy it into `rollout-memory/` promptly.

## Dashboard Requirements

The dashboard should show:

- sessions used for stats,
- sessions with token data,
- sessions with cost estimates,
- sessions with missing rendered transcript,
- sessions with missing mined memory,
- sessions with risk signals,
- per-session signal detail.

The existing dashboard reads:

```text
rollout-memory/signals/readings.jsonl
rollout-memory/signals/aggregates.json
```

after running:

```bash
retro signal run
python dashboard/build_dashboard.py
```

## Secret Detection Signal

Captured rollouts can contain secrets because tool outputs, pasted text, environment dumps, and file contents may be written into transcript JSONL.

Add risk signals:

- `secret_exposure_count`: number of likely secrets found,
- `secret_exposure_signal`: boolean flag for any likely secret exposure.

The scanner should detect common credential shapes:

- OpenAI keys,
- Anthropic keys,
- GitHub tokens,
- AWS access key ids,
- Google API keys,
- Stripe secret keys,
- Slack tokens,
- JWTs,
- generic assignments such as `api_key=...`, `token: ...`, `password=...`.

Signal metadata must include only redacted snippets, secret type, event id, actor, and event type. It must never store the raw secret value in `readings.jsonl`.

## Operating Flow

Recommended local flow:

```bash
retro import all
retro signal run
python dashboard/build_dashboard.py
```

For long-term use, this should become a scheduled job or app automation.

## Security Notes

`rollout-memory/raw/` is intentionally complete and may contain sensitive data. Treat it like a private audit log:

- keep it outside public repos,
- encrypt backups,
- avoid syncing to consumer cloud folders without encryption,
- add redaction/export modes before sharing dashboards,
- consider a future encrypted archive backend.

## Acceptance Criteria

- `retro import all` imports every currently discoverable Codex and Claude Code session.
- Re-running `retro import all` is safe and skips existing raw captures.
- A scheduled run can preserve sessions before upstream cleanup.
- `retro signal run` emits secret-risk readings.
- Dashboard shows secret-risk aggregates and per-session signal details.
- Secret signal evidence is redacted.
