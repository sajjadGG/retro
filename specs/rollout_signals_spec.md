# Rollout Signals Spec (v2)

Status: implemented in `src/retro/signals/`. Replaces the v0 spec and supersedes
`specs/experimental_trajectory_signals_spec.md`
(see [What was dropped](#what-was-dropped-and-why)).

## 1. Why signals exist

The archive holds every Codex, Claude Code and Copilot session the user has run.
Raw transcripts do not answer questions; signals do. The catalogue below is
derived backwards from the seven decisions the archive is supposed to support:

| # | Decision | Signals that answer it |
| --- | --- | --- |
| D1 | Which agent / model / client should I use for this project and this kind of work? | `work.kind`, `work.primary_language`, `outcome.status`, `usage.cost_usd`, `usage.active_s`, `efficiency.*`, grouped by the catalog's `agent` / `client` / `models` / `project` |
| D2 | Where do my time and money actually go? | `usage.cost_usd`, `usage.credits`, `usage.tokens_*`, `usage.duration_s`, `usage.active_s`, `efficiency.cache_read_share`, `efficiency.idle_share`, `friction.context_compactions` |
| D3 | Where do agents struggle, and why? | `friction.tool_error_rate`, `friction.tool_errors`, `friction.longest_error_streak`, `friction.edit_churn`, `friction.permission_denied`, `friction.provider_errors`, plus the command mix from `retro analyze` |
| D4 | Which of my own habits should change? | `collaboration.first_prompt_chars`, `collaboration.turns_before_first_edit`, `collaboration.reply_latency_s`, `friction.user_corrections`, `friction.interruptions`, `work.validation_commands` |
| D5 | Did this session succeed? | `outcome.status`, `outcome.validated`, `outcome.committed`, `outcome.ended_in_error` |
| D6 | What was risky? | `risk.secret_exposure`, `risk.secret_kinds`, `risk.destructive_commands`, `risk.edits_outside_workspace` |
| D7 | Can I trust these numbers at all? | `quality.capture_score`, `quality.unknown_events`, `quality.untimed_event_ratio`, `quality.unlinked_result_ratio`, `quality.usage_source`, `quality.cost_basis` |

Anything that does not serve one of those decisions is not a signal. That rule
removed roughly forty measurements from v0.

Signals remain separate from mining: mining produces memories to inject into
future prompts, signals produce comparable observations about how a rollout went.

## 2. Architecture

```
catalog/sessions.jsonl ──► included sessions (key, host, session_id, fingerprint, …)
                              │
normalized/<host>/<id>.events.jsonl ──┐
raw/<host>/<id>/…                   ──┴──► build_profile()   ← ONE pass per session
                                               │
                                               ▼
                                        SessionProfile   (facts: counts, timings,
                                               │          usage, commands, evidence)
                                               ▼
                                        50 registered signals   (pure reads)
                                               │
                                               ▼
                          signals/definitions.json    signals/sessions.jsonl
                                               │
                                               ▼
                             summarize() / compare() / weekly_trend()
```

Three properties follow from this shape and are non-negotiable:

1. **One scan.** v0 re-walked the event list once per signal (65 scans per
   session). v2 scans once into `SessionProfile`; signal functions are
   `profile -> value` and cost nothing.
2. **Facts and judgements are separated.** `profile.py` only extracts;
   `classify.py` holds the two inferential signals (`outcome.status`,
   `work.kind`) so their rules can be argued with and re-tuned against hand
   labels without touching extraction.
3. **Payload-driven, not type-driven.** Extraction keys on actor plus payload
   content, never on `event_type` alone, because the normalized taxonomy is
   changing (`usage`, `lifecycle`, `metadata` are being added) and the same
   logical thing is typed differently per importer and per schema version.

### Module map

| File | Responsibility |
| --- | --- |
| `profile.py` | `SessionProfile` and the single scan |
| `usage.py` | per-host token extraction, normalized into one bucket shape |
| `pricing.py` | `PricingMap`, model-id normalization, cost and basis |
| `commands.py` | base-command resolution, command categories, destructive/commit detection |
| `patterns.py` | lexicons: injected turns, corrections, interruptions, secrets, work cues |
| `classify.py` | `outcome.status` and `work.kind` rule sets |
| `registry.py` | `SignalDef`, `@register`, set digest, `evaluate()` |
| `definitions.py` | the 50 signals |
| `store.py` | incremental `definitions.json` + `sessions.jsonl` |
| `sources.py` | `SessionRecordLike` / `SessionSource` protocols and the file fallback |
| `summary.py` | `summarize`, `compare`, `rate`, `category_share`, `weekly_trend` |
| `analyze.py` | cross-session command/tool report (`retro analyze`) |

## 3. The session profile

`build_profile(key, host, session_id, events, raw_dir, record=None)` returns a
`SessionProfile`. The scan resolves the three things every host disagrees about.

**Calls versus results.** A result is any event with `event_type ==
"tool_result"`, or `actor == "tool"` with an action type, or `actor in
{"tool", "subagent"}` carrying a result key (`tool_use_id`, `output`, `result`,
`is_error`, `success`, `stdout`, `exit_code`). This matters: Claude and Copilot
keep the typed event (`command`, `file_edit`) on the *result*, and Copilot emits
large volumes of hook `attachment` events with `actor="tool"` that are not tool
results at all (64,250 of 91,580 events in a 40-session Copilot sample).

**Linking.** Results link to calls by id (`call_id`, `tool_id`, `tool_use_id`,
`toolCallId`, `tool_call_id`, `id`), then `parent_event_id`, then arrival order.
On the real archive this reaches ~100% linkage for Claude and Copilot and ~94%
for Codex (`web_search_end`, `patch_apply_end` carry no call id). The residual is
published as `quality.unlinked_result_ratio` rather than hidden.

**Failure.** A result failed if any of: `is_error is True`, `success is False`,
`status in {failed, error, timeout, cancelled, canceled}`, a non-empty `error`
field, a nested non-zero `exit_code` / `exitCode` / `returncode` (depth ≤ 5), or
text matching `(?:process exited with code|exited with code|exit code|exit
status)\D{0,3}(-?\d+)` with a non-zero code. The nested path matters because
Copilot reports `success: true` on the tool transport while
`data.toolTelemetry.metrics.exit_code` is 1; v0 counted those as successes, and
missed Codex entirely (403 of 411 Codex failures in the probe sample are visible
only as `Process exited with code N`).

**Human turns.** A `user`/`message` event counts as a human turn only if it is
not injected. Injected markers, all observed in the archive: `<command-name>`,
`<command-message>`, `<command-args>`, `<local-command-stdout>`,
`<local-command-stderr>`, `<local-command-caveat>`, `<system-reminder>`,
`<environment_context>`, `<user_instructions>`, `<editor_context>`,
`<attachment`, `<ide_selection>`, `<ide_opened_file>`, `Caveat: The messages
below were generated by the user while running local commands`, `This session is
being continued from a previous conversation`, `[Request interrupted by user`,
`API Error:`, `tool_use_id`; plus payload flags `isMeta` / `is_meta` /
`synthetic`, and content lists made only of `tool_result` / `image` / `document`
blocks. 13.7% of Claude user messages in the probe sample are injected.

**Time.** Timestamps are collected, **sorted**, then differenced. `active_s` sums
gaps ≤ 300 s (matching the catalog's definition) and `duration_s` is last minus
first. Sorting is required: 38 of 43 Claude sessions have non-monotonic
timestamps because attachments are backdated, and the naive in-order difference
under-reports active time by roughly an order of magnitude.

**Thresholds used by the scan.** Active-gap cap 300 s; human reply-gap cap
3600 s; result text scanned for exit codes capped at 20,000 chars; secret scan
capped at 100,000 chars per event and 8,000,000 chars per session (truncation is
reported through `quality.capture_score`); evidence capped at 5 event ids per
signal in the store.

### Token and cost normalization

Providers disagree about what `input_tokens` means. `usage.py` converts
everything to one bucket — `input` (fresh, uncached), `cache_read`,
`cache_create`, `output`, `reasoning` — so pricing never needs to know the host:

| Source | Location | Conversion |
| --- | --- | --- |
| Codex | normalized events, `payload.type == "token_count"`; `info.last_token_usage` (per-turn delta) or `info.total_token_usage` (cumulative, differenced); model from the preceding `turn_context` payload | `input = input_tokens − cached_input_tokens`, `cache_read = cached_input_tokens` |
| Claude Code | `raw/claude-code/<id>/transcript.jsonl`, assistant `message.usage` + `message.model`, deduplicated on `(message.id, requestId)`, `<synthetic>` skipped | fields are already disjoint; the `cache_creation` dict is summed when the scalar is absent |
| Copilot CLI / Agent Host | `raw/vscode-copilot/<id>/sidecars/session-store.json` → `assistant_usage_events[]` | `input = input_tokens − cache_read_tokens − cache_write_tokens`; `request_multiplier` summed into `usage.credits` |
| VS Code chat | `raw/vscode-copilot/<id>/session.snapshot.json` → `requests[]` (`modelTotals` preferred) | `input = promptTokens`, `output = completionTokens`; no cache split exists, so `cache_read = 0` |

Normalized events are tried first and the raw copy is the fallback, keyed on
payload shape. When the normalization workstream starts emitting `usage` events
for Claude and Copilot, those are picked up with no change here and
`quality.usage_source` flips from `raw` to `normalized`.

Pricing: model ids are normalized (`copilot/claude-sonnet-4.6` →
`claude-sonnet-4-6`; date and `-latest` suffixes stripped) and looked up in
`src/retro/pricing/litellm-pricing.json`, with a longest-prefix family fallback
when no entry matches. Every result carries a basis — `exact`, `family`, `mixed`,
`embedded`, `unpriced`, `none` — published as `quality.cost_basis`, so a dollar
figure can never silently be a guess. Models observed in the archive but absent
upstream (`claude-opus-5`, `claude-opus-4-8`, `claude-opus-5-5`,
`claude-sonnet-5`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-6-astra`)
were added to the snapshot at the vendor's published tier rate with
`"rate_basis": "family-estimate"`, which forces `cost_basis = family`.
Third-party models routed through Copilot whose list price is unknown
(`grok-4.6`, `mai-code-1.1-flash`, `gemini-3.7-flash`, `copilot/auto`,
`codex-auto-review`) are deliberately left unpriced.

For Copilot the dollar figure is a **counterfactual API list price**, not what
was billed; `usage.credits` (premium-request multipliers) is the real billing
unit there and is reported separately.

## 4. The catalogue

50 signals. Every one carries id, title, category, kind, unit, polarity, method,
version, description and applicability in `signals/definitions.json`; the
dashboard renders anything it does not special-case generically from that file.

Polarity convention: **`usage.*` is deliberately `neutral`.** A $5 session is not
worse than a $1 session; it is worse only at equal outcome. Judgement lives in
`efficiency.*` and in the metric layer, which normalizes by outcome.

| id | category | kind | polarity | definition |
| --- | --- | --- | --- | --- |
| `usage.cost_usd` | usage | money | neutral | token cost at list API prices for the models used; omitted when no model could be priced |
| `usage.tokens_total` | usage | count | neutral | input + cache read + cache write + output |
| `usage.tokens_input` | usage | count | neutral | fresh uncached prompt tokens (cache excluded for every host) |
| `usage.tokens_output` | usage | count | neutral | completion tokens, including reasoning when billed as output |
| `usage.tokens_cache_read` | usage | count | neutral | prompt tokens served from cache |
| `usage.tokens_cache_write` | usage | count | neutral | prompt tokens written into cache |
| `usage.duration_s` | usage | duration | neutral | last minus first timestamp (calendar span; resumed sessions span days) |
| `usage.active_s` | usage | duration | neutral | sum of sorted inter-event gaps, each capped at 300 s |
| `usage.credits` | usage | count | neutral | Copilot premium-request multipliers consumed |
| `activity.user_turns` | activity | count | neutral | messages a human actually typed (injected turns excluded) |
| `activity.assistant_turns` | activity | count | neutral | assistant message events (reasoning and tool traffic excluded) |
| `activity.tool_calls` | activity | count | neutral | assistant-initiated tool invocations |
| `activity.file_edits` | activity | count | neutral | `file_edit` events |
| `activity.files_touched` | activity | count | neutral | distinct edited paths |
| `activity.files_read` | activity | count | neutral | distinct read paths |
| `activity.commands` | activity | count | neutral | tool calls carrying a shell command line |
| `activity.edited_lines` | activity | count | neutral | ± lines over edits with a diff body; replacement-only edits count their line length |
| `outcome.status` | outcome | category | higher_better | `success\|partial\|failed\|abandoned\|unknown` (§5) |
| `outcome.validated` | outcome | boolean | higher_better | the last test / typecheck / lint / build command passed |
| `outcome.committed` | outcome | boolean | higher_better | a `git commit` ran, did not fail and did not report nothing to commit |
| `outcome.ended_in_error` | outcome | boolean | lower_better | the final tool result failed and was never retried |
| `friction.user_corrections` | friction | count | lower_better | human turns after the first that reject or reverse the agent's work |
| `friction.tool_errors` | friction | count | lower_better | failed tool results (any mechanism above) |
| `friction.tool_error_rate` | friction | ratio | lower_better | failed tool results ÷ tool results |
| `friction.interruptions` | friction | count | lower_better | user-initiated aborts |
| `friction.longest_error_streak` | friction | count | lower_better | longest run of consecutive failing results |
| `friction.edit_churn` | friction | ratio | lower_better | `(file_edits − files_touched) ÷ file_edits`, needs ≥ 2 edits |
| `friction.permission_denied` | friction | count | lower_better | permission requests denied or unapprovable |
| `friction.provider_errors` | friction | count | lower_better | API errors, rate limits, quota exhaustion, overloads |
| `friction.context_compactions` | friction | count | lower_better | context compaction events |
| `efficiency.cost_per_edit` | efficiency | money | lower_better | `usage.cost_usd ÷ activity.file_edits` |
| `efficiency.cache_read_share` | efficiency | ratio | higher_better | `cache_read ÷ (input + cache_read + cache_create)` |
| `efficiency.time_to_first_edit_s` | efficiency | duration | lower_better | first human turn → first edit |
| `efficiency.idle_share` | efficiency | ratio | neutral | `(duration_s − active_s) ÷ duration_s`, sessions longer than 60 s |
| `collaboration.first_prompt_chars` | collaboration | count | neutral | characters in the opening human turn after stripping injected blocks |
| `collaboration.turns_before_first_edit` | collaboration | count | neutral | human turns before the agent's first edit |
| `collaboration.reply_latency_s` | collaboration | duration | neutral | median human reply gap, each gap capped at 3600 s |
| `risk.secret_exposure` | risk | boolean | lower_better | a credential-shaped string appears anywhere in the rollout |
| `risk.secret_kinds` | risk | count | lower_better | distinct credential patterns seen |
| `risk.destructive_commands` | risk | count | lower_better | commands that can lose work or publish irreversibly |
| `risk.edits_outside_workspace` | risk | count | lower_better | absolute edited paths outside the session's working directory |
| `quality.capture_score` | quality | ratio | higher_better | 1 − penalties for unknown events, missing timestamps, unlinked results, missing usage, truncated scan |
| `quality.unknown_events` | quality | count | lower_better | events the importer could not classify |
| `quality.untimed_event_ratio` | quality | ratio | lower_better | events with no timestamp ÷ events |
| `quality.unlinked_result_ratio` | quality | ratio | lower_better | results not matched to a call ÷ results |
| `quality.usage_source` | quality | category | neutral | `normalized\|raw\|none` |
| `quality.cost_basis` | quality | category | neutral | `exact\|family\|mixed\|embedded\|unpriced\|none` |
| `work.kind` | work | category | neutral | `build\|debug\|refactor\|test\|explore\|review\|docs\|ops\|research\|chat\|other` (§5) |
| `work.primary_language` | work | category | neutral | most common language among edited files, else read files |
| `work.validation_commands` | work | count | neutral | test + typecheck + lint + build commands executed |

Returning `None` means *not applicable*, and the signal is omitted from the
stored `values` rather than written as a zero — the difference between "ran no
tests" and "we cannot tell" has to survive aggregation.

### Command categories

`classify_command` splits on `&&`, `||`, `;`, `|` and newlines (heredoc bodies
removed), resolves each segment's base program (`python -m pytest` → `pytest`,
`.venv/bin/ruff` → `ruff`, `npx tsc` → `tsc`, `uv run pytest` → `pytest`,
`npm run <script>` kept whole), and classifies base-first into `test`,
`typecheck`, `lint`, `build`, `vcs`, `package`, `search`, `inspect`, `net`,
`ops`, `run`, `other`. Base-first matters: matching the raw string scores
`rg 'pytest'` as a test run. `VALIDATION_CATEGORIES = {test, typecheck, lint,
build}`.

Destructive kinds: `recursive_delete`, `hard_reset`, `git_clean`, `force_push`,
`checkout_discard`, `branch_delete`, `drop_database`, `disk_write`,
`chmod_world`, `prune_all`, `publish`.

## 5. The two inferential signals

### `outcome.status` — first matching rule wins

Rule order encodes what dominates what: a provider limit or a user abort beats
any amount of completed work; an explicit refusal beats an edit count; a final
message that is still announcing the next step means the session stopped
mid-stride regardless of what came before it.

| # | Condition | Result | Confidence |
| --- | --- | --- | --- |
| 1 | no human turns, assistant messages or tool calls | `unknown` | 0.90 |
| 2 | final assistant message matches a usage-limit phrase (`you've hit your session limit`, `usage limit reached`, `out of premium requests`, `resets 8:50pm`) | `abandoned` | 0.80 |
| 3 | ≥1 interruption and nothing substantive after it (no commit, no passing validation, and either trailing failures, no edits, or ≥2 interruptions) | `abandoned` | 0.70 |
| 4 | last human turn matches the negative-closing lexicon (requires ≥2 human turns) | `failed` | 0.65 |
| 5 | final assistant message states inability (`I can't complete…`, `I was unable to…`) | `failed` | 0.70 |
| 6 | final assistant message ≤600 chars and forward-looking (`I'll…`, `Let me…`, `I'm checking…` in its first or last 200 chars) | `partial` | 0.60 |
| 7 | a commit succeeded | `success` | 0.90 (0.70 if validation failed) |
| 8 | ≥1 edit and the last validation run passed | `success` | 0.80 (0.65 if only lint/build ran) |
| 9 | last human turn matches the positive-closing lexicon | `success` | 0.60 |
| 10 | the last validation run failed | `partial` with edits, else `failed` | 0.65 / 0.60 |
| 11 | the session ends on an unretried tool failure | `partial` with edits, else `failed` | 0.60 / 0.55 |
| 12 | ≥1 edit and nothing above fired | `success` | 0.50 |
| 13 | an assistant message exists | `success` | 0.45 |
| 14 | otherwise | `unknown` | 0.50 |

A one-turn session has **no** closing message: its last human text is the opening
request, so rules 4 and 9 require ≥2 human turns. Without that guard an opening
prompt containing "check that everything works perfectly" is read as a verdict on
the work.

### `work.kind` — weighted cues, action mix as tie-breaker

Intent cues are regexes over the opening human turn, falling back to the session
title — the only intent signal for `codex exec` runs, which record no user
message. Each matching cue scores 2–3, and the cue appearing **earliest** gets
+2, because the primary intent is normally stated first; that single rule moved
agreement from 0.57 to 0.77.

Realized behavior then adds: no tools and some assistant output → `chat` +4; no
edits with reads or searches → `explore` +3; any edits → `build` +1; ≥70% of
edited files are tests → `test` +3; ≥70% docs → `docs` +3; edits plus tool errors
plus a debug cue → `debug` +2; ≥3 ops commands with ≤1 edit → `ops` +2; ≥3 web
searches with no edits → `research` +2; ≥2 test commands with no edits →
`test` +2.

Highest score wins; ties go to the more specific kind (`build` and `other` are
last). A run with no human prompt, no assistant output and no edits is `other`,
not a guess. Confidence is `min(0.85, 0.4 + 0.1 × margin + 0.1 if the winner had
a prompt cue)`.

## 6. Storage and incrementality

`signals/definitions.json`:

```json
{"schema": 2, "version": "<12-hex digest>", "signals": [{"id": "outcome.status", "...": "..."}]}
```

`signals/sessions.jsonl`, one line per included session, sorted by key:

```json
{"key":"codex/019d…","fingerprint":"<catalog fingerprint>","version":"<digest>",
 "values":{"usage.cost_usd":0.42,"outcome.status":"success"},
 "evidence":{"outcome.status":["evt-123"]},"confidence":{"outcome.status":0.9}}
```

Evidence lists are capped at 5 event ids; confidences equal to 1.0 are omitted.

The digest covers only the *semantic* fields of the registered set (id, category,
kind, unit, polarity, method, version, categories). Editing a description must
not trigger a full archive recompute; changing an algorithm must, and the way to
do that is to bump that signal's `version`.

A session is recomputed when `force` is set, when its catalog fingerprint
changed, or when the digest changed. Sessions the catalog no longer includes are
dropped; `prune=False` keeps them, which is what filtered runs such as
`retro signal run --host codex` use. Both files are written atomically, and the
v0 artifacts (`readings.jsonl`, `aggregates.json`, `summary.md` — 600 MB on this
archive) are deleted on the first write of the new store.

### Input contract

`update_signals` never imports `retro.catalog`. It needs four attributes:

```python
class SessionRecordLike(Protocol):
    key: str; host: str; session_id: str; fingerprint: str
```

and optionally reads `project` (mapping with `id` / `name`), `client`, `agent`,
`interactive`, `tags`, `started_at`, `models`, `title`, `cwd` / `cwd_resolved`.

`NormalizedSource` is the fallback: it walks `normalized/`, fingerprints with
`stat1:<size>.<mtime_ns>-<raw mtime_ns>`, and skips sessions whose recorded cwd is
under `/tmp`, `/private/tmp`, `/var/folders`, `/private/var/folders` or
`/var/tmp` — on the real archive that is ~30k programmatic `copilot -p`
evaluation runs.

### Python API

```python
from retro.signals import update_signals, load_signal_store, summarize

update = update_signals(layout, catalog, force=False)   # .added_keys .changed_keys
                                                        # .removed_keys .reused .total
store = load_signal_store(layout)                       # .definitions .version
                                                        # .values(key) .records()
summarize(store.all_values(), store.definitions)
```

## 7. The metric layer

Per-session values are raw material; every number a human reads is an aggregate.

**Per-signal statistics** (`summarize`): `n`, `sum`, `mean`, `median`, `p90`,
`min`, `max` for numeric kinds; `true` / `false` / `true_rate` for booleans;
`counts` / `top` for categories; plus `null` and `coverage` (share of sessions
where the signal applied) for every signal. Coverage is mandatory: a median over
20% of sessions is a different claim from a median over 95%.

**Rates, not means of ratios** (`rate(values, numerator, denominator)`): the
portfolio tool-error rate is `Σ errors ÷ Σ results`, never the mean of
per-session rates, which over-weights two-call sessions. Per-session ratio
signals exist for ranking single sessions, not for roll-ups.

**Success rate** (`category_share(values, "outcome.status", ["success"])`),
optionally counting `partial` as a half.

**Trends** (`weekly_trend`): ISO-week buckets of a numeric signal, to answer "is
my correction rate falling?" rather than "what is it?".

**Comparisons** (`compare(groups, definitions, min_sessions=5)`): every group is
summarized and tagged `sufficient: n >= min_sessions`. A group below the
threshold must never be rendered as a winner. For D1, group by
`(agent, work.kind)`, `(client, project)` and `(model, work.kind)`.

**Cost normalization.** Never compare raw `usage.cost_usd` across groups. Use
cost per successful session (`Σ cost ÷ successes`), cost per edit, or cost per
active hour, and always report `quality.cost_basis` coverage alongside, since a
`family` basis is an estimate. For Copilot, report `usage.credits` instead of
dollars.

## 8. Validation

### Method

60 sessions were sampled from the real archive, stratified by host (20 Codex, 15
Claude Code, 25 Copilot; all with activity; seed 20261009). For each one an
evidence bundle was dumped containing only raw material — title, every human
turn, first and last assistant messages, edited paths, the command list,
validation outcomes, commits, interruption count, durations — and **never** the
computed signal values. An independent labeler produced `outcome`, `work_kind`,
`corrections`, `validated`, `interrupted` and a confidence for each session.
Agreement was then measured against the store. The harness lives outside the repo
(`<scratch>/calibrate_dump.py`, `calibrate_score.py`); re-running it is the way to
re-validate after any rule change.

### Measured agreement (n = 60)

| Signal | Result |
| --- | --- |
| `outcome.status` | 87% exact agreement. `success` precision 0.86 / recall 1.00; `unknown` precision 1.00 / recall 0.93; `abandoned` precision 1.00 / recall 0.67; `partial` and `failed` recall ≈ 0 |
| `work.kind` | 77% exact agreement over 11 classes |
| `outcome.validated` | 98% agreement including the `null` (no validation ran) case |
| `friction.interruptions` | precision 1.00, recall 1.00 |
| `friction.user_corrections` | precision 0.78, recall 0.30; exact per-session count agreement 50/60 |

Targets for future changes: `outcome.status` ≥ 85% accuracy with `success`
precision ≥ 0.85; `work.kind` ≥ 75%; `friction.user_corrections` precision ≥ 0.75;
`outcome.validated` ≥ 0.95; `friction.interruptions` precision ≥ 0.95. A change
that trades precision for recall on corrections must be rejected: the signal
drives a behavioral recommendation, and a false "you corrected the agent nine
times" is worse than a missed one.

### Known biases and failure modes

- **`partial` is under-called.** It is reached only through a failing validation
  run, an unretried trailing error, or a mid-step final message. A session that
  produced edits and simply left the task half-finished scores `success` at
  confidence 0.50. Read `success` as "nothing contradicts delivery", not
  "verified delivery"; use `outcome.validated` and `outcome.committed` when a
  strong claim is needed.
- **`failed` is rare** for the same reason: explicit failure needs a negative
  closing message, a stated inability, or terminal errors with no edits.
- **Corrections measure explicit rejection only.** "give me the answer here"
  after the agent wandered off is a correction to a human and invisible to a
  regex. Treat the level as a floor and the trend as the signal.
- **Advice and research sessions** are scored by conversation shape, not by code
  outcomes; `work.kind` for them is noisier (`research` vs `explore` vs `other`
  is the dominant residual confusion, along with `ops` vs `build`).
- **`usage.duration_s` is calendar span.** Claude sessions are resumed across
  days (median 26 h on this archive against a median 49 min of `active_s`).
  Compare `active_s`, not `duration_s`.
- **Copilot dollars are counterfactual.** See §3.
- **`risk.secret_exposure` scans the rollout, not the repo.** A key printed by a
  tool the agent ran counts, which is right for "what entered the model's
  context" and wrong for "what is committed".
- **`collaboration.reply_latency_s` is unreliable per session on Claude**, whose
  transcript writes a turn's events in a batch with near-identical timestamps, so
  individual gaps collapse to milliseconds. The portfolio median (60 s on this
  archive) is sound; a single Claude session's value is not.
- **`activity.edited_lines` over-counts whole-file writes**, which carry no diff;
  its confidence scales with the share of edits that had one.
- **`friction.provider_errors` is regex-shaped** (confidence 0.6) because
  importers currently drop structured API errors — 129 raw Claude
  `system/api_error` records never reach the normalized stream.
- **Codex `exec` runs record no user message.** 1,154 of 1,310 Codex sessions
  have no human turn and no tool calls at all; they score `unknown` / `other` and
  should be excluded by the catalog's `builtin.empty` rule.

## 9. Performance

Measured on the real archive (4.4 GB of normalized events, 31,793 sessions, 1,651
of them included after temp-directory exclusion), single process, Python 3.9.6:

| Operation | Measured | Target |
| --- | --- | --- |
| Full recompute of every included session | 162 s | < 15 min |
| No-op update with a warm catalog | 0.04 s | < 5 s |
| No-op update with the file fallback (stats ~30k raw dirs) | 17 s | n/a |
| `load_signal_store` | 0.02 s | — |
| `summarize` over 1,651 sessions | 0.03 s | — |
| Store size | 2.3 MB `sessions.jsonl` + 23 KB `definitions.json` | < 50 MB |

Against v0: 600 MB → 2.3 MB on disk, and a re-run with nothing changed goes from
a full rebuild to a fingerprint comparison.

## 10. CLI

```bash
retro signal list [--category usage|activity|…] [--describe]
retro signal run [--host H] [--session-id A,B] [--force] [--include-scratch]
retro signal show <host> <session-id>
retro signal summary [--signal a,b] [--json]
retro analyze [--host H]            # command/tool mix and failure rates
```

`signal run` is incremental; `--force` recomputes everything; `--include-scratch`
disables the temp-directory exclusion in the fallback source. Filtered runs keep
the rest of the store intact.

## What was dropped, and why

**From v0 (65 signals → 50, with almost no overlap):**

- `trajectory_*` (25 signals: action-category counts and ratios, 4-gram
  repetition, sequence entropy, phase transitions, explore/exploit balance). They
  described trajectory *shape* without connecting to a decision, duplicated one
  another (every category produced both a count and a ratio), and depended on an
  action taxonomy that could not be validated against anything. The two ideas
  worth keeping were re-expressed as validated, decision-linked signals:
  "repeated action without progress" → `friction.longest_error_streak` and
  `friction.edit_churn`; "termination without validation" → `outcome.validated`
  plus `work.validation_commands`.
- `user_satisfaction_lexical`: a three-way lexical label on the last user
  message, emitted at confidence 0.4 for every session, including those whose
  last message was also their first. Its useful half is now rules 4 and 9 inside
  `outcome.status`, guarded by the ≥2-turn requirement.
- `git_commits_made_during`: it shelled out to `git log` in the session's cwd and
  produced nothing when the directory had moved or disappeared — which, for an
  archive full of worktrees, is most of them. Replaced by `outcome.committed`,
  read from the commands the agent actually ran.
- `capture_gap_signal`, `failed_command_count` / `_ratio`,
  `secret_exposure_count` / `_signal`, `command_count`, `unique_files_edited`,
  `unique_files_read`, `web_search_count`, `user_message_count`,
  `assistant_message_count`, `session_duration_seconds`,
  `time_to_first_edit_seconds`, `user_correction_count`, `interrupted_signal`,
  `unknown_event_count`, `events_without_timestamps` — all survive under the new
  ids with corrected definitions (human-turn filtering, cross-host failure
  detection, sorted timestamps, precision-tuned correction patterns).

**From `specs/experimental_trajectory_signals_spec.md`** (Bouzenia & Pradel,
*Understanding Software Engineering Agents: A Study of Thought-Action-Result
Trajectories*, ASE 2025 — the one external source that changed a design decision
here): kept the finding that **termination without test validation, and repeated
action without new information, correlate with failure**, which is why
`outcome.validated`, `work.validation_commands`, `friction.longest_error_streak`
and `friction.edit_churn` exist, and why `outcome.status` rule 8 requires a
passing validation run. Dropped the thought/action/result iteration model, the
eight-way action taxonomy, n-gram mining and the five semantic adjacency
relations: the paper's own implementation stores those adjacency labels as
hand-annotated CSVs, so there is no automatic classifier to reproduce, and an LLM
judge is out of scope for a local-first tool. That spec is superseded by this one.

**Not adopted:** LLM-judged signals. Every signal here is deterministic and
re-derivable from `raw/` + `normalized/`; adding a judge would make the store
non-reproducible and would need its own validation set. If one is added later it
must use `method: "llm"`, carry a model id in its `version`, and be measured on
the same hand-labeled sample.
