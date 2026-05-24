# Rollout Signals Spec

## Purpose

Signals are evaluators that observe a captured rollout (or a slice of one) and emit a comparable reading: a number, a boolean, a label, or a short string. They answer:

> "How did this rollout go, by some objective measure?"

Signals are **separate from mining**. Mining extracts memories to inject into future prompts. Signals extract *observations about the rollout itself* — successful behaviors, repeated failures, cost proxies, capture quality — and roll them up across many sessions.

Signals feed three downstream consumers:

1. **Dashboard.** Per-session columns, KPI strip, distributions over time.
2. **Mining.** Tag sessions as success/failure so the miner can weight evidence (next phase).
3. **Retrieval.** Filter or rank captured rollouts when injecting prior context into new sessions (later phase).

## What a Signal Is

A signal is a callable that takes a `SessionContext` (normalized events, raw dir, host, session id, optional repo cwd) and returns zero or more `SignalReading`s. A reading carries:

- `signal`: stable identifier (e.g., `git_commits_made_during`)
- `group`: one of `activity`, `outcome`, `cost`, `risk` (see grouping below)
- `kind`: `numeric` | `boolean` | `categorical` | `text`
- `unit`: optional human label for the value (`count`, `seconds`, `ratio`, `score`, `label`, …)
- `value`: the result
- `confidence`: 0.0–1.0; defaults to 1.0 for pure heuristics
- `evidence_refs`: event_ids the signal pointed at (so the dashboard can link back)
- `method`: `heuristic` | `external` | `regex` | `llm_judge`
- `metadata`: free-form supplemental fields

Each signal is **pure with respect to its inputs**. External signals (e.g., reading `git log` in the project's cwd) declare themselves so they can be skipped when the repo isn't present or has moved.

## Grouping

Signals are organized by intent, not by implementation:

| Group      | Question answered                                | Examples |
| ---------- | ------------------------------------------------ | -------- |
| `activity` | What did the agent do?                           | `command_count`, `unique_files_edited`, `web_search_count` |
| `outcome`  | Did the work land?                               | `git_commits_made_during`, `failed_command_ratio`, `user_satisfaction_lexical` |
| `cost`     | What did it cost (effort/tokens/time)?           | `session_duration_seconds`, `user_correction_count`, `time_to_first_edit_seconds` |
| `risk`     | How trustworthy / clean was the rollout?         | `unknown_event_count`, `events_without_timestamps`, `interrupted_signal` |

A signal lives in exactly one group.

## Method Categories

| Method     | What it does                                                   |
| ---------- | -------------------------------------------------------------- |
| `heuristic`| Pure function over normalized events (counts, durations, ratios). |
| `regex`    | Pattern match over text payloads (user corrections, satisfaction lexicon). |
| `external` | Touches state outside the rollout (git log in cwd, file-still-exists). May produce no reading if the external context is missing. |
| `llm_judge`| Calls a model to rate or label the session. (Not in v0.) |

## Pipeline

```
rollout-memory/normalized/<host>/<id>.events.jsonl
                    │
                    ▼
        ┌──────── signal runner ────────┐
        │  for each session × signal:   │
        │  produce SignalReading rows   │
        └───────────────────────────────┘
                    │
                    ▼
rollout-memory/signals/readings.jsonl   ← one row per (session, signal)
rollout-memory/signals/aggregates.json  ← rolled-up stats per signal across all sessions
rollout-memory/signals/summary.md       ← human view
```

Aggregation, per signal:

- `count`: how many sessions produced a reading
- `kind`: inherited from the signal
- For numerics: `min`, `max`, `mean`, `median`, `p90`, `sum`
- For booleans: `true_count`, `false_count`, `true_ratio`
- For categoricals: histogram of labels

## CLI

```bash
retro signal list                                # list registered signals + groups
retro signal run                                 # compute every signal for every imported session
retro signal run --host claude --session-id ID   # one session
retro signal run --signal git_commits_made_during
retro signal show <host> <session-id>            # readings for one session
```

`signal run` is incremental by default: re-running it overwrites readings only for sessions or signals that match the filter. Pass `--clean` to drop and rebuild.

## Initial Catalog (v0)

| Signal                          | Group    | Kind        | Unit       | Method     |
| ------------------------------- | -------- | ----------- | ---------- | ---------- |
| `command_count`                 | activity | numeric     | count      | heuristic  |
| `file_edit_count`               | activity | numeric     | count      | heuristic  |
| `unique_files_edited`           | activity | numeric     | count      | heuristic  |
| `unique_files_read`             | activity | numeric     | count      | heuristic  |
| `web_search_count`              | activity | numeric     | count      | heuristic  |
| `assistant_message_count`       | activity | numeric     | count      | heuristic  |
| `user_message_count`            | activity | numeric     | count      | heuristic  |
| `session_duration_seconds`      | cost     | numeric     | seconds    | heuristic  |
| `time_to_first_edit_seconds`    | cost     | numeric     | seconds    | heuristic  |
| `user_correction_count`         | cost     | numeric     | count      | regex      |
| `failed_command_count`          | outcome  | numeric     | count      | heuristic  |
| `failed_command_ratio`          | outcome  | numeric     | ratio      | heuristic  |
| `interrupted_signal`            | outcome  | boolean     | flag       | heuristic  |
| `user_satisfaction_lexical`     | outcome  | categorical | label      | regex      |
| `git_commits_made_during`       | outcome  | numeric     | count      | external   |
| `unknown_event_count`           | risk     | numeric     | count      | heuristic  |
| `events_without_timestamps`     | risk     | numeric     | count      | heuristic  |
| `capture_gap_signal`            | risk     | boolean     | flag       | heuristic  |

`git_commits_made_during` produces no reading when the session's cwd is missing or is not a git repo. That is reported as a `risk` capture-gap, not silently swallowed.

## Acceptance Criteria

- `retro signal list` shows every registered signal with its group and method.
- `retro signal run` writes `readings.jsonl` and `aggregates.json` under `rollout-memory/signals/`.
- Each reading carries enough metadata (signal, session, kind, value, evidence_refs, method) to be reproduced from raw + normalized.
- The dashboard surfaces signal readings without breaking existing capture/mining metrics.
- Signals that cannot run (missing repo, missing data) are reported, not silently skipped.

## Non-Goals For V0

- No LLM-based signals yet.
- No back-propagation from signals into mining weights yet.
- No streaming/online signal computation — runner is batch over imported sessions.
- No SQL or vector index — JSONL + JSON aggregates are enough at this stage.
