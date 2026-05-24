# ccusage Comparison & Gap Spec

Date: 2026-05-20
Reference: [ryoppippi/ccusage](https://github.com/ryoppippi/ccusage) — Rust + TS CLI that "Analyzes coding (agent) CLI token usage and costs from local data". ~14k stars.

## Purpose

ccusage and `rollout-memory` overlap in one place — both read local logs from coding agent CLIs — but diverge in goal. ccusage is a **token-and-cost reporter**. We capture **the whole rollout**: prompts, tool calls, edits, sub-agents, then mine it for behavioral memory and evaluate it via signals. ccusage is therefore not a competitor but a strong reference for the parts where we **also** want to report on tokens, cost, and time.

This document inventories what ccusage does well, what we already have, where we lag, and which gaps are worth closing — with a phased plan and explicit non-goals.

## What We Already Have That ccusage Does Not

Keep these. They are the project's reason to exist.

| Capability | Where it lives |
| --- | --- |
| Full event-level rollout capture (raw + normalized JSONL) | `src/retro/importers/` |
| Markdown render of any session for human reading | `src/retro/renderer.py` |
| Memory mining (ReMe POC) — extracts memory candidates for future prompts with evidence links | `src/retro/mining.py` |
| Signals layer — `activity` / `outcome` / `cost` / `risk` evaluators per session, with aggregates | `src/retro/signals/` |
| SQLite-aware Codex discovery via `state_5.sqlite.threads.rollout_path` | `src/retro/importers/codex.py` |
| Parent/child thread graph from `thread_spawn_edges` | same |
| Per-event evidence linking from every mined memory and every signal reading | schema + mining + signals |

ccusage operates one layer up (per-session token totals) and is not trying to replay sessions, mine memory, or score behavior.

## What ccusage Does Well — Gap Inventory

Severity:
- **P0** — silent data loss or correctness bug today.
- **P1** — meaningful usability/accuracy gap; high adoption ROI.
- **P2** — nice-to-have; low blocking impact.

### Capture / discovery

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| C1 | Missing `~/.config/claude/projects/` path | **P0** | We only scan `~/.claude/projects/` | Scans both; combines | Add `~/.config/claude/projects/` to `ClaudeImporter.projects_dir` candidates. Combine results, de-dup by session id. |
| C2 | No `CLAUDE_CONFIG_DIR` / `CODEX_HOME` env overrides | **P1** | Hard-coded | Honors both; comma-separated list ok | Read env in importer constructors; split on `,`; iterate. |
| C3 | Codex multi-root + plain-JSONL-dir support | **P2** | Single `~/.codex` | `CODEX_HOME` entries with `sessions/` treated as Codex homes; entries without are read as plain JSONL dirs (for `codex exec --json` output) | Mirror in `CodexImporter`: if a root has `sessions/`, treat normally; else scan for `*.jsonl` directly. |
| C4 | Retention/lifecycle awareness for Claude Code | **P1** | Silent | Documents 30-day default retention, points users to `cleanupPeriodDays` | Surface a "log retention risk" check in `retro list` showing oldest discoverable Claude transcript per project. Add a doc note. |
| C5 | Dedup across project dirs | **P2** | Each session lives in exactly one project dir today | ccusage dedups by `(message.id, requestId)` because the same message can land in multiple project files when sessions are continued | The user already added this for token tallying in `extract_raw_usage`. Verify it also runs at the capture/normalize step before counts diverge. |

### Token computation

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| T1 | Codex token deltas, not just cumulative totals | **P1** | `extract_token_stats` takes max of cumulative `total_token_usage`. Loses per-turn timing. | Subtracts previous cumulative totals from each `token_count` event → per-turn delta. Aggregates by turn and by model. | In `extract_token_stats`, walk `token_count` events in order, keep `previous_totals`, emit `delta = current - previous`. Keep cumulative for sanity check. |
| T2 | Per-model token attribution | **P1** | Single `models[]` list on the session; tokens are not split per model. | Reads `turn_context.model`; attributes the per-turn delta to that model. Output supports `--breakdown` for per-model rows. | Track active model from each `turn_context` event during the deltas walk; emit `tokens_by_model[model] = {input, output, cache_*}`. |
| T3 | Fallback model tag | **P2** | Silently uses `choose_cost_model` defaults | Marks rows `isFallback: true` when model metadata was missing and `gpt-5` was assumed | Add `model_fallback: bool` to per-session token stats. Show in dashboard tooltip. |

### Cost

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| $1 | Rate table is hardcoded and prone to drift | **P0** | `DEFAULT_RATES` in `build_dashboard.py` is hand-maintained | Pulls from LiteLLM pricing JSON. Ships a locked snapshot for offline reproducibility; updates via Nix flake input + scheduled GH Action PR. | Phase 1: vendor the LiteLLM model_prices_and_context_window.json (or a subset) under `pricing/litellm-fallback.json`. Phase 2: optional `--refresh-pricing` to fetch the upstream. |
| $2 | No `auto` / `calculate` / `display` cost modes | **P1** | Always calculate | Three modes: `auto` (use embedded `costUSD` when Claude provided it, else compute), `calculate` (always compute), `display` (only show pre-calculated). | Detect `costUSD` on Claude events and add `mode` flag in dashboard config. `auto` is the right default. |
| $3 | No Codex "speed / service tier" pricing | **P2** | Standard rates only | Reads `~/.codex/config.toml` for `service_tier = "priority"`/`fast`; uses model-specific multiplier from LiteLLM, falls back to 2x. | Phase later. Parse `config.toml` once at dashboard build, expose `speed: standard|fast` per session, apply multiplier. |
| $4 | ~~Reasoning tokens are not billed correctly~~ | ~~P1~~ → **already correct** | After re-reading the current code: `DEFAULT_RATES` has no `reasoning_output` entry and `estimate_cost` never multiplies by reasoning tokens. Reasoning is tracked only as an informational counter. | ccusage clarifies reasoning tokens are part of the output charge; not billed separately. | No change needed. Closed. |
| $5 | ~~Cache token math edge cases~~ | ~~P1~~ → **already correct (asymmetry is intentional)** | Codex path subtracts `cached_input_tokens` from `input_tokens`; Anthropic path does not. | Verified against `ccusage/rust/crates/ccusage/src/adapter/codex.rs:475-528`: ccusage **only subtracts for Codex** via `non_cached_input_tokens(input_tokens, cached_input_tokens) = input_tokens.saturating_sub(cached_input_tokens)`. Anthropic's API reports `input_tokens` as already-non-cached (cache reads are a separate disjoint field), so subtraction would double-discount. | No change needed. Closed. |

### Reports & rollups

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| R1 | No date-range filter | **P1** | All sessions, every time | `--since YYYY-MM-DD --until YYYY-MM-DD` | Add `--since`/`--until` to `retro list` and the dashboard's JS filter UI. |
| R2 | No weekly / monthly / blocks rollups | **P1** | Per-session table + `by_day` only | `daily`, `weekly`, `monthly`, `session`, `blocks` views | Add a `reports.py` that produces rollup JSON. Dashboard adds tabs. |
| R3 | No 5-hour billing block view | **P2** | None | `ccusage blocks` groups by Claude's 5-hour billing windows; shows active block w/ burn rate and projection | Implement in two parts: (a) `blocks_for(events)` that rolls user/assistant message timestamps into 5-hour windows starting on first message, and (b) dashboard tile. |
| R4 | No per-model breakdown column | **P1** | Single model list per session | `--breakdown` reveals one row per model under each date | Once T2 lands, the data is there. Add a `Models` panel in session detail + a per-day per-model rollup. |
| R5 | No "project" / "instance" grouping | **P1** | We display `project_slug` (Claude only) but don't aggregate by it | `--instances` (group by Claude project), `--project <slug>` filter | Reuse `project_slug` for Claude and `raw_meta.cwd` for Codex; add a rollup-by-project view. |
| R6 | No timezone control | **P2** | All `by_day` keys are UTC (first 10 chars of ISO timestamp) | `--timezone` for date grouping | Add `--timezone` to the dashboard builder; convert timestamps before bucketing. |

### Output formats

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| O1 | No JSON CLI output | **P1** | CLI prints Rich tables; only the dashboard emits JSON (`rollouts.json`) | Every command has `--json` for piping/automation | Add `--json` to `retro list`, `retro show`, `retro signal show`, `retro signal list`. |
| O2 | No compact / screenshot mode | **P2** | Always wide | `--compact` for narrow terminals and screenshots | Pass `--compact` to Rich tables; collapse columns when set. |
| O3 | No statusline integration | **P2** | None | `ccusage statusline` reads Claude's hook input, prints one-line session cost + today's total + active block + burn rate | Implement `retro statusline` that reads `session_id` + `transcript_path` from stdin (Claude hook format) and emits one line. Useful even before token math is perfect — can show event count, last edit time, active model. |

### Infra & ergonomics

| # | Gap | Severity | Today | ccusage approach | Adoption sketch |
|---|---|---|---|---|---|
| I1 | No config file | **P2** | All flags every run | `ccusage.example.json` with JSON Schema; `--config` flag; defaults | Add `rollout-memory.config.json` reader to `_layout()`. Map `root`, default host, default since/until. |
| I2 | No `--offline` mode | **P2** | Always offline (we never fetch) | `--offline` uses bundled pricing | If we add LiteLLM fetch ($1), expose `--offline` to skip the fetch. |
| I3 | Parallel file reading | **P2** | Serial reads | Rust + parallel workers chunked by file size | Optional `concurrent.futures.ThreadPoolExecutor` over normalized JSONL reads in `collect_sessions`. Don't over-engineer. |

## What We Should Consciously Skip

ccusage solves a few problems we should not adopt:

| Thing | Why skip |
| --- | --- |
| Supporting 15+ agent CLIs (OpenCode, Amp, Droid, Codebuff, Hermes, pi-agent, Goose, OpenClaw, Kilo, Kimi, Qwen, Copilot, Gemini, …) | Our value is depth (full rollout + memory + signals), not breadth. Adding adapters dilutes focus before Claude/Codex capture is solid. |
| Rust port | Premature. Python is fine until we have >1k sessions and slow dashboards. |
| Nix flake build pipeline for pricing updates | We can vendor a static snapshot and refresh via a simple `make` target. |
| 5-hour-block "burn rate / projection" UI | Useful for live dashboards. Worth adopting **only** if we add live-session monitoring. Otherwise it's a static rollup we can fake with date-window math. |
| pnpm/Bun packaging for `bunx ccusage` | We are a local Python tool; `pipx install` is the equivalent. |

## Where We Should Go Beyond ccusage

These are not in ccusage and should remain our differentiators:

1. **Tokens-per-tool-call attribution.** Today we know totals per session. We have the raw events to compute "this Bash call cost ~N output tokens" by attributing each tool_call's surrounding assistant message tokens. ccusage doesn't see tool calls.
2. **Cost-per-outcome.** Combine `$cost` with the `git_commits_made_during` signal → "$/commit" per session. ccusage cannot do this; it doesn't see git or outcomes.
3. **Cost-per-correction.** `$cost / user_correction_count` → flags expensive sessions that the user kept correcting.
4. **Mining the high-cost / high-value sessions preferentially.** When the miner picks evidence, weight by cost so high-investment sessions inform more memory.
5. **Per-event token estimate.** When `token_count` events are sparse, attribute a delta proportionally to the assistant messages and tool calls in the turn. Lets us drill into "where did the tokens go inside this session".

## Phased Plan

### Phase 1 — fix capture correctness (1–2 days)
Land C1, C2, C3, C4, C5 (verify). ($4 and $5 turned out on closer reading to already be correct — see notes above.)

- Add `~/.config/claude/projects/` to the discovery roots.
- Honor `CLAUDE_CONFIG_DIR` and `CODEX_HOME` (both comma-separated).
- Classify Codex roots: `state_5.sqlite` → SQLite discovery; else `sessions/` → JSONL scan; else plain JSONL dir.
- Surface Claude's ~30-day log retention as a warning in `retro list`.

### Phase 2 — token & cost rigor ✅ (landed)
T1, T2, $1, $2, R4 all shipped.

- ✅ Per-turn token deltas: Codex prefers `info.last_token_usage` (already-computed delta) and falls back to subtracting cumulative `total_token_usage` for older builds. Claude reads per-message `usage` from the raw transcript with `(message.id, requestId)` dedup.
- ✅ Per-model attribution: tracks `turn_context.model` (Codex) and `message.model` (Claude). Stored as `tokens_by_model` on every session.
- ✅ LiteLLM pricing snapshot: `dashboard/pricing/litellm-pricing.json` (LiteLLM shape, curated subset). Refresh via `python dashboard/pricing/refresh.py`. `PricingMap` loader prefers the snapshot, falls back to `DEFAULT_RATES`.
- ✅ Cost modes: `--mode auto|calculate|display` on `build_dashboard.py` (also `RETRO_COST_MODE` env var). `auto` uses any embedded `costUSD` when present and computes otherwise; `display` returns None when no embedded cost exists.
- ✅ Per-model breakdown: new "Models" tab in session detail with per-model token + cost rows.

### Phase 3 — reports (2–3 days)
Land R1, R2, R5, O1.

- `retro report daily|weekly|monthly|by-project` writing `rollout-memory/reports/<view>.json`.
- Add `--since`, `--until`, `--project` filters.
- Dashboard learns the new views.
- `--json` for the CLI commands that don't already have a structured output.

### Phase 4 — usability (when needed)
T3, $3, R3, R6, O2, O3, I1, I2.

Pick when a specific use case demands them (e.g., we start running long active sessions and want the 5-hour block view; or we want the statusline for live monitoring).

## Acceptance Criteria For Phase 1

- `retro list` finds Claude transcripts in `~/.claude/projects/` *and* `~/.config/claude/projects/`.
- Setting `CLAUDE_CONFIG_DIR=/tmp/claude1,/tmp/claude2 retro list` scans both roots.
- Setting `CODEX_HOME=/tmp/codex-archive retro list` scans there.
- Dashboard cost numbers for a Claude session no longer change when reasoning is added / removed from the rate table.
- Anthropic cost path subtracts `cached_input_tokens` from `input_tokens` before pricing.
- Capture-gap counters now also report "Claude project log might be expired" when the oldest discoverable transcript is older than 25 days.

## Non-Goals (recap)

- Adding more host CLIs beyond Claude Code + Codex.
- Rewriting in Rust.
- Replicating `ccusage blocks` for live monitoring before we have a live use case.
- Becoming a generic token-and-cost reporter; we are a rollout-memory project that happens to also account for cost.
