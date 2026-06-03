# Experimental Trajectory Signals Spec

## Purpose

Add a new experimental signal layer inspired by Bouzenia and Pradel's ASE 2025 trajectory study, "Understanding Software Engineering Agents: A Study of Thought-Action-Result Trajectories".

The paper studies software engineering agents as sequences of thought/action/result triples, then compares successful and failing runs by trajectory length, token cost, action categories, frequent action 4-grams, debugging anti-patterns, and semantic relationships among adjacent trajectory components.

Retro already has the right substrate: normalized rollout events, pure signal functions, evidence-linked readings, JSONL aggregates, and dashboard attachment. The missing piece is a trajectory abstraction over normalized events plus signals that expose agent workflow shape.

This spec defines what to add so a coding agent can implement the experimental metrics without changing the existing capture contract.

## References

- Paper: https://www.software-lab.org/publications/ase2025_trajectories.pdf
- Implementation/data repo: https://github.com/sola-st/llm-agents-study
- Repo parser model: `parsers/trajectory.py` defines `Iteration(thought, action, result)` and `Trajectory(iterations)` with `total_actions` and `action_redundancy`.
- Repo serialization model: `parsers/serialize.py` emits five pair views: `thoughts_actions`, `thoughts_thoughts`, `actions_actions`, `results_actions`, and `results_thoughts`.
- Repo data layout: per-agent CSV folders store `actions_categories/`, `action_action/`, `thought_action/`, `thought_thought/`, `result_action/`, and `result_thought/` annotations.

## Paper Takeaways To Productize

The paper's reusable ideas:

- Normalize each rollout into a sequence of iterations containing a thought, an external action, and the result of that action.
- Categorize actions into eight high-level debugging activities: `explore`, `locate`, `search`, `reproduce`, `generate_fix`, `run_tests`, `refactor`, and `explain`.
- Compute trajectory-level cost and complexity: iteration count and token consumption.
- Mine action n-grams, especially 4-grams, because `n in {4,5,6}` worked well and the paper selected 4.
- Flag failure-correlated anti-patterns:
  - repeated identical actions without using new information,
  - repeated fix generation without testing,
  - task termination without proper test validation.
- Model five adjacent relationships:
  - thought -> action: alignment / misalignment,
  - thought -> next thought: follow-up / refinement / redundancy / divergence / contradiction,
  - action -> next action: follow-up / refinement / repetition / divergence,
  - result -> next thought: follow-up / refinement / no-influence / misinterpretation,
  - result -> next action: informative / triggering / no-influence.

The initial Retro implementation should automate the structural and action-sequence metrics first. Semantic relationship labels should start as opt-in `llm_judge` or lightweight heuristic experiments, because the paper's published implementation stores these annotations as manual CSV data rather than a robust automatic classifier.

## Current Retro Fit

Existing modules:

- `src/retro/schema.py`: normalized event model. Event types already distinguish messages, tool calls/results, file reads/edits, commands, reasoning, errors, and session boundaries.
- `src/retro/signals/base.py`: `SessionContext`, `SignalReading`, `register`, `reading`.
- `src/retro/signals/heuristics.py`: pure event-walk signals.
- `src/retro/signals/runner.py`: signal execution, aggregation, and `summary.md` rendering.
- `src/retro/cli.py`: `retro signal list/run/show`.
- `dashboard/build_dashboard.py`: attaches readings to dashboard sessions.

Add new code without disrupting v0 signals:

```text
src/retro/trajectory.py                 # normalized events -> trajectory iterations
src/retro/signals/trajectory.py         # experimental trajectory signals
tests/                                  # focused unit fixtures, if the repo adds tests
```

Also import the new signal module from `src/retro/signals/__init__.py` so decorators register on CLI startup.

## Data Model

Add a small internal dataclass layer. Do not add these to the public JSONL schema yet.

```python
ActionCategory = Literal[
    "explore",
    "locate",
    "search",
    "reproduce",
    "generate_fix",
    "run_tests",
    "refactor",
    "explain",
    "permission",
    "error",
    "other",
]

@dataclass(frozen=True)
class TrajectoryStep:
    index: int
    thought_event_ids: tuple[str, ...]
    action_event_id: str
    result_event_ids: tuple[str, ...]
    thought_text: str
    action_text: str
    result_text: str
    action_category: ActionCategory
    action_fingerprint: str
```

Build steps by scanning normalized events in sequence:

- Treat `reasoning` and assistant `message` events immediately before an action as thought candidates.
- Treat assistant `tool_call`, assistant `command`, `file_read`, and `file_edit` events as actions.
- Treat following `tool_result`, tool `command`, `error`, and command output-like events as results until the next thought/action begins.
- Preserve evidence event ids in every signal that points to suspicious steps.

For Codex and Claude rollouts, not every event stream will have explicit hidden reasoning. The builder should still produce steps with empty `thought_text` when only actions/results are visible. This lets structural signals work even when semantic signals abstain.

## Action Categorization

Implement deterministic categorization first. Keep confidence in metadata, not in the category enum.

Recommended classifier order:

| Category | Event/payload clues |
| --- | --- |
| `search` | `rg`, `grep`, `find`, tool names containing search, web search invocations, text search APIs |
| `explore` | `ls`, `pwd`, `sed`, `cat`, `head`, `tail`, file reads, opening files, listing directories |
| `locate` | stack traces, symbol lookup, line-specific inspection, AST or definition lookup, `git blame`, references |
| `generate_fix` | `file_edit`, `apply_patch`, write/edit tools, patch generation, code modification commands |
| `run_tests` | `pytest`, `npm test`, `cargo test`, `go test`, `mvn test`, `gradle test`, `tox`, `tsc`, lint/typecheck when used as validation |
| `reproduce` | adding/running repro scripts, new failing tests, commands/messages containing reproduce/regression/failing test |
| `refactor` | formatting, renaming, cleanup, `black`, `prettier`, `ruff --fix`, comments/doc-only edits |
| `explain` | assistant summary/planning message with no external action, final answer, explicit explanation |
| `permission` | permission prompts/approvals |
| `error` | error events or failed parser/command states |
| `other` | anything not mapped |

Important improvement over the paper: keep both the high-level category and a normalized action fingerprint. The category detects workflow phase; the fingerprint detects exact repetition.

Fingerprint rules:

- Include event type, tool name/command executable, normalized command arguments, file path, and target symbol if available.
- Strip volatile substrings: temp paths, line numbers when not semantically important, UUIDs, timestamps, ANSI codes, memory addresses.
- Lowercase and collapse whitespace.
- Store first 12 hex chars of SHA-256 in metadata where compactness matters, but preserve the human-readable normalized string in debugging metadata for flagged evidence.

## Signal Catalog

All names below are experimental but stable enough for a coding agent to implement. Put them in `src/retro/signals/trajectory.py`.

### Structural Signals

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_step_count` | activity | numeric | count | Number of inferred thought/action/result steps. Paper equivalent: trajectory length. |
| `trajectory_action_category_histogram` | activity | categorical | label | Emit one reading per category? Avoid that. Prefer one `text`/metadata reading or add aggregate support for dict values. See implementation note below. |
| `trajectory_unknown_action_ratio` | risk | numeric | ratio | Fraction of steps categorized as `other` or `error`. High values mean the classifier is missing host-specific behavior. |
| `trajectory_phase_coverage` | activity | numeric | ratio | Unique core categories seen divided by eight paper categories. |
| `trajectory_validation_presence` | outcome | boolean | flag | True when a rollout includes at least one `run_tests` or credible validation command after the first edit. |

Implementation note: the current aggregator supports numeric, boolean, and categorical scalar values. For histograms, prefer several numeric signals with the pattern `trajectory_action_count_<category>` and `trajectory_action_ratio_<category>`, generated by one registered function returning multiple readings. This fits the current runner without changing aggregation.

### Category Count Signals

Return one reading per category:

```text
trajectory_action_count_explore
trajectory_action_count_locate
trajectory_action_count_search
trajectory_action_count_reproduce
trajectory_action_count_generate_fix
trajectory_action_count_run_tests
trajectory_action_count_refactor
trajectory_action_count_explain
trajectory_action_count_other
```

Return one ratio per category:

```text
trajectory_action_ratio_<category>
```

Metadata for each reading:

- `total_steps`
- `category`
- `first_step_indices`
- `first_evidence_refs`

### Sequence Signals

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_action_redundancy` | risk | numeric | count | Same as the study repo: sum of repeated action fingerprints beyond the first occurrence. |
| `trajectory_consecutive_repetition_count` | risk | numeric | count | Count adjacent steps with identical category or identical fingerprint. |
| `trajectory_max_repetition_run` | risk | numeric | count | Longest run of repeated category/fingerprint. |
| `trajectory_4gram_repetition_ratio` | risk | numeric | ratio | Fraction of 4-gram windows that repeat within the session. |
| `trajectory_4gram_top` | activity | text | label | Most frequent 4-gram category sequence, with count in metadata. |
| `trajectory_sequence_entropy` | activity | numeric | score | Shannon entropy over action categories. Very low entropy means the rollout is stuck in a narrow loop. |
| `trajectory_phase_transition_count` | activity | numeric | count | Count category changes. Too few suggests loops; too many may suggest thrashing. |

Default n-gram size: 4, matching the paper. Add a helper that accepts `n=4` so later experiments can use 5 or 6 without duplicating code.

### Anti-Pattern Signals

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_repeated_action_without_progress` | risk | boolean | flag | True when the same fingerprint repeats in a short window without an intervening result difference, edit, or category shift. |
| `trajectory_fix_without_validation_count` | risk | numeric | count | Count `generate_fix` steps not followed by `run_tests` within the next K steps. Default K=4. |
| `trajectory_fix_validation_latency_steps` | cost | numeric | count | Median number of steps from `generate_fix` to the next `run_tests`. |
| `trajectory_premature_finish_without_validation` | risk | boolean | flag | True when the final assistant response occurs after edits but no validation step follows the last edit. |
| `trajectory_test_fix_loop_count` | risk | numeric | count | Count repeated `generate_fix -> run_tests` cycles where validation keeps failing and no exploration/search step occurs between cycles. |

These directly productize the paper's anti-patterns and add a latency metric that is easier to trend over your own rollouts.

### Result-Sensitivity Signals

These are the most useful "better than the paper" additions for live rollout improvement. They are heuristic proxies for result -> thought/action influence, avoiding an LLM judge at first.

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_failed_result_recovery_steps` | cost | numeric | count | Median steps from a failed command/result to the next different category or changed fingerprint. |
| `trajectory_failure_ignored_count` | risk | numeric | count | Failed result followed by an unrelated final/explanation or same action repeat without changed parameters. |
| `trajectory_result_token_reuse_ratio` | activity | numeric | ratio | Token overlap between salient result terms and next action/thought text. Proxy for result sensitivity. |
| `trajectory_error_to_search_or_edit_ratio` | outcome | numeric | ratio | Fraction of failed results followed by search/explore/edit/test rather than final answer or repetition. |

Implementation detail for salient result terms:

- Extract identifiers, paths, exception names, failing test names, command names, and quoted strings from result text.
- Count whether any appear in the next thought/action text.
- Do not use raw English stopwords.

### Token/Cost Signals

The paper uses total tokens as a key cost metric. Retro's dashboard already estimates token/cost data from events. Add signal-level wrappers only if the token fields are consistently present.

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_input_tokens` | cost | numeric | tokens | Sum known input tokens across model events. |
| `trajectory_output_tokens` | cost | numeric | tokens | Sum known output tokens. |
| `trajectory_total_tokens` | cost | numeric | tokens | Input + output + cache tokens where available. |
| `trajectory_tokens_per_step` | cost | numeric | tokens/step | Total tokens divided by inferred step count. |

If token metadata is missing for a host, return no reading or a null reading with `confidence=0.0` and metadata reason `missing_token_fields`.

### Optional LLM-Judge Signals

Keep these out of the default run until cost and privacy controls exist:

| Signal | Group | Kind | Unit | Description |
| --- | --- | --- | --- | --- |
| `trajectory_thought_action_misalignment_count` | risk | numeric | count | LLM labels thought/action pairs as aligned or misaligned. |
| `trajectory_thought_redundancy_count` | risk | numeric | count | LLM labels redundant consecutive thoughts. |
| `trajectory_result_misinterpretation_count` | risk | numeric | count | LLM labels result -> next thought misinterpretations. |
| `trajectory_no_influence_count` | risk | numeric | count | LLM labels result -> next thought/action no-influence cases. |

Prompt shape should mirror the paper's five relationship types, but the output must be strict JSON with one label per pair and a short rationale. Store only labels, confidence, rationale snippets, and evidence refs in signal metadata.

## Improved Experimental Ideas

Add these after the paper-faithful metrics land.

### 1. Verification Debt

Measure edits that accumulate without validation:

```text
verification_debt_steps = max number of generate_fix/file_edit steps since last run_tests
verification_debt_files = edited files not covered by any later validation command
```

This is more actionable than a binary "fix without testing" signal because it shows whether the agent is taking a small risk or building a large unverified patch.

### 2. Adaptive Recovery Score

After a failure, a strong agent should change tactics. Score the next 1-3 steps:

- +1 for changed fingerprint,
- +1 for category shift to search/explore/locate after repeated failure,
- +1 for editing after a specific diagnosis,
- -1 for same action same parameters,
- -1 for final answer after failure without validation.

Emit `trajectory_adaptive_recovery_score` as an outcome numeric score in `[-1, 3]`, averaged across failures.

### 3. Exploration-Exploitation Balance

Group categories:

- exploration: `explore`, `search`, `locate`, `explain`
- exploitation: `generate_fix`, `run_tests`, `reproduce`, `refactor`

Emit:

- `trajectory_exploration_ratio`
- `trajectory_exploitation_ratio`
- `trajectory_balance_score = 1 - abs(exploration_ratio - exploitation_ratio)`

This gives you a quick way to compare rollouts where one model jumps to edits too early versus another reads forever.

### 4. Human-Intervention Coupling

Retro has user correction signals. Link them to trajectory state:

- `trajectory_user_correction_after_repetition`: user correction within 3 events after a repetition run.
- `trajectory_user_correction_after_unvalidated_fix`: user correction after final/edit without validation.
- `trajectory_time_to_recover_from_user_correction`: steps until changed category/fingerprint after correction.

This directly supports "see how they work based on my rollout" because your correction moments become labels for bad trajectory behavior.

### 5. Outcome-Weighted Pattern Mining

Once there are enough sessions, add a cross-session artifact:

```text
rollout-memory/signals/trajectory_patterns.json
```

Contents:

- top 4-grams overall,
- top 4-grams for positive/negative lexical outcomes,
- 4-grams overrepresented in interrupted or corrected sessions,
- per-host differences.

Use log odds ratio with add-one smoothing rather than raw frequency. Raw frequency mostly finds common behavior; overrepresentation finds signals worth changing.

## Implementation Plan

### Phase 1: Trajectory Builder

Add `src/retro/trajectory.py`.

Functions:

```python
def build_trajectory(events: Sequence[NormalizedEvent]) -> list[TrajectoryStep]: ...
def categorize_action(ev: NormalizedEvent, text: str) -> ActionCategory: ...
def action_fingerprint(ev: NormalizedEvent, text: str) -> str: ...
def ngrams(labels: Sequence[str], n: int = 4) -> list[tuple[str, ...]]: ...
def shannon_entropy(labels: Sequence[str]) -> float: ...
def is_failed_result(ev: NormalizedEvent) -> bool: ...
```

Use `retro.utils.event_text` and existing `_is_failed` logic as a starting point, but avoid importing private helpers from `heuristics.py`. If necessary, move shared failure detection into `retro.utils` in a small separate patch.

### Phase 2: Signals

Add `src/retro/signals/trajectory.py`.

Register:

- structural signals,
- category count/ratio signals,
- sequence signals,
- anti-pattern signals,
- result-sensitivity heuristic signals.

Import from `src/retro/signals/__init__.py`.

Each suspicious boolean/count signal should include:

- step indices,
- evidence refs,
- categories/fingerprints involved,
- short text snippets, truncated and redacted using existing secret-risk conventions where relevant.

### Phase 3: Aggregation Compatibility

Do not change the aggregator for v1. Use scalar readings only.

For "top 4-gram" text readings, put structured details in metadata:

```json
{
  "ngram": ["explore", "search", "generate_fix", "run_tests"],
  "count": 3,
  "windows": [[4, 7], [11, 14], [20, 23]]
}
```

### Phase 4: Dashboard

No dashboard schema change is required because readings already attach to each session. Add a small curated display later:

- risk badges for anti-pattern booleans,
- sparkline/histogram for action categories,
- table columns for step count, sequence entropy, validation presence, verification debt, adaptive recovery.

### Phase 5: Optional Pattern Artifact

After enough local sessions exist, add a separate runner or dashboard builder step that reads `readings.jsonl` and normalized events to produce `trajectory_patterns.json`.

Do not overload `aggregate_readings`, because pattern mining is cross-session and sequence-aware rather than per-signal summary math.

## Testing Plan

Create small synthetic normalized event fixtures covering:

- search -> edit -> test happy path,
- repeated identical search commands,
- edit -> final answer without test,
- edit -> test fail -> same edit/test loop,
- failure result followed by changed search,
- missing reasoning text,
- unknown command category.

Assertions:

- `build_trajectory` creates stable step counts and evidence ids.
- category classifier maps common Codex/Claude events correctly.
- fingerprints collapse whitespace and volatile values but distinguish changed commands.
- repetition and anti-pattern signals point to the correct evidence.
- signals return no reading or null readings gracefully when data is absent.
- `retro signal list` includes the new signal names after importing the module.

## Acceptance Criteria

- Running `retro signal run --signal trajectory_step_count` works for existing normalized Codex and Claude sessions.
- New signals do not break existing `retro signal run`, `retro signal show`, or dashboard build.
- Every new reading has `group`, `kind`, `method`, `session_id`, `host`, `value`, `confidence`, and reproducible evidence refs when applicable.
- Unknown or low-confidence categorization is explicit via `trajectory_unknown_action_ratio`.
- Anti-pattern signals include enough metadata for a human to inspect the relevant steps in rendered rollout markdown.
- No LLM calls are made by default.

## Non-Goals

- Do not replicate the paper's manual annotation CSV workflow as-is.
- Do not add a database.
- Do not require cloning `sola-st/llm-agents-study`.
- Do not expose hidden model reasoning that is not present in captured normalized events.
- Do not treat these experimental signals as objective truth; they are rollout diagnostics and should carry confidence metadata.

