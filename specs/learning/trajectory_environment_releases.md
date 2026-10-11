# Trajectory-derived environment releases

Status: implemented task/scorer releases and terminal-reward episodes; an RL
optimizer and predictive-validity study are not implemented.

Updated: 2026-10-10

This is the implementation companion to
[the task/scorer contract](rollout_task_scorer_pipeline_spec.md) and
[the user-distribution research design](user_task_distribution_benchmark.md).
The existing localization benchmark remains a separate product.

## 1. Product and information boundaries

```text
captured trajectories + explicit Git proof
  -> selection.json, including every rejection
  -> immutable SourceBundle: events + base/outcome trees + project context
  -> task definitions supported by user-message event IDs
  -> hidden behavioral scorer + reference + mutant cases
  -> repeated scorer validation + independent audit
  -> published task candidate pool
  -> curated, frozen suite: groups, train/validation/test, content hashes
  -> frozen experiment setup + Ghostlab agent trials OR external training episodes
  -> attested terminal scores + source-normalized reports
  -> Tasks / Evals / Experiments dashboard
```

The actor receives exactly the public task prompt, initial repository, limits
and public environment. Source trajectories, accepted changes, reference
solutions, hidden tests, audits and provenance stay on the creator/scorer side.
Static dashboard projections expose provenance summaries, hashes and validation
results, not scorer source, accepted patches or candidate code. The opt-in local
operator server permits on-demand private grader inspection without changing
the public task export or exposing hidden code to the candidate agent.

This is not a claim that every captured session becomes a good task. Missing
state proof, ambiguous goals, external effects, weak scorers and nondeterminism
are explicit rejection states.

## 2. Evidence-backed decisions

Research used Paperclip's arXiv search/read capability and public primary
project documentation. These sources change implementation decisions; their
reported model rankings or dataset popularity are not a design requirement.

| Primary source | Relevant result or construction method | Consequence here |
| --- | --- | --- |
| [SWE-bench](https://arxiv.org/abs/2310.06770), ICLR 2024 | Repository tasks anchored at commits with fail-to-pass and pass-to-pass checks | Pin the initial state; require observed behavioral change and protect regressions |
| [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/), 2024 | Human review checks whether issue descriptions are clear and tests accept valid alternative solutions | Generation is not validation; require a separate audit and a construct-preserving alternative |
| [SWE-Gym](https://arxiv.org/abs/2412.21139), 2024/2025 | Executable training tasks use repositories disjoint from SWE-bench | Repository-grouped train/evaluation separation is the default |
| [SWE-smith](https://arxiv.org/abs/2504.21798), 2025 | Synthetic task validity comes from executable, test-breaking changes at known repository states, not historical fidelity | A validated anchored reconstruction can be useful, but must not be called exact replay |
| [R2E-Gym](https://arxiv.org/abs/2504.07164), 2025 | Commit-derived environments and generated behavioral checks support agent training and verification | Reuse one task/environment/verifier object for evaluation and terminal training rewards |
| [SWE-rebench](https://arxiv.org/abs/2505.20411), 2025 | Fresh tasks and continuous construction address static-benchmark contamination | Keep a candidate pool separate from immutable releases; do not claim private historical tasks are necessarily uncontaminated |
| [UTBoost](https://aclanthology.org/2025.acl-long.189/), ACL 2025 | Test augmentation exposes benchmark patches incorrectly accepted by insufficient tests | Require behavior-changing mutants, preserved-behavior alternatives and regression mutants |
| [Large Language Monkeys](https://arxiv.org/abs/2407.21787), 2024 | Repeated identical solutions can receive different results from flaky repository tests | Re-run base, oracle and mutants; distinguish test instability from agent failure |
| [Automated Unit Test Improvement at Meta](https://arxiv.org/abs/2402.09171), 2024 | Repeated execution is used to reject flaky generated tests | A single successful scorer execution is insufficient evidence |
| [Harbor](https://harborframework.com/docs/tasks), execution framework | Instructions, environments, verifiers and solutions are separate task components; rollouts can feed RL | Export actor inputs separately from trusted-harness reward packages; do not couple tasks to a specific optimizer |
| [AgentGym](https://arxiv.org/abs/2406.04151), 2024 | A unified environment interface supports multiple agent-training environments | Provide framework-neutral reset/terminal-reward contracts rather than pretend this is already a Gymnasium action environment |

The literature supports the direction of the gates, not Retro's exact
`0.20 / 0.90 / 0.50 / 0.05` calibration. Those values are explicit engineering
choices inherited from the task/scorer contract, not paper-derived guarantees.
An independent model audit is also not equivalent to professional human review.

Ghostlab's checked local 0.3.0 CLI exposes `artifact-run` and `scorer-run`.
Retro uses those public commands, not conversational substring grading.
Public Ghostlab overview documentation may lag these repository-execution
capabilities; inspect the installed CLI before a production run.

## 3. Exact replay versus anchored reconstruction

An exact replay requires a captured clean start or a successful, pre-mutation
HEAD observation paired with clean porcelain status. Timestamp-selected commits
and current HEAD are never evidence of a historical initial state.

An explicitly opted-in anchored reconstruction uses the parent of a commit
observed in the trajectory, plus a clean-status observation. It defines a
new task at that immutable commit; it does not assert that all of the
developer's original pre-commit editor state has been recovered. It must still
pass environment, prompt, scorer, alternative-solution and audit gates.

Suite tasks carry:

- `exact_replay`: user-derived replay with exact start proof;
- `anchored_reconstruction`: user-derived replay at a known-parent anchor;
- `adjacent_synthesis`: an explicitly generated adjacent task, not historical
  user behavior.

`allow_reconstructed` defaults to false. Dirty-start replay is still rejected.
The distinction is visible in both the suite manifest and dashboard.

`select --repo-root` reconnects old worktree paths to an explicit current
checkout. It preserves the original path and capture files, rechecks commit and
tree identities, and carries the override through `bundle --selected-only`.
It never repairs missing start proof by reading the checkout's current HEAD.

Repository snapshots preserve tracked bytes and executable modes, including
files marked `export-ignore` and literal `export-subst` strings. Before use, a
snapshot's tree is recomputed without host Git filters or hooks and compared to
the declared Git tree. Agent workspaces and portable exports contain no `.git`,
future objects, branches, reflogs or accepted-solution history.

## 4. Trusted-release gate

`curate` only freezes published candidates that pass all of these:

1. Public prompt agrees with the evidence-backed task definition.
2. Public artifact names are exactly `task.json`, `prompt.txt`,
   `base.bundle`, `environment.json`.
3. Source, repository, base SHA and tree agree; provenance grade satisfies the
   explicit policy.
4. Goal, state and scorability confidence are finite and at least the policy
   threshold (default `0.8`). These are generator assessments, not independently
   calibrated probabilities.
5. By default every reward component is deterministic. Judged/performance
   scoring is an evaluation-only opt-in and cannot populate a training split.
6. Base, oracle, no-op, construct-changing, construct-preserving and regression
   cases each have at least two valid scored executions; repeatability passes
   and validation attests read-only candidates and isolated scorer execution.
7. Numeric gates are rechecked from the validation evidence, not inferred from
   a `passed: true` flag:
   - base fails a requested hard gate and total is at most `0.20`;
   - oracle passes every hard gate and total is at least `0.90`;
   - no-op reproduces the failing base score;
   - changing mutant drops its named component by at least `0.50`;
   - preserving alternative changes the total by at most `0.05`;
   - regression mutant fails its named gate and total is `0`.
8. An accepting, non-skipped independent scorer audit has no leakage or missing
   observable findings.
9. Every frozen file is hashed, the base tree materializes correctly, and task
   directories do not contain symlinks. Release provenance uses the verified
   canonical scorer-package hash. A legacy hash that the runtime can verify
   must be republished under canonical metadata before freezing a new release;
   immutable historical releases are not rewritten.

Older validations without the targeted component summaries or repeated runs
must be rebuilt; they are not silently upgraded to stronger evidence.

Rejected candidates and split-allocation failures persist in `curation.json`.
Zero eligible tasks means a rejected release, not an empty successful benchmark.

## 5. Curation and split algorithm

The default policy is an **evaluation-only** release:
`train=0`, `validation=0`, `test=1`, grouped by repository. Export/reset/reward
commands default to `train`, so they cannot accidentally use this held-out data.

An explicit policy can be supplied outside the evaluated repository:

```json
{
  "schema_version": "retro-task-curation-policy-v1",
  "split_unit": "repository",
  "split_ratios": {"train": 0.8, "validation": 0.1, "test": 0.1},
  "seed": 42,
  "min_confidence": 0.8,
  "include_judged": false,
  "allow_reconstructed": false,
  "overrides": {}
}
```

Task overrides identify a task ID and may provide `include`, `family`, `tags`,
`priority` and a mandatory `reason`. Exclusion is a recorded curation decision.
Sampling priorities must be finite in `[0.01, 1000]`; they never affect reward
or benchmark aggregation.

Union-find groups all tasks from one source and explicitly related families,
and all tasks in one repository by default. Within a repository, identical
prompts and token-trigram Jaccard similarity of at least `0.85` (at least five
distinct trigrams) also join groups. Identical repository/base/prompt/environment
inputs are deduplicated after grouping; larger sampling priority wins, then
replay over adjacent, then task ID. Grouping before deduplication preserves
transitive relationships.

Whole groups are ordered by `sha256([seed, group_id])`. Largest-remainder
allocation implements the requested ratios, reserving at least one group for
each positive split. Too few independent groups is an error, not random
task-level fallback.

`split_unit: "source"` is explicit within-repository interpolation, not
repository generalization. The manifest/dashboard warn about shared repository
history. Neither trigram matching nor family labels prove semantic
decontamination; held-out future-real tasks remain necessary to validate the
broader user-distribution hypothesis.

Frozen suites live under `task-scorer/suites/<suite_id>/` and copy task artifacts.
A suite ID cannot be reused for changed inputs. Rebuilding the candidate pool
does not mutate existing releases or historical evaluation thresholds.

## 6. Training episodes and evaluation cohorts

```bash
retro benchmark taskset curate --name personal-v1 --suite release-v1 \
  --policy /outside/repo/curation.json

retro benchmark taskset run --name personal-v1 --suite release-v1 --split test \
  --agent agents/candidate-a.json --seeds 0,1,2 --eval heldout-v1
retro benchmark taskset run --name personal-v1 --suite release-v1 --split test \
  --agent agents/candidate-b.json --seeds 0,1,2 --eval heldout-v1

retro benchmark taskset export --name personal-v1 --suite release-v1 \
  --split train --out /external/data/train-v1 --include-rewards
retro benchmark taskset reset --name personal-v1 --suite release-v1 \
  --split train --task-id <id> --out /external/data/episode-1
retro benchmark taskset reward --name personal-v1 --suite release-v1 \
  --split train --task-id <id> --candidate /external/data/candidate-state.tar.zst \
  --out /external/data/reward-1 --ghostlab-bin /path/to/ghostlab
```

`reset` materializes a fresh public workspace; it does not execute setup or
arbitrary candidate code on the host. Ghostlab owns isolated setup, the
agent/tool action loop and canonical candidate export. A process runner can be
a model-backed agent or a custom script; its executable dependencies should be
declared as hashed agent assets or baked into a task-derived pinned image.

`episodes.jsonl` carries public inputs, limits, paths and sampling weights.
Each source gets equal sampling mass; priorities redistribute mass only within
that source. With `--include-rewards`, verifiers live in `private/rewards/` and
must never be mounted into the actor sandbox. No accepted outcome, reference
patch, full trajectory or private provenance is exported.

Exports include verified file manifests and pinned image references, **not**
container layers. The same images must be made available separately. There is
no cloud upload, optimizer, model-weight update, automatic Gymnasium `step()`
adapter or conversational user emulator in this feature.

`reward` delegates to the same `score_candidate` implementation used by
benchmark trials. Valid observed failures produce numeric `0`; harness,
scorer, model or artifact failures have `reward: null` and an explicit error.
Scored episodes set `terminated=true`; unavailable rewards set
`truncated=true`. Optional `--trace` and `--resources` pass terminal evidence
to the trusted scorer. The command exits nonzero on unavailable reward.

Each curated evaluation binds an immutable cohort: suite hash, split, task IDs,
source index, base/scorer hashes, pass thresholds and requested seeds. A
different cohort requires a different evaluation ID. Interrupted/missing
attempts reduce coverage against the full requested task-by-seed grid.

Reports show:

- mean scored reward and pass rate, macro-averaged tasks within source and then
  sources;
- scored/requested coverage and separate agent/scorer/harness failures;
- population seed spread by task;
- source-normalized unbiased pass@k where every observed task has enough
  **scored** trials (`k=1,2,4,8`), conditional on valid executions;
- a deterministic 2,000-resample, 95% source-percentile-bootstrap interval when
  at least two sources have scores;
- resource and budget views separate from correctness reward.

These intervals do not measure unseen-task generalization, model-training
uncertainty or future user utility. Partial-coverage scores are conditional
inspection results, not completed benchmark rankings.

## 7. Experiment jobs, visualization and remaining acceptance work

Experiment setup is immutable `experiments/<id>/setup.json`; atomic
`job.json` stores progress and cleanup. The setup freezes title/hypothesis,
dataset suite hashes, selected tasks/splits, candidates and their configuration
and referenced-asset hashes, seeds and timeout overrides. Multiple candidates
share a frozen evaluation cohort per dataset; datasets are never pooled for a
cross-cohort ranking. Job creation does not invoke a model. Run is a separate
explicit operation, with planned/running/completed/failed/interrupted states.
Interrupted jobs recover published attempts and can resume; failed input
verification requires a new plan, not a silently updated setup.

The dashboard reads published disk contracts and lazily loads benchmark
catalog/taskset/task/evaluation shards. **Tasks** shows one current
verified release per collection, deduplicated by task and repository, with
plain-language task goals, environment tooling, success criteria and grader
rules. Unreleased tasks and older collection versions remain separate from
this default view. Task details lead with the episode and grading contract;
exact versions and validation evidence remain expandable. **Evals** contains
only frozen task datasets. **Experiments** contains setup, model/script labels,
results, every trial's grade/component evidence and explicit cleanup controls.

Warnings describe the affected record, comparison impact and next action,
retaining the original paths/reasons as collapsed technical evidence. Dismissal
is browser-local, keyed by archive and exact warning content; the global banner
also uses the warning-set digest so different warnings with equal counts
reappear. Dismissal never changes verification or comparison eligibility. A
benchmark-history warning is not presented as a capture failure. Creation history shows selection/build/curation yield,
rejections, release splits, prompt and goal event IDs, source/base/outcome
anchors, validation cases, audit decisions, agent-by-task heatmaps and per-seed
failures. Different cohorts are never pooled. Private scorer/candidate bytes
are not rescanned in the browser; runtime integrity checks remain authoritative.

`retro dashboard serve` is an opt-in `127.0.0.1` control server, not a cloud
service. It requires matching loopback Host/origin and a per-process token for
controls/private inspection, disallows CORS, accepts only startup-registered
candidate configurations, and defaults to a 100-trial UI cap. The operator can
inspect the actual grader entrypoint/code, pinned judge prompt when used, public
task input, result schema and each saved input/report/attestation. Deterministic
graders explicitly report that no LLM prompt is used. Missing retained artifacts
are shown as missing, never reconstructed as observed input. New job results
load from the live archive so a cached static shard cannot hide completed runs.
The additional control asset is bounded at 32 KiB and the shipped shell/assets
at 225 KiB.

Execution prompts append explicit ignore/experiment markers without rewriting
the immutable public task prompt. Run fingerprints include execution metadata;
attempt ownership records link cleanup and provenance to the experiment.
The default catalog excludes explicit experiment markers, Codex originator
`ghostlab`, Copilot's reserved `67686f73-746c-` prefix and evidenced descendants,
not arbitrary automation or working-directory guesses. Normal catalog-driven
mining, memory, signals and command analysis exclude them. Deliberate inspection
and user catalog overrides retain their existing behavior.

After each trial, exact disposable workspace/runtime paths and known duplicate
scorer staging repositories are removed; saved input JSON, reports, attestations,
events and hashes remain. Finished-job prune removes only owned candidate
exports, never task/suite/base/oracle bundles or raw captures. Cleanup failures
are recorded rather than hidden. Ghostlab owns sandbox teardown; no global
Docker/OpenShell pruning is performed. Archive TTL is deferred: container
teardown does not itself expire host-side exported artifacts.

Model-free integration tests execute real behavioral scorer code against a
small fixture repository through a clearly labelled fake Ghostlab executable.
They verify alternative solutions, incorrect solutions, null infrastructure
rewards, splits, snapshot tampering, export boundaries and dashboard rendering.
They are plumbing evidence, not live model benchmark results.

A production release additionally needs available, SSD-backed sandbox
infrastructure, project environment validation, generated task/scorer/auditor
runs, and real candidate-agent trials. Predictive validity on held-out future
user tasks, difficulty calibration, semantic contamination review and human
review remain separate research/product work rather than claimed completed
capabilities.
