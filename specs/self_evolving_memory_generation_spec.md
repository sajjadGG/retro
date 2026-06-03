# Self-Evolving Memory Generation Spec

## Purpose

Add a memory-generation layer that turns captured Codex and Claude Code rollouts into reusable procedural memories, skills, cases, and utility-scored retrieval records.

The goal is not to clone one research repo wholesale. The useful pattern across the papers is:

```text
rollout trajectory -> distilled memory/skill -> retrieve for similar future task -> update by observed utility
```

Retro already captures rollouts, normalizes events, mines memory candidates, and stores evidence-linked artifacts. This spec extends that into a self-evolving memory system with explicit build, retrieve, evaluate, update, prune, and export stages.

## References

Primary papers and repos:

- MemP paper: https://arxiv.org/pdf/2508.06433
- MemP repo: https://github.com/zjunlp/MemP/tree/main
- MemP prompt templates to adapt:
  - `ProcedureMem/prompt_generator.py`
  - `ProcedureMem/memory_adjust.py`
  - `ProcedureMem/Alfworld/prompts.py`
- SkillRL paper: https://arxiv.org/abs/2602.08234
- SkillRL repo: https://github.com/aiming-lab/SkillRL
- MemRL paper: https://arxiv.org/abs/2601.03192
- MemRL repo: https://github.com/MemTensor/MemRL
- MemGen paper: https://arxiv.org/abs/2509.24704
- MemGen repo: https://github.com/KANABOON1/MemGen
- SKILL0 paper: https://arxiv.org/abs/2604.02268
- SKILL0 repo: https://github.com/ZJU-REAL/SkillZero
- SkillNet paper: https://arxiv.org/abs/2603.04448
- SkillNet repo: https://github.com/zjunlp/SkillNet

Related, useful infrastructure:

- Agent0 repo: https://github.com/aiming-lab/Agent0
- A-Evolve repo: https://github.com/A-EVO-Lab/a-evolve

Finding: there is no single repo that cleanly gathers all listed memory generation, self-evolving memory, skill generation, skill evaluation, and skill internalization methods behind one lightweight interface. SkillNet is closest for large-scale skill assets and evaluation. MemRL is closest for value-aware runtime memory updating. MemP is closest for prompt-level procedural memory generation. Retro should integrate the usable abstractions locally instead of depending on these heavyweight research stacks at runtime.

## Research Ideas To Productize

### MemP

Use MemP as the base memory-generation design:

- Procedural memory has a build/retrieve/update lifecycle.
- Build can use either direct trajectory-to-workflow generation or a two-stage event extraction flow.
- Retrieval can use the current task query, extracted facts, or averaged fact embeddings.
- Update supports ordinary addition, validation filtering, reflection-based correction, and dynamic deletion.
- Memory should store both abstract workflows and concrete evidence.

MemP also provides the prompt templates we should adapt first.

### MemRL

Use MemRL to move from passive similarity retrieval to value-aware retrieval:

- Store memory as an intent/experience/utility record.
- Retrieve in two phases: semantic recall, then Q-value/utility reranking.
- Update utility from later rollout feedback without updating model weights.
- Keep a stable frozen agent and let the memory layer be plastic.

### SkillRL

Use SkillRL to turn raw rollout logs into a hierarchical `SkillBank`:

- Extract reusable skills from experience instead of storing noisy raw trajectories.
- Separate general heuristics from task-specific skills.
- Let the skill library co-evolve with the agent policy or, in Retro's local-first version, with observed rollout outcomes.
- Prefer compact skills over long pasted histories to reduce token footprint.

### SkillNet

Use SkillNet for skill quality, ontology, and connection:

- Skills are structured assets with metadata, dependencies, examples, and evaluations.
- Evaluate skills across safety, completeness, executability, maintainability, and cost-awareness.
- Connect related skills so future retrieval can load a small cluster rather than one isolated note.

### MemGen

MemGen's latent memory trigger/weaver is not directly implementable in Retro without model-level access. Use it as a conceptual guide:

- Add a memory-trigger decision before retrieval/injection.
- Add a memory-weaver stage that compresses retrieved memories into a short, task-specific context block.
- Keep this as text memory for now, not latent tokens.

### SKILL0

SKILL0 is mostly a training-time method, not a local mining method. Use it as a long-term direction:

- Track skill helpfulness.
- Reduce runtime context for skills that have become unnecessary or are consistently unhelpful.
- Export successful skills as training data later, but do not require RL or model fine-tuning in Retro v1.

## Current Retro Fit

Existing pieces:

- `src/retro/mining/base.py` defines `MemoryCandidate`, `MiningContext`, mining methods, and filters.
- `src/retro/mining/methods/memp_procedural.py` already implements a heuristic whole-session procedure extractor inspired by MemP.
- `src/retro/mining/methods/skill_pro.py` already implements heuristic skill extraction with activation, steps, termination, and verification.
- `specs/rollout_mining_methods.md` defines scoped memory output, storage, dashboard expectations, and method catalog.
- `specs/experimental_trajectory_signals_spec.md` defines the trajectory signals needed to score workflows and anti-patterns.

This spec should be implemented as the next layer over those pieces, not as a replacement.

## Target Architecture

```text
rollout-memory/normalized/<host>/<id>.events.jsonl
        |
        v
trajectory builder + signal readings
        |
        v
memory builders
  - procedural workflow builder
  - event graph builder
  - skill builder
  - case builder
        |
        v
memory store
  rollout-memory/memories/
    items.jsonl
    indexes/
    skills/
    prompts/
        |
        v
retrieval and evolution
  - semantic recall
  - utility rerank
  - trigger/abstain
  - update / reflect / deprecate
        |
        v
future prompt block or generated Codex skill
```

## Memory Types

Add or formalize these memory kinds:

| Kind | Use | Source inspiration |
| --- | --- | --- |
| `procedure` | Natural-language workflow for a recurring rollout shape. | MemP script/proceduralization |
| `skill` | Activation, steps, termination, verification, dependencies. | SkillRL, SkillNet, Skill-Pro |
| `case` | Intent, situation, action summary, outcome, lessons. | MemRL episodic memory |
| `failure_trigger` | A pattern that caused wasted effort or user correction. | MemP reflection, trajectory anti-patterns |
| `repo_convention` | Project-local command, path, style, or deployment rule. | Retro-specific |
| `tool_lesson` | Tool-specific lesson, e.g. which command worked. | Retro-specific |
| `risk_rule` | Guardrail for unsafe/stale/misleading reuse. | SkillNet safety + risk-aware retrieval |

Extend `MemoryCandidate.structured` to support:

```json
{
  "schema_version": 1,
  "memory_type": "procedure|skill|case|failure_trigger",
  "intent": "what future task this helps with",
  "workflow": "short natural-language workflow",
  "events": [
    {
      "step": 1,
      "pre_state": "...",
      "action": "...",
      "result": "...",
      "new_state": "...",
      "evidence_refs": ["..."]
    }
  ],
  "skill": {
    "name": "...",
    "activation": "...",
    "steps": ["..."],
    "termination": "...",
    "verification": "...",
    "dependencies": [],
    "do_not_use_when": "..."
  },
  "utility": {
    "q": 0.5,
    "hits": 0,
    "successes": 0,
    "failures": 0,
    "last_reward": null,
    "last_used_at": null
  },
  "quality": {
    "safety": 0.0,
    "completeness": 0.0,
    "executability": 0.0,
    "maintainability": 0.0,
    "cost_awareness": 0.0
  }
}
```

## Storage

Add a canonical self-evolving memory store:

```text
rollout-memory/memories/
  items.jsonl                         # all accepted/candidate memory records
  events.jsonl                        # memory lifecycle log
  index.sqlite                        # derived SQLite/FTS index; rebuildable
  skills/
    <skill-id>/SKILL.md               # optional exported Codex-compatible skill
    <skill-id>/references/*.md
  prompts/
    memp_workflow_from_trajectory.md
    memp_events_from_trajectory.md
    memp_workflow_from_events.md
    memp_reflect_adjust_memory.md
```

The `prompts/` directory should store local adapted prompt templates with source attribution comments:

```text
Adapted from zjunlp/MemP:
- ProcedureMem/prompt_generator.py
- ProcedureMem/memory_adjust.py
```

Do not store raw secrets in memory items. Reuse `secret_exposure_signal` before promoting a memory to accepted status.

## Prompt Templates To Use

Implement `src/retro/mining/prompts/memp.py` or template files under `rollout-memory/memories/prompts/`. The first implementation should adapt MemP's prompts to software-engineering rollouts.

### 1. Workflow From Trajectory

Source reference: `zjunlp/MemP/ProcedureMem/prompt_generator.py::generate_workflow_from_trajectory_prompt`.

Purpose: generate a compact workflow directly from a task query and full trajectory.

Required behavior:

- Input: user query, normalized trajectory with thought/action/result or event summaries.
- Select only critical steps that contributed positively.
- Output a natural, coherent paragraph or a JSON object with `workflow`, `critical_steps`, and `guardrails`.
- Do not output a numbered list unless the caller requests `format=steps`.

Software-engineering adaptation:

```text
You are given a software-engineering task and an agent trajectory made of reasoning, tool calls, file reads/edits, commands, and results.
Generate a reusable workflow for solving similar future tasks.
Only include critical steps that materially helped the task, such as inspecting relevant files, searching for symbols, editing code, running validation, fixing errors, or updating docs/specs.
Ignore dead ends unless they teach a guardrail.
Output JSON with:
- workflow: concise natural-language workflow
- critical_steps: ordered list of reusable actions
- guardrails: mistakes or conditions that should change the workflow
- evidence_step_ids: source step ids
```

### 2. Events From Trajectory

Source reference: `zjunlp/MemP/ProcedureMem/prompt_generator.py::generate_events_from_trajectory_prompt`.

Purpose: convert noisy trajectory into event records with pre-state, action, and new-state.

Required behavior:

- Input: normalized rollout events.
- Output strict JSON list.
- Preserve `event_id` or `step_id` for provenance.
- Use software-engineering states such as `repo inspected`, `test failed`, `patch applied`, `validation passed`, `user corrected direction`.

Software-engineering adaptation:

```text
Convert each important trajectory step into an event:
{
  "step": <integer>,
  "event_id": "<source event id>",
  "pre_state": "<state before the action>",
  "action": "<tool/action taken>",
  "actor": "assistant|tool|user",
  "new_state": "<state after the result>",
  "outcome": "useful|neutral|failed|invalid",
  "evidence_refs": ["..."]
}
Return only JSON.
```

### 3. Workflow From Events

Source reference: `zjunlp/MemP/ProcedureMem/prompt_generator.py::generate_workflow_from_events_prompt`.

Purpose: identify critical events from a normalized event graph before generating memory.

Required behavior:

- Input: query and generated event list.
- Output strict JSON with step ids and rationale.
- Prefer events with positive contribution and validation evidence.
- Include failure events only when they teach a reusable guardrail.

### 4. Reflection / Adjust Memory

Source reference: `zjunlp/MemP/ProcedureMem/memory_adjust.py`.

Purpose: update a workflow when a future rollout guided by that memory fails.

Required behavior:

- Input: existing workflow, reward, trajectory, and failed evidence.
- If reward is low, explain why the workflow failed and return a revised workflow.
- Preserve correct parts; change only the misleading or incomplete parts.
- Output strict JSON:

```json
{
  "analysis": "...",
  "action": "keep|revise|deprecate",
  "revised_workflow": "...",
  "new_guardrails": ["..."],
  "confidence": 0.0
}
```

## Build Policies

Implement these memory builders behind `retro mine --method ...`.

### `memp_direct_llm`

Directly adapt MemP workflow generation:

```text
normalized rollout -> trajectory text -> workflow JSON -> MemoryCandidate(kind=procedure)
```

Use for fast, cheap procedural memories.

### `memp_round_llm`

Two-stage MemP build:

```text
normalized rollout -> event JSON -> critical event selection -> workflow/skill
```

Use when the rollout is long, noisy, or contains many failures.

### `skillbank_llm`

SkillRL/SkillNet-inspired builder:

```text
rollout + signals -> candidate skill -> quality evaluator -> SkillBank item
```

Output skill fields:

- name,
- description,
- domains,
- activation,
- steps,
- termination,
- verification,
- dependencies,
- examples,
- failure modes,
- evidence refs.

### `case_memrl`

MemRL-inspired episodic builder:

```text
intent + summarized experience + reward/utility -> case memory
```

Output:

- `intent`: first user request or task summary,
- `experience`: what happened,
- `strategy`: what worked or failed,
- `reward`: derived from signals,
- `q`: initial utility estimate,
- `evidence_refs`.

### `memory_weaver`

MemGen-inspired text compressor:

```text
retrieved memories + current task -> short synthesized context block
```

This is a retrieval-time prompt block generator, not a stored memory builder.

## Retrieval

Implement retrieval in two phases.

### Phase A: Recall

Start local and simple:

- exact repo key match,
- kind filter,
- lexical BM25-ish overlap or SQLite FTS when available,
- optional embedding search later.

Candidates should include:

- top repo-scoped memories,
- top user-scoped preferences,
- relevant task/global skills,
- recent successful cases.

### Phase B: Value Rerank

Inspired by MemRL:

```text
score = semantic_score * 0.45
      + q_value * 0.35
      + quality_score * 0.10
      + recency_score * 0.05
      + scope_match * 0.05
      - risk_penalty
```

Fields:

- `q_value`: memory utility estimate.
- `quality_score`: average of SkillNet-style quality dimensions.
- `risk_penalty`: high if stale repo, low confidence, secret exposure, unvalidated workflow, or repeated previous failure.

## Memory Trigger

Inspired by MemGen, add a retrieval trigger before injecting memory:

```text
should_retrieve = task_is_nontrivial
               or repo_has_prior_memories
               or user_asks_for_memory/reuse
               or current task resembles past failures
```

Allow abstention:

- `none`: no memory useful enough,
- `ask`: memory conflicts or scope mismatch,
- `inject`: safe to use.

Add CLI:

```bash
retro memory retrieve --host codex --session-id <id>
retro memory retrieve --query "..." --cwd /path/to/repo
retro memory weave --query "..." --cwd /path/to/repo
```

## Utility Update

Add lifecycle events:

```json
{
  "event": "memory_used|memory_rewarded|memory_revised|memory_deprecated",
  "memory_id": "...",
  "session_id": "...",
  "reward": 0.0,
  "reason": "...",
  "created_at": "..."
}
```

Update rules:

- On use, increment `hits`.
- On successful session, increment `successes`, update Q upward.
- On failure/user correction/interruption, increment `failures`, update Q downward.
- If `hits >= 3` and success rate `< 0.5`, mark `status=deprecated` or send to reflection.
- Use MemP reflection prompt to revise a memory when there is enough failed evidence.

Default non-parametric update:

```text
q_new = q_old + alpha * (reward - q_old)
alpha = 0.2
```

Reward should be derived from existing signals:

- positive user satisfaction: `1.0`
- explicit validation after edits: `0.8`
- commit made during session: `0.7`
- user correction after memory use: `0.2`
- interrupted or failed command loop: `0.1`
- secret exposure or unsafe action: `0.0`

## Skill Evaluation

Add a SkillNet-inspired evaluator for every `skill` candidate.

Dimensions:

- `safety`: no secrets, no destructive defaults, no overbroad commands.
- `completeness`: activation, steps, termination, and verification are present.
- `executability`: steps are concrete enough for a coding agent.
- `maintainability`: concise, scoped, and not too repo-specific unless repo-scoped.
- `cost_awareness`: avoids huge context dumps and unnecessary reruns.

Implementation:

- deterministic v0 scorer for required fields and risk patterns,
- optional LLM judge later,
- store scores in `structured.quality`.

Promotion policy:

- `candidate` by default,
- `accepted` if quality average >= 0.7 and confidence >= 0.6,
- `needs_review` if high priority but low safety or unclear scope,
- `deprecated` if utility drops after repeated use.

## Skill Export

Export high-quality skills as Codex-compatible local skills:

```text
rollout-memory/memories/skills/<skill-id>/SKILL.md
rollout-memory/memories/skills/<skill-id>/references/evidence.md
```

`SKILL.md` structure:

```markdown
# <Skill Name>

## When To Use

...

## Steps

...

## Verification

...

## Do Not Use When

...

## Provenance

- source session: ...
- memory id: ...
```

Do not auto-install generated skills into `$CODEX_HOME/skills` in v1. Generate them under `rollout-memory/` for review.

## Implementation Plan

### Phase 1: Prompt Assets

Add:

```text
src/retro/mining/prompts/
  __init__.py
  memp.py
```

Functions:

```python
def workflow_from_trajectory_messages(query: str, trajectory: str) -> list[dict]: ...
def events_from_trajectory_messages(trajectory: str) -> list[dict]: ...
def workflow_from_events_messages(query: str, events_json: str) -> list[dict]: ...
def reflect_adjust_memory_messages(workflow: str, reward: float, trajectory: str) -> list[dict]: ...
```

Each function must include a docstring referencing the MemP source path it adapts.

### Phase 2: LLM Mining Method Skeletons

Add:

```text
src/retro/mining/methods/memp_llm.py
src/retro/mining/methods/skillbank_llm.py
src/retro/mining/methods/case_memrl.py
```

V1 can support `--dry-run-prompts` before wiring provider calls, because mining with external LLMs needs explicit user configuration.

CLI:

```bash
retro mine codex <id> --method memp_direct_llm
retro mine codex <id> --method memp_round_llm
retro mine codex <id> --method skillbank_llm
retro mine codex <id> --method case_memrl
```

### Phase 3: Memory Store

Add:

```text
src/retro/memory_store.py
```

Responsibilities:

- append/update `items.jsonl`,
- write lifecycle events,
- build the derived SQLite/FTS index,
- deduplicate by content hash + scope + kind,
- preserve source provenance.

### Phase 4: Retrieval And Weaving

Implemented in:

```text
src/retro/memory_store.py
```

Commands:

```bash
retro memory init
retro memory reindex
retro memory doctor
retro memory import-authored /path/to/markdown
retro memory retrieve --query "..." --cwd /path/to/repo
retro memory weave --query "..." --cwd /path/to/repo
retro memory update-utility --memory-id ... --reward 0.8 --session-id ...
```

### Phase 5: Skill Export

Add:

```bash
retro memory export-skill --memory-id <id>
retro memory export-skills --status accepted
```

Generate reviewable `SKILL.md` folders only.

### Phase 6: Dashboard

Add dashboard sections:

- memory count by kind/status/scope,
- top utility memories,
- deprecated memories,
- skills needing review,
- memory lifecycle events,
- source session links.

## Acceptance Criteria

- Specified MemP prompt templates are ported/adapted with source references in code comments or docstrings.
- `retro mine --method memp_direct_llm --dry-run-prompts` can render the prompt for an existing normalized session.
- `retro mine --method case_memrl` emits case memories with initial utility fields.
- `retro memory list` reads `rollout-memory/memories/items.jsonl`.
- Utility update changes Q-values without model weight updates.
- Skill candidates include SkillNet-style quality scores.
- No generated memory is promoted to `accepted` if secret-risk signals are positive.
- Generated Codex skills are written under `rollout-memory/memories/skills/` and are not auto-installed.

## Non-Goals

- Do not train or fine-tune a model in v1.
- Do not implement latent token memory from MemGen.
- Do not require FAISS, LangChain, vLLM, Ray, or benchmark environments for local use.
- Do not vendor MemP, SkillRL, MemRL, SkillNet, SkillZero, or MemGen into this repo.
- Do not inject memories automatically into live Codex/Claude sessions until retrieval risk controls exist.
