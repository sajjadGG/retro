# Learning from Rollouts: Objective, Hypotheses, and First Algorithm

Status: research design v0.1

Scope: a fresh design derived from the current conversation, independent of the branch's existing learning implementation

First algorithm: Evidence-Grounded Reflection (EGR-1)

## 1. North star

The system should become more useful to a particular user as it accumulates experience with that user.

Let:

- `B` be the cumulative lifecycle token budget spent collecting rollouts, reflecting, validating, training, and maintaining memory;
- `D_u` be the user's task distribution;
- `pi_B` be the deployed agent after learning from all experience available at budget `B`;
- `J(pi, x)` be task performance on task `x`, measured with a fixed inference-time token and tool budget.

The quality curve is:

\[
Q(B) = \mathbb{E}_{x \sim D_u}[J(\pi_B, x)].
\]

The primary objective is not merely final performance. It is strong performance throughout the agent's lifetime:

\[
\operatorname{AULC}(B_{max}) =
\frac{1}{B_{max}}
\int_0^{B_{max}} Q(b)\,db.
\]

This is the area under the learning curve. It rewards learning early and continuing to improve. It is a better match to the product goal than measuring only the last checkpoint.

Strict monotonic improvement on every task is impossible to guarantee because tasks, rollouts, and evaluations are stochastic. The deployable approximation is monotonic checkpointing:

\[
\pi_{B+\Delta} \leftarrow
\begin{cases}
\pi_{candidate} & \text{if the candidate passes the deployment gate} \\
\pi_B & \text{otherwise.}
\end{cases}
\]

This makes the deployed system best-so-far on the gate distribution, while preserving rejected candidate states for analysis.

### 1.1 Token accounting

The curve is meaningful only if every lifecycle token is counted:

- ordinary task execution;
- rollout summarization;
- goal and outcome inference;
- pattern mining and reflection;
- counterfactual generation;
- judges and verifiers;
- replay or branch execution;
- memory retrieval and prompt injection;
- parameter training;
- evaluation and deployment gating.

Evaluation-time inference tokens must be held fixed. Otherwise a larger memory prompt can look like learning when it is only extra test-time compute.

Report ordinary task tokens, learning tokens, evaluation tokens, latency, and dollar cost separately in addition to total lifecycle tokens.

## 2. What “learn from any rollout” can mean

Not every rollout contains enough evidence to justify a behavioral update. Some have no trustworthy feedback, reflect exogenous failures, contain private or poisoned content, or cover a task that will never recur.

The attainable guarantee is:

> Every rollout can be safely classified and can update the learner's evidence, uncertainty, coverage, or behavior when justified.

A retained rollout may be classified as:

- actionable evidence;
- useful only when paired with other experiences;
- redundant but confidence-increasing;
- ambiguous or insufficiently observed;
- exogenous failure;
- stale;
- corrupted or prompt-injected;
- privacy-sensitive;
- currently irrelevant but worth cold archival;
- non-actionable.

“No behavioral update” is a valid learning decision.

Immutable means append-only and provenance-preserving while evidence is retained. It does not override consent withdrawal, secret removal, retention expiry, legal deletion, or quarantine of poisoned content. Deletion creates a tombstone containing only non-sensitive provenance and invalidates every lesson whose evidentiary support no longer meets its threshold.

## 3. Separate the problems currently hidden inside “reflection”

Reflection is not one operation. It contains at least seven:

1. Compressing a rollout without destroying important evidence.
2. Recovering goals, constraints, and outcome signals.
3. Finding decision points and candidate lessons.
4. Estimating causal credit.
5. Deciding which claims deserve validation effort.
6. Deciding what to store and retrieve later.
7. Proving that the resulting update improves held-out performance.

The central rule of this design is:

> Reflection proposes learning claims; reflection does not certify them.

A fluent causal story from one rollout is a hypothesis. It becomes reusable knowledge only after replay, independent recurrence, an executable counterfactual, or a held-out intervention supports it.

## 4. Hypothesis audit

### H1: Rollouts should be summarized before learning

Verdict: useful but unsafe if the summary replaces the trace.

A summary is an index over evidence, not the evidence itself. Exact tool arguments, action ordering, observations, user corrections, and constraints may be the causal breakpoint. EGR-1 therefore creates a hierarchical experience card and retains immutable event links back to the raw rollout.

### H2: Human memory is blurry, so agent memory should be blurry

Verdict: a metaphor, not a design justification.

Compression is justified by token and retrieval budgets. It should preserve information that predicts future decisions and outcomes. The optimal blur is task-dependent and should be measured through downstream utility and fidelity tests.

### H3: Not every experience is equally worth learning from

Verdict: probably correct computationally, but value is conditional rather than intrinsic.

An experience's value depends on the future task distribution, current policy, current memory, other available experiences, validity, recurrence, and learning cost:

\[
V(e \mid M, \pi, D_u) =
\frac{
\mathbb{E}[Q_{D_u}(\operatorname{Learn}(M,e),\pi)-Q_{D_u}(M,\pi)]
}{
\Delta \operatorname{Cost}(e)
}
- \lambda\operatorname{Interference}(e).
\]

Therefore selection initially allocates reflection and validation compute; it does not delete raw evidence.

### H4: Useful or abnormal rollouts should be selected

Verdict: incomplete.

Abnormality is not usefulness. Rare events can be noise; frequent ordinary events estimate stable invariants. A useful learner needs failures, successes, recoveries, high-cost successes, contradictions, novel situations, and a uniform coverage reservoir.

### H5: Patterns across experiences reveal lessons

Verdict: patterns generate hypotheses, not causal knowledge.

Repeated errors may share a hidden confounder. Repeated success may reflect an easy task rather than a good action. Each pattern must become a scoped and falsifiable claim with evidence, counterevidence, and a validation plan.

### H6: Goals can be recovered from the conversation

Verdict: partially.

A conversation may contain changing, conflicting, implicit, abandoned, and process-oriented goals. The learner must recover a time-indexed goal graph, not a single final goal. Later information must not be used to judge an earlier action as though the agent knew it at the time.

### H7: Terminal reward can be assigned to actions or tokens

Verdict: underidentified without additional structure.

The first useful unit is a semantic decision span or tool action, not an individual token. Many token sequences express the same choice, a good action can appear in a failed rollout, and later recovery can hide an early mistake. Token losses can be weighted after action-level credit is estimated, but direct token causality should not be assumed.

### H8: Hindsight can reveal better paths

Verdict: valuable if validity and information leakage are controlled.

An imagined alternative is a proposal, not evidence. It may use future information, skip tool preconditions, assume unavailable permissions, or rely on a perfect continuation. The strongest test is to reset to the original state, change one decision, and execute several continuation seeds.

### H9: More experience should monotonically improve the agent

Verdict: appropriate as an optimization target, not a raw-update guarantee.

Memory interference, nonstationarity, exploration, evaluator noise, and catastrophic forgetting cause regressions. Statistical deployment gates and rollback are required.

## 5. Core representations

### 5.1 Rollout

A rollout is the immutable chronological record of:

- user, system, and agent messages;
- reasoning or planning spans when available;
- actions and tool calls;
- tool observations and environment state changes;
- feedback and rewards;
- costs and timing;
- model, tool, environment, and policy versions.

The archive is logically immutable while retained, but subject to the privacy and retention deletion rules in Section 2.

### 5.2 Experience card

An experience card is a lossy but evidence-linked view over one rollout. It contains:

```text
ExperienceCard
  rollout_id
  environment_fingerprint
  policy_fingerprint
  task_family
  summary_short
  summary_timeline
  goal_graph
  outcome_vector
  decision_points[]
  failure_recovery_segments[]
  anomalies[]
  candidate_skills[]
  privacy_and_integrity_labels[]
  uncertainty
  raw_event_refs[]
```

The card is regenerated when the extraction policy improves. Raw events remain unchanged.

### 5.3 Time-indexed goal graph

Goal nodes have:

```text
GoalNode
  goal_id
  kind: task | subgoal | success_test | hard_constraint | preference | safety
  statement
  introduced_at
  superseded_at
  parent_goal_ids[]
  evidence_event_ids[]
  explicit_or_inferred
  confidence
  completion_evidence[]
```

This representation distinguishes task success from process quality. An agent can reach the output while violating a permission constraint, wasting large amounts of compute, or ignoring the user's preferred workflow.

### 5.4 Outcome vector

Avoid immediately collapsing the outcome to one scalar:

```text
OutcomeVector
  task_correctness
  goal_completion_by_node
  hard_constraint_satisfaction
  user_correction_count
  user_acceptance
  safety_and_privacy
  efficiency_tokens
  efficiency_wall_time
  tool_error_rate
  recovery_quality
  calibration
  evidence_quality
  exogenous_failure_probability
```

A user- or domain-specific reward model may later map the vector to a scalar. Keeping the vector prevents reward shaping from hiding regressions on important dimensions.

### 5.5 Decision point

```text
DecisionPoint
  decision_id
  state_event_refs[]
  information_available_at_time
  active_goal_ids[]
  chosen_action_event_refs[]
  action_type
  observed_transition_refs[]
  later_outcome_refs[]
  alternative_actions[]
  credit_by_outcome_dimension
  credit_uncertainty
```

The `information_available_at_time` field is mandatory for preventing hindsight leakage.

### 5.6 Lesson

A lesson is a causal claim, not generic advice:

```text
Lesson
  lesson_id
  status: candidate | validated | deployed | rejected | stale | quarantined
  when_conditions
  recommended_intervention
  expected_effects
  anti_pattern
  scope
  source_evidence_refs[]
  counterevidence_refs[]
  validation_records[]
  confidence_by_context
  utility_posterior
  retrieval_stats
  conflicts_with[]
  environment_fingerprints[]
  created_by_policy
  version
```

Good lesson:

> When a repository has uncommitted user changes, inspect status and isolate edits before applying a patch; this reduces accidental overwrite risk.

Bad lesson:

> Be more careful next time.

## 6. Four knowledge states

Every learned claim moves through explicit states:

1. **Archived evidence**: immutable rollout events.
2. **Candidate lesson**: a reflector-generated, scoped hypothesis.
3. **Validated lesson**: supported by replay, recurrence, a verifier, or a held-out intervention.
4. **Deployed lesson**: included in the policy or active memory after passing quality and safety gates.

Promotion is reversible and provenance-preserving. Candidate lessons are never silently converted into trusted instructions.

## 7. First algorithm: Evidence-Grounded Reflection (EGR-1)

EGR-1 is deliberately nonparametric: it changes an external procedural memory while keeping the base model frozen. This makes updates reversible and isolates whether the learning signal is real before weight updates amplify it.

### 7.1 Inputs

- a batch or stream of normalized rollouts;
- immutable raw event access;
- current candidate, validated, and deployed lesson stores;
- a task-family model;
- outcome adapters and available verifiers;
- reflection, validation, retrieval, and prompt token budgets;
- fixed anchor and rolling gate suites.

### 7.2 Phase A: compile evidence-linked experience cards

For each rollout:

1. Recover the time-indexed goal graph.
2. Infer the outcome vector and separate observed from inferred outcomes.
3. Segment the trajectory at semantic decisions, tool calls, validations, retries, and user corrections.
4. Produce a short summary, timeline summary, and decision-point summaries.
5. Detect failure, recovery, contradiction, high-cost success, novelty, and possible exogenous causes.
6. Attach raw event references to every factual and causal statement.
7. Assign integrity, privacy, and uncertainty labels.

The extractor must be tested for factual fidelity and breakpoint recall. A card that cannot point back to evidence cannot be used for promotion.

### 7.3 Phase B: allocate reflection attention

EGR-1 begins with a fixed stratified allocator plus a uniform reservoir. It does not assume that experience value is already known. Initial strata include successes, failures, recoveries, efficient and inefficient successes, contradictions, novel task/skill clusters, uncertain outcomes, and random samples. Each selection records its sampling probability.

After enough randomized data exists, a learned selector may estimate expected marginal learning utility:

\[
\hat V(e) =
p_{recur}(e)
p_{valid}(e)
\widehat{\Delta Q}(e)
+ \beta U(e)
+ \gamma C(e)
- \lambda T(e)
- \mu I(e),
\]

where:

- `p_recur` estimates recurrence under the user's future task distribution;
- `p_valid` estimates whether feedback and attribution are trustworthy;
- `Delta Q` estimates possible future performance gain;
- `U` is epistemic uncertainty, used for exploration;
- `C` is coverage or diversity value;
- `T` is lifecycle token cost;
- `I` is interference or negative-transfer risk.

All predictive terms must have defined estimators, priors, calibration sets, and uncertainty. Quality terms are normalized to a common expected gate-utility scale; token and interference terms are converted to the same scale using pre-registered tradeoff coefficients. Until an estimator reaches minimum support and calibration thresholds, its fixed prior is used. The exploration term is implemented as expected value of information rather than an arbitrary additive novelty bonus.

The score is a learned estimate, not truth. Selection continues to use a stratified budget:

- high estimated value;
- high uncertainty;
- contradiction pairs;
- success/failure/recovery contrasts;
- coverage across task and skill clusters;
- a uniform random reservoir.

No raw rollout is deleted merely because it was not selected. Privacy, security, consent, and retention policies still apply.

### 7.4 Phase C: form comparison neighborhoods

Single-rollout reflection is weak evidence. EGR-1 groups cards by:

- shared or analogous goal nodes;
- tool and environment fingerprints;
- skill or action type;
- similar pre-decision information state;
- outcome differences;
- policy version and time.

The strongest neighborhoods contain matched contrasts:

- success versus failure on the same goal;
- efficient versus inefficient success;
- failure versus recovered failure;
- two paths differing at one decision;
- a lesson-assisted rollout versus an unassisted rollout.

### 7.5 Phase D: generate scoped candidate lessons

The reflector sees experience cards plus selectively retrieved raw evidence. It must output:

- the shared context;
- the hypothesized pivotal decision;
- recommended intervention;
- expected change in the outcome vector;
- supporting and contradicting rollout IDs;
- alternative explanations;
- scope and expiry conditions;
- confidence;
- cheapest strong validation plan.

The reflector is required to abstain when the evidence does not distinguish explanations.

### 7.6 Phase E: assign multi-resolution credit

Credit is assigned in this order:

1. task and constraint outcome;
2. subgoal completion;
3. environment state transitions;
4. semantic decision and tool-action spans;
5. planning or verification acts;
6. generated action tokens, only for parameter training.

For a decision at time `t`, a causal counterfactual target is:

\[
\Delta_{t,k}(a') =
\mathbb{E}[R_k \mid do(a'), I_t, \pi_{continue}]
-
\mathbb{E}[R_k \mid do(a_t), I_t, \pi_{continue}],
\]

where `I_t` contains only information available at time `t`, `k` indexes an outcome dimension, and the continuation policy is fixed or explicitly modeled.

EGR-1 combines evidence sources with uncertainty rather than pretending they are equivalent:

- actual reset-and-branch execution;
- deterministic test or formal verifier;
- matched natural repetitions;
- learned world-model estimate;
- post-hoc LLM attribution.

Post-hoc verbal attribution alone is weak evidence. It may prioritize validation but cannot directly promote a durable lesson.

### 7.7 Phase F: create and validate counterfactuals

There are four distinct counterfactual operations:

1. **Local action replacement**: change one decision under the original goal.
2. **Trajectory repair**: synthesize a better suffix for the original goal.
3. **Branch search**: execute multiple alternatives from a resettable checkpoint.
4. **Goal relabeling**: identify a different goal the failed trajectory actually achieved.

Goal relabeling learns an achieved skill; it does not teach compliance with the original user request. It must be labeled as a distribution change and cannot turn a wrong-goal trajectory into evidence of instruction following.

Counterfactual validity tiers:

| Tier | Evidence | Permitted use |
|---|---|---|
| 0 | One model imagines a path | Candidate generation only |
| 1 | Independent judges agree and all claims are observation-supported | Low-confidence memory experiment |
| 2 | Deterministic validator, simulator, or independent matched repetition | Validated lesson and weighted training datum |
| 3 | Alternative executed from the same or equivalent environment state | Strong causal credit and promotion evidence |

Validation must freeze `I_t`, check permissions and tool availability, change as few decisions as possible, and run multiple continuation seeds when stochastic.

Executable validation additionally requires a replay contract:

```text
ReplayContract
  rollout_id
  checkpoint_id
  serializable_state_refs[]
  known_hidden_state
  excluded_or_unrecoverable_state[]
  filesystem_snapshot
  external_service_fixtures[]
  model_tool_environment_versions
  credentials_and_permissions_scope
  random_seeds[]
  concurrent_actor_policy
  equivalence_predicates[]
  equivalence_tolerances
  supported_outcome_claims[]
  replay_limitations[]
```

“Same or equivalent state” means that every registered equivalence predicate passes within tolerance and no excluded hidden state is plausibly outcome-determining. When this cannot be established, the result is labeled associational. The preferred alternative is prospective randomized lesson retrieval on future matched tasks, not simulated causal credit.

### 7.8 Phase G: deploy candidate memory behind a gate

Construct a trial memory state from validated lessons. Retrieve lessons for evaluation under a fixed prompt budget using:

\[
\operatorname{score}(m,x) =
\operatorname{relevance}(m,x)
\cdot \mathbb{E}[\operatorname{uplift}(m,x)]
- \lambda\operatorname{tokens}(m)
- \mu\operatorname{interference}(m,x).
\]

Selection under the prompt budget is a contextual knapsack problem, not top-k semantic similarity alone.

The trial state is evaluated with paired tasks and seeds. Deploy only when:

- the lower confidence bound on primary improvement exceeds a margin;
- no important slice regresses beyond tolerance;
- safety, privacy, and integrity checks pass;
- the gain persists under matched inference tokens;
- old capabilities pass a backward-retention suite.

Otherwise retain the old deployed state and mark the candidate as rejected, underpowered, or needing more evidence.

Validation and deployment operate at distinct units:

- **claim validity** asks whether a lesson's factual and causal statement is supported;
- **lesson utility** asks whether retrieving one lesson improves matched future decisions;
- **bundle utility** asks whether a consolidated memory version improves the deployed agent;
- **retrieval-policy utility** asks whether lesson selection improves outcomes under a fixed context budget.

A passing bundle gate does not assign utility to every included lesson. EGR-1 estimates lesson-level uplift with randomized withhold/substitute tests, crossover trials, and small factorial ablations. Larger bundles use conservative grouped attribution or Shapley approximations when affordable. Harmful lessons hidden by a helpful bundle remain unpromoted until their own utility is supported. Interacting lessons may be deployed and versioned as an explicit bundle with joint scope rather than receiving fictional individual credit.

Tier-1 judge agreement never establishes causality. It permits only a low-risk randomized memory experiment and the lesson remains a candidate until measured utility or stronger evidence is available.

### 7.9 Phase H: learn the retrieval policy

Retrieval is itself an action and needs credit assignment. Log:

- candidate lessons considered;
- lessons injected;
- prompt position and token count;
- whether the agent cited or followed the lesson;
- eventual outcome vector;
- matched no-memory or alternate-memory results when available.

Use paired A/B evaluation initially. Later use a conservative contextual bandit to estimate per-context lesson uplift, with exploration, propensity logging, and an interference penalty.

Cold-start retrieval uses relevance plus fixed scope and safety rules, with a randomized holdout rate. Every retrieval decision logs candidate-set probability, selection propensity, prompt position, and substitutions. Contextual estimates replace fixed rules only after reaching minimum support, off-policy reliability, and calibration thresholds.

### 7.10 Pseudocode

```python
def egr_update(rollouts, state, budgets, gate_suites):
    cards = [compile_experience_card(r) for r in rollouts]
    archive(cards)

    reflection_set = allocate_attention(
        cards,
        current_memory=state.memory,
        task_distribution=state.task_model,
        budget=budgets.reflection,
        include_coverage_reservoir=True,
    )

    neighborhoods = build_comparison_neighborhoods(
        reflection_set,
        historical_cards=state.card_index,
    )

    candidates = []
    for neighborhood in neighborhoods:
        evidence = retrieve_raw_evidence(neighborhood)
        candidates.extend(reflect_into_scoped_hypotheses(neighborhood, evidence))

    validated = []
    for lesson in prioritize_validation(candidates, budgets.validation):
        result = run_strongest_affordable_validation(lesson)
        lesson.add_validation(result)
        if lesson.meets_validation_threshold():
            validated.append(lesson)

    trial_memory = consolidate(
        deployed=state.memory,
        additions=validated,
        conflicts=detect_conflicts(validated, state.memory),
        prompt_budget=budgets.retrieval,
    )

    gate_result = paired_compute_matched_evaluation(
        old_memory=state.memory,
        trial_memory=trial_memory,
        suites=gate_suites,
    )

    if gate_result.passes_quality_safety_and_retention():
        state.deploy(trial_memory, gate_result)
    else:
        state.retain_old_deployment_and_record(gate_result)

    return state
```

## 8. From action credit to token learning

Directly rewarding every token in a successful trajectory repeats the original credit problem at finer resolution. The first parameter-learning dataset should instead contain decision examples:

```text
TrainingDecision
  prefix_state_without_future_information
  active_goal_and_constraints
  chosen_action_span
  validated_alternative_action_spans[]
  outcome_advantage_vector
  validation_tier
  uncertainty
  source_event_refs[]
```

Training rules:

- never optimize user, system, or observation tokens;
- place loss only on agent decision/action spans;
- mask irrelevant actions in repaired or relabeled trajectories;
- weight examples by validated advantage and confidence;
- retain a replay set of mastered behaviors;
- regularize against the deployed reference policy;
- test adapter updates behind the same deployment gate;
- start with reversible adapters before full-model consolidation.

The eventual slow-learning objective can mix:

\[
\mathcal{L} =
\mathcal{L}_{SFT}^{validated}
+ \alpha \mathcal{L}_{preference}^{counterfactual}
+ \beta \mathcal{L}_{RL}^{outcome}
+ \gamma \mathcal{L}_{retention}
+ \eta D_{KL}(\pi_\theta || \pi_{deployed}).
\]

EGR-1 should prove that its lessons and credit estimates have causal value before this stage begins.

## 9. Three timescales of learning

### Fast: episodic retrieval

- Store exact or compressed relevant episodes.
- Useful for recurrence and local personalization.
- Immediate and reversible.
- High risk of context clutter and surface-level retrieval errors.

### Medium: semantic and procedural memory

- Distill repeated, validated causal lessons.
- Scope rules by context, tool version, user preference, and environment.
- Track conflict, confidence, and expected uplift.
- EGR-1 operates primarily at this timescale.

### Slow: parametric consolidation

- Train on validated action spans, counterfactual preferences, and outcome rewards.
- Replay retained skills and use KL or adapter constraints.
- Deploy only after forward-transfer and backward-retention gates.

No lesson should jump directly from a single rollout to slow parametric memory.

## 10. Evaluation protocol

### 10.1 Measurement contract

Before any learner is compared, each domain must register a measurement contract:

```text
MeasurementContract
  scope: user | organization | repository | task_family
  task_distribution_definition
  task_family_sampling_weights
  time_window_and_nonstationarity_policy
  observed_outcome_fields[]
  inferred_outcome_fields[]
  evaluator_for_each_field
  evaluator_independence_rules
  missing_data_policy
  uncertainty_model
  hard_constraints_and_noninferiority_margins[]
  soft_utility_scalarization
  fixed_inference_budget
  lifecycle_token_accounting
  gate_suites_and_split_boundaries
  minimum_sample_sizes
  update_and_evaluation_cadence
  sequential_testing_rule
  privacy_and_retention_policy
```

Observed outcomes such as test results, tool exit codes, explicit user corrections, and measured token cost are kept separate from inferred outcomes such as satisfaction or exogenous-failure probability. Every inferred field names its evaluator, calibration data, uncertainty, and missing-data behavior. The lesson proposer cannot serve as the sole evaluator that certifies its own claim.

Hard safety, permission, privacy, and user constraints are evaluated lexicographically and are not traded away for average reward. Soft outcome dimensions use weights chosen by the user or pre-registered domain policy. If weights are unknown, report a Pareto vector and defer scalar deployment rather than allowing the reflector to invent a scalarization.

Estimate two distributions and two curves:

- `Q_current(B)` on a rolling, time-weighted estimate of the user's evolving current task distribution;
- `Q_anchor(B)` on a stable retention distribution representing capabilities that should not regress.

The definition of `D_u`, sampling frame, time window, task-family weights, and shift-detection policy must be explicit. Learning claims are scoped to the distribution on which they were measured.

### 10.2 Task streams

Evaluate on all of:

- repeated instances of the same task;
- held-out instances from known task families;
- held-out families with shared skills;
- genuinely unrelated new families;
- previously mastered tasks;
- irrelevant and adversarial rollouts;
- stale tool and environment versions;
- task-order permutations.

Use both controlled compositional streams, where prior skills are intentionally reusable, and natural streams, where reusability is unknown.

### 10.3 Primary metrics

- compute-matched AULC versus cumulative lifecycle tokens;
- final held-out quality;
- cumulative lifetime regret;
- worst checkpoint regression;
- forward transfer to related unseen tasks;
- backward retention and forgetting;
- negative-transfer rate;
- success, progress, and action/step efficiency;
- constraint and safety violation rates;
- reflection and validation cost;
- retrieval token cost and latency;
- lesson-value calibration;
- counterfactual feasibility and advantage-prediction calibration.

### 10.4 Statistical gate discipline

- Maintain a fixed anchor suite for retention.
- Maintain a rolling, time-split suite for current user tasks.
- Maintain hidden refresh sets to prevent deployment-gate overfitting.
- Use paired seeds and confidence intervals.
- Freeze model and tool versions within comparisons.
- Hold inference context and sampling budget fixed.
- Keep the evaluator separate from the lesson proposer when possible.
- Pre-register update cadence, minimum sample size, primary endpoint, non-inferiority margins, and slice hierarchy.
- Use sequential testing with alpha spending or confidence sequences rather than repeatedly accepting the best of unlimited candidates against one gate.
- Correct or hierarchically control multiple slice tests; mark underpowered slices instead of treating noise as proof of no regression.
- Enforce time-based separation between experience mining, selector training, lesson validation, and gate evaluation.
- Refresh hidden gates under separate ownership after a bounded number of exposures.
- Charge gate evaluation to the candidate's lifecycle budget.
- Evaluate all algorithms at common cumulative-token checkpoints; different internal checkpoint schedules must not change the AULC sampling grid.
- When one user lacks enough matched tasks, report uncertainty and abstain from strong deployment claims rather than substituting model-judge confidence.

## 11. Minimal experimental program

### Experiment 1: representation

Compare under equal retrieved-token budgets:

- no memory;
- raw trajectory snippets;
- free-form summaries;
- structured experience cards;
- structured cards with on-demand raw evidence retrieval.

Measure task reward, fidelity, causal-breakpoint recall, constraint recall, and total lifecycle cost.

### Experiment 2: experience allocation

Fix reflection compute, storage count, and retrieval budget. Compare:

- random;
- all experiences truncated to budget;
- failures only;
- successes only;
- anomalies only;
- reward magnitude;
- relevance;
- estimated marginal utility plus uncertainty, diversity, and coverage.

Include rare noise, common informative failures, redundant successes, rare critical cases, recoveries, and exogenous failures.

### Experiment 3: lesson generation

Compare:

- single-rollout free-form reflection;
- single-rollout structured reflection;
- matched success/failure contrast;
- multi-rollout pattern reflection;
- pattern reflection with raw evidence verification.

Score lesson specificity, evidence fidelity, falsifiability, recurrence, and measured uplift.

### Experiment 4: credit assignment

Use deterministic resettable environments where branch values can be measured. Compare:

- uniform terminal credit;
- LLM verbal attribution;
- subgoal credit;
- tool/decision-span credit;
- return redistribution;
- hindsight policy-ratio estimates;
- replay-validated branch advantage.

Measure sign accuracy, correlation with true branch advantage, calibration, and downstream learning gain.

### Experiment 5: counterfactual validity

At selected decision points:

1. freeze the original information state;
2. generate alternatives;
3. check preconditions and permissions;
4. execute feasible alternatives from the same state;
5. compare predicted and realized deltas.

Report feasibility, regret reduction, calibration, and cost. Never count an unexecuted prose path as a successful counterfactual.

### Experiment 6: continual deployment

Compare:

- no learning;
- indiscriminate memory accumulation;
- always-deploy reflection memory;
- validated memory;
- validated memory with deployment gate and rollback;
- gated memory plus slow adapter consolidation.

Plot quality against total lifecycle tokens, not only episode count.

## 12. Rejection criteria

The system is not yet demonstrating learning from experience if:

- gains disappear under matched inference tokens;
- gains occur only on repeated task instances;
- lessons cannot point to source evidence;
- removing a supposedly useful lesson does not reduce held-out performance;
- imagined counterfactuals are treated as causal evidence without calibration;
- one model writes, judges, and approves its own lesson without external checks;
- candidate updates repeatedly mine the deployment test;
- old capabilities regress without rollback;
- the system cannot abstain from corrupted, private, or ambiguous experience;
- memory growth produces increasing prompt cost without increasing net utility.

## 13. Implementation sequence

### Milestone 0: measurement contract

- Define observed and inferred task/outcome adapters, uncertainty, missing-data behavior, scalarization, and token-cost accounting.
- Define `D_u`, current and anchor distributions, time splits, shift handling, and task-family sampling weights.
- Define anchor, rolling, and hidden gate suites, sequential testing, minimum sample sizes, non-inferiority margins, and evaluation cadence.
- Define replay/checkpoint contracts for every environment where causal counterfactuals will be claimed.
- Record policy, model, tool, and environment fingerprints.
- Produce the baseline quality-versus-token curve with no learning.

### Milestone 1: EGR-1 offline prototype

- Compile experience cards from existing rollouts.
- Mine candidate lessons from matched neighborhoods.
- Provide evidence links and abstention.
- Run offline fidelity and usefulness evaluations.
- No production retrieval and no weight updates.

### Milestone 2: gated procedural memory

- Add validated/deployed lesson states.
- Add budgeted retrieval and matched A/B evaluation.
- Credit the retrieval decision.
- Deploy only through quality, safety, and retention gates.

### Milestone 3: executable counterfactual laboratory

- Add environment checkpoints where possible.
- Generate local one-decision branches.
- Calibrate model and judge estimates against executed advantages.
- Learn which validation mechanism is trustworthy for each domain.

### Milestone 4: slow parametric consolidation

- Construct action-span datasets from tier-2 and tier-3 evidence.
- Train reversible adapters with replay and KL constraints.
- Gate against memory-only EGR-1, not only the frozen baseline.

### Milestone 5: learn the learner

- Train or search the selection, reflection, validation-allocation, and retrieval policies using AULC as the outer objective.
- Preserve held-out user-task and retention gates.
- Optimize net utility per lifecycle token.

## 14. Relevant research and implications

Reflexion established the lightweight loop of translating feedback into verbal memory without weight updates, but explicitly identifies credit assignment and self-evaluation reliability as limitations [1]. ExpeL extended experience-derived natural-language insights across tasks and reported forward transfer [2]. CLIN's persistent causal abstractions are closer to the representation proposed here than generic advice [3].

Recent work strengthens several pieces while also showing why they need separation. Experiential Reflective Learning reports that selective retrieval and heuristic abstraction can beat raw-trajectory prompting [4]. Evo-Memory formalizes memory learning as separate update, retrieval, and context-construction operations, and shows that unfiltered failures can degrade naive memory [5]. META-TTL uses weighted area under the learning curve and local/global validation gates for adaptation policies [6]. AgentCL argues that naive task streams often fail to reveal whether memory is truly reusable and can expose memory-induced degradation [7].

For credit assignment, RUDDER redistributes delayed return while preserving return-equivalent policies [8]. Agent Lightning separates runtime trajectory collection from training and models agent traces as state-action-reward transitions [9]. HCAPO derives a step-level hindsight ratio by conditioning on the final outcome, but acknowledges reliance on model reasoning and out-of-distribution hindsight information [10]. TRACE uses tool-call boundaries and temporal differences in answer-evidence values, reinforcing the choice of semantic turns over individual tokens [11].

For counterfactual learning, AgentHER relabels a failed trajectory with a goal it actually satisfies and requires observation-supported claims plus judge validation [12]. ECHO goes further by synthesizing improved workflows but explicitly depends on the model's incomplete world knowledge [13]. These are useful proposal generators; environment execution remains stronger evidence.

Procedural Memory Distillation is especially close to the long-term direction: it organizes raw trajectories, reflected strategies, and recurring higher-level patterns, then distills that memory into weights while the policy and memory co-evolve [14]. EGR-1 adds the missing emphasis on evidence states, causal validation, compute-matched deployment gates, and personalized lifetime performance.

## 15. Open decisions for the next design pass

1. What exactly is the user's target task distribution: all coding work, one repository, one organization, or a hierarchy of scopes?
2. Which outcome signals are trustworthy today: tests, user corrections, explicit ratings, task completion, time, cost, or a learned evaluator?
3. Which environments can be reset or replayed for real counterfactual branches?
4. What must never be learned or retained: secrets, personal data, transient instructions, third-party content, or inferred preferences?
5. Should lessons transfer across users, repositories, models, and tool versions, or remain scoped by default?
6. What is the maximum acceptable learning overhead per task and retrieval overhead per future task?
7. Which first benchmark provides repeated tasks, related held-out tasks, and executable outcome checks?

The next concrete artifact should be the EGR-1 measurement, replay, and statistical-gate contracts plus JSON schemas for `ExperienceCard`, `GoalNode`, `DecisionPoint`, `Lesson`, `ReplayContract`, and `GateResult`. The first experiment should then compare raw traces, summaries, and evidence-linked cards under equal token budgets.

--------

## References

[1] Shinn N., Cassano F., Berman E., Gopinath A., Narasimhan K., Yao S. “Reflexion: Language Agents with Verbal Reinforcement Learning.” *arXiv* (2023).
https://paperclip.gxl.ai/citations/papers/arx_2303.11366#L9-L15

[2] Zhao A., Huang D., Xu Q., Lin M., Liu Y.-J., Huang G. “ExpeL: LLM Agents Are Experiential Learners.” *AAAI* (2024).
https://paperclip.gxl.ai/citations/papers/arx_2308.10144#L8-L18

[3] Majumder B. P., Dalvi Mishra B., Jansen P., Tafjord O., Tandon N., Zhang L., Callison-Burch C., Clark P. “CLIN: A Continually Learning Language Agent for Rapid Task Adaptation and Generalization.” *arXiv* (2023).
https://paperclip.gxl.ai/citations/papers/arx_2310.10134#L9-L17

[4] “Experiential Reflective Learning for Self-Improving LLM Agents.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2603.24639#L1

[5] “Evo-Memory: Benchmarking LLM Agent Test-time Learning with Self-Evolving Memory.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2511.20857#L27-L44,L73-L74,L101-L102

[6] “Learning to Learn-at-Test-Time: Language Agents with Learnable Adaptation Policies.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2604.00830#L23-L30,L45-L52

[7] “AgentCL: Toward Rigorous Evaluation of Continual Learning in Language Agents.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2606.02461#L1

[8] Arjona-Medina J. A., Gillhofer M., Widrich M., Unterthiner T., Brandstetter J., Hochreiter S. “RUDDER: Return Decomposition for Delayed Rewards.” *arXiv* (2018).
https://paperclip.gxl.ai/citations/papers/arx_1806.07857#L6-L10

[9] Luo X., Zhang Y., He Z., Wang Z., Zhao S., Li D., Qiu L. K., Yang Y. “Agent Lightning: Train ANY AI Agents with Reinforcement Learning.” *arXiv* (2025).
https://paperclip.gxl.ai/citations/papers/arx_2508.03680#L6-L18

[10] “Hindsight Credit Assignment for Long-Horizon LLM Agents.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2603.08754#L51-L67,L120-L122

[11] “TRACE: Turn-level Reward Assignment via Credit Estimation for Long-Horizon Agents.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2607.13988#L1

[12] “AgentHER: Hindsight Experience Replay for LLM Agent Trajectory Relabeling.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2603.21357#L20-L38

[13] “Sample-Efficient Online Learning in LM Agents via Hindsight Trajectory Rewriting.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2510.10304#L23-L30,L65

[14] “Procedural Memory Distillation: Online Reflection for Self-Improving Language Models.” *arXiv* (2026).
https://paperclip.gxl.ai/citations/papers/arx_2607.01480#L1
