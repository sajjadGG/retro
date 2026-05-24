# Rollout Dashboard Spec

## Purpose

Build a local dashboard for browsing captured Codex and Claude Code rollouts, comparing session-level stats, and drilling into any rollout's rendered transcript and mined memory.

The dashboard is a view layer over `rollout-memory/`. It should not own capture, normalization, rendering, or mining logic.

## Useful Metrics

### Portfolio Metrics

These answer: "What has happened across all sessions?"

- total imported sessions,
- total sessions used for dashboard stats,
- sessions with token usage,
- sessions with cost estimates,
- sessions by host: Codex vs Claude Code,
- active days,
- sessions per day,
- total captured events,
- total user messages,
- total assistant messages,
- total tool calls/results,
- total commands,
- total file reads,
- total file edits,
- total errors,
- total unknown events/capture gaps,
- total estimated tokens,
- total estimated cost,
- average session duration,
- average events per session,
- average tool calls per session.

### Per-Session Metrics

These answer: "What happened in this rollout?"

- host,
- session/thread id,
- inferred task title,
- first timestamp,
- last timestamp,
- duration,
- event count,
- event type counts,
- actor counts,
- user message count,
- assistant message count,
- reasoning event count,
- tool call count,
- tool result count,
- command count,
- failed command/tool result count,
- file read count,
- file edit count,
- unique files touched,
- top tool names,
- token usage:
  - reported input tokens,
  - reported cache creation tokens,
  - reported output tokens,
  - reported cached input tokens,
  - reported reasoning output tokens,
  - reported total tokens,
- inferred model names and the model used for cost estimation,
- estimated cost,
- mined memory candidate count,
- rendered transcript path,
- raw and normalized artifact paths.

### Timeline Metrics

These answer: "When did activity happen?"

- sessions by day,
- tokens by day,
- tool calls by day,
- edits by day,
- cost by day.

### Quality And Capture Metrics

These answer: "How reliable is the captured data?"

- unknown event count,
- events without timestamps,
- host-specific capture gaps,
- sessions with missing rendered transcript,
- sessions with missing mined memory,
- sessions with no token/cost signal.

## Cost And Token Model

Cost should be treated as an estimate, not billing truth.

Reasons:

- rollout formats do not always preserve exact billable model names,
- some token payloads are cumulative while others are per-message,
- cached token billing can differ by provider and date,
- provider prices change.

The dashboard should follow the conventions used by existing usage dashboards:

- Claude Code JSONL usage should be deduplicated by assistant `message.id` plus request id. Claude may write the same assistant response across multiple lines for thinking, text, and tool-use fragments, each carrying the same `message.usage`.
- Claude cost should split `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and `output_tokens`. If `costUSD` is available from the raw log, a future mode can prefer it for billing display.
- Codex `token_count.info.total_token_usage` is cumulative. For reporting, use the final/max cumulative total, or compute per-turn deltas from successive totals when per-model breakdown is required.
- Codex `input_tokens` includes cached input. Cost should charge `input_tokens - cached_input_tokens` at the normal input rate and `cached_input_tokens` at the cache-read rate.
- Codex reasoning tokens are useful to show, but the current ccusage implementation does not charge them as a separate line item.

The dashboard should show:

- token fields separately,
- the cost rate used,
- whether cost is estimated or unavailable,
- enough metadata to recompute later.

V0 can use a small local rate table with conservative defaults and label the output as estimated.

## Dashboard UX

### First Screen

Show:

- KPI strip: sessions, days, events, tool calls, edits, tokens, estimated cost,
- coverage counts: sessions used, sessions with token data, sessions with cost estimates,
- host breakdown,
- sessions by day chart,
- searchable/filterable session table.

### Session Table

Columns:

- host,
- date,
- title,
- duration,
- events,
- tool calls,
- commands,
- edits,
- tokens,
- estimated cost,
- mined memories,
- capture gaps.

Clicking a row opens the drill-down.

### Drill-Down View

For a selected session, show:

- summary cards,
- event type breakdown,
- tool name breakdown,
- token/cost breakdown,
- mined prompt memory if present,
- rendered transcript preview,
- links to normalized/raw/rendered/mined artifacts.

## Implementation Shape

Keep dashboard code separate from the CLI package:

```text
dashboard/
  build_dashboard.py
  index.html
  data/
    rollouts.json
```

`build_dashboard.py` reads:

```text
rollout-memory/normalized/**.events.jsonl
rollout-memory/rendered/**/*.md
rollout-memory/mined/**/*.json
rollout-memory/mined/**/*.prompt.md
```

and writes a static dashboard. The dashboard should work by opening `dashboard/index.html` from disk.

## V0 Acceptance Criteria

- Build command creates `dashboard/data/rollouts.json`.
- Build command creates or updates `dashboard/index.html`.
- Dashboard lists all imported sessions.
- Dashboard shows aggregate stats.
- Dashboard filters by host and search text.
- Clicking a session shows drill-down metrics.
- Clicking "Transcript" shows the rendered markdown text.
- Clicking "Memory" shows mined prompt memory text when present.
- Cost is clearly labeled as estimated.
