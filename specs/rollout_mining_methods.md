# Rollout Mining Methods

Search date: 2026-05-21

## Purpose

This project should support multiple rollout-to-memory methods behind one interface:

```text
normalized rollout -> mined memories/skills -> later retrieval -> prompt memory block
```

The first implementation phase should focus on extraction and storage only:

```text
normalized rollout -> scoped mined memories/skills
```

Retrieval and prompt/session injection are the next phase. The memory block can
later be fetched once before a future Codex or Claude Code run and placed in the
prompt. Mid-execution memory injection can come after start-of-session retrieval.

## Shared Output

Every method should produce comparable memory candidates:

- `kind`: procedure, case, skill, failure_trigger, user_preference, repo_convention, tool_lesson, risk_rule.
- `text`: the memory text to inject.
- `when_to_use`: activation guidance.
- `do_not_use_when`: optional guardrail for stale, mismatched, or risky use.
- `evidence_refs`: event IDs from the captured rollout.
- `source_sessions`: one or more `{host, session_id}` pairs that support the memory.
- `confidence`: evidence strength.
- `priority`: usefulness for future runs.
- `risk`: low, medium, or high.
- `scope`: user or repo for v0. Host, global, and task-type scopes can come later.
- `repo`: repo association when `scope=repo`, including the source working directory and stable repo key.
- `user`: user association when `scope=user`, using a local user key or profile id rather than raw PII.
- `created_at`: extraction timestamp.
- `updated_at`: last consolidation/update timestamp, if any.
- `status`: candidate, accepted, rejected, superseded, or deprecated.

Skill memories should additionally include:

- `activation`: conditions that should cause the skill to be considered.
- `steps`: ordered execution guidance.
- `termination`: when the skill is done or should be abandoned.
- `verification`: how the future agent should check that it followed the skill correctly.

## Scope And Provenance Requirements

Memory extraction must associate every memory or skill with the source directory
it came from and the scope in which it is valid.

### Repo Scope

Use repo scope when the memory depends on a specific codebase, project layout,
local commands, tests, deployment workflow, conventions, or hidden constraints.

Each repo-scoped memory must include:

```json
{
  "scope": "repo",
  "repo": {
    "cwd": "/absolute/path/seen/in/session",
    "root": "/absolute/git/root/or-cwd",
    "key": "stable-local-repo-key",
    "name": "repo-directory-name",
    "git_remote": "optional-redacted-origin-url",
    "git_head": "optional-commit-sha-at-extraction"
  }
}
```

The `key` should be stable across sessions for the same local repo. Prefer the
git root plus remote URL when available; otherwise use the normalized absolute
root path. Do not require git: sessions in non-git directories should still be
scoped to their working directory.

### User Scope

Use user scope when the memory reflects the collaborator's durable preference or
working style across repositories, such as how they want status updates, review
style, approval behavior, or research rigor.

Each user-scoped memory must include:

```json
{
  "scope": "user",
  "user": {
    "key": "local-user-key",
    "source": "local-profile|host-user|default-local-user"
  }
}
```

V0 should support only `repo` and `user` scopes. A future consolidation phase can
promote repeated repo memories into user-level preferences, or demote broad user
memories when they only apply to one repo.

### Session Provenance

Every memory and skill must be auditable back to the sessions and events that
produced it:

```json
{
  "source_sessions": [
    {
      "host": "codex|claude-code",
      "session_id": "...",
      "cwd": "/absolute/path/seen/in/session",
      "started_at": "iso-8601-or-null",
      "ended_at": "iso-8601-or-null"
    }
  ],
  "evidence_refs": [
    {
      "host": "codex|claude-code",
      "session_id": "...",
      "event_id": "...",
      "reason": "user_correction|successful_workflow|failed_command|file_edit|final_outcome"
    }
  ]
}
```

The dashboard and future retrieval layer must be able to jump from a memory to
the source session and rendered transcript.

## Storage Requirements

Session-level mining artifacts may continue to live under the method-specific
path:

```text
rollout-memory/mined/<method>/<host>/<session-id>.json
rollout-memory/mined/<method>/<host>/<session-id>.prompt.md
```

V0 must also write normalized memory records into scope-indexed locations:

```text
rollout-memory/memories/
  repos/<repo-key>/memories.jsonl
  users/<user-key>/memories.jsonl
  index.json
```

The `memories.jsonl` rows should use the shared output schema and include method,
scope, repo/user metadata, source sessions, evidence refs, status, confidence,
priority, and risk. The `index.json` should map repo keys and user keys to their
human-readable labels and storage paths so the dashboard and future retrieval
can enumerate memories without scanning every mined artifact.

For v0, extraction should append candidate memories or update exact duplicates
by stable id/content hash. Full consolidation, contradiction handling,
supersession, and decay can be implemented after basic extraction works.

## Extraction V0 Boundary

The next implementation slice is memory and skill extraction only. It should:

1. Read normalized rollout events and available session metadata.
2. Infer repo/user scope from `cwd`, raw metadata, and normalized session events.
3. Extract candidate memories and skills.
4. Attach source session and evidence metadata.
5. Write method-specific artifacts and scope-indexed memory records.
6. Expose extracted memories to the dashboard.

It should not yet:

- inject memories into Codex or Claude Code sessions,
- decide final retrieval ranking for a new task,
- implement mid-session memory injection,
- require vector search,
- require cross-session consolidation beyond simple duplicate handling.

## Dashboard Requirements

The dashboard should visualize mined memories and skills alongside sessions and
signals.

Minimum dashboard views:

- per-session mining status: missing, mined, candidate count, high-risk count;
- repo memory view: all memories/skills associated with a repo key and source directory;
- user memory view: user-scoped preferences and working-style lessons;
- memory detail drawer/page: text, kind, scope, risk, confidence, priority, status, method, source sessions, evidence event refs;
- source navigation: link from a memory to the rendered rollout/session and event anchor when available;
- filters by scope, repo, user, kind, status, risk, confidence, method, host, and date.

Dashboard data should come from:

```text
rollout-memory/memories/index.json
rollout-memory/memories/repos/<repo-key>/memories.jsonl
rollout-memory/memories/users/<user-key>/memories.jsonl
rollout-memory/mined/<method>/<host>/<session-id>.json
rollout-memory/signals/readings.jsonl
```

The dashboard should not need to parse raw rollouts to show memory summaries.

## Methods And Papers

| Method | Paper | Core idea | How to use it here |
| --- | --- | --- | --- |
| `memp_procedural` | [Memp: Exploring Agent Procedural Memory](https://arxiv.org/abs/2508.06433) | Distills past agent trajectories into fine-grained step instructions and higher-level script-like procedural memories. It studies build, retrieval, and update strategies for procedural memory. | Mine successful rollouts into reusable coding procedures: preconditions, ordered steps, warnings, and deprecation notes. |
| `memento_case` | [Memento: Fine-tuning LLM Agents without Fine-tuning LLMs](https://arxiv.org/abs/2508.16153) | Uses memory-based online reinforcement learning. Past experiences are stored as cases and reused through retrieval instead of model weight updates. | Store rollout cases as situation -> action -> outcome -> feedback. Useful when a future task resembles a prior one. |
| `legomem_role` | [LEGOMem: Modular Procedural Memory for Multi-agent LLM Systems](https://arxiv.org/abs/2510.04851) | Decomposes trajectories into reusable memory units and places them differently for orchestrator agents vs task agents. | Split memories for parent Codex/Claude sessions and sub-agents. Orchestrator memories guide delegation; worker memories guide execution details. |
| `reme_refine` | [Remember Me, Refine Me](https://arxiv.org/abs/2512.10696) | Extracts success patterns, failure triggers, and comparative insights, then refines/prunes memory by utility. | Current POC. Mine rollouts into success patterns, failure triggers, and risk rules. Keep evidence-linked candidates and prune weak memories later. |
| `macla_hierarchical` | [Learning Hierarchical Procedural Memory for LLM Agents through Bayesian Selection and Contrastive Refinement](https://arxiv.org/abs/2512.18950) | Maintains external hierarchical procedural memory while keeping the LLM frozen. Tracks reliability and refines procedures by contrasting success and failure. | Store high-level workflows with low-level steps and reliability estimates. Useful after repeated sessions create enough evidence. |
| `skill_pro` | [Skill-Pro: Learning Reusable Skills from Experience](https://arxiv.org/abs/2602.01869) | Converts episodic narratives into executable skills with activation, execution, and termination conditions, plus verification and maintenance. | Convert rollouts into skills such as "verify memory system claims" or "update project spec." Include when to activate, steps, stop condition, and verification gate. |
| `risk_aware` | [Learning When to Remember](https://arxiv.org/abs/2604.27283) | Treats memory retrieval for coding agents as a risk-sensitive decision. It can inject memory, summarize multiple candidates, abstain, or ask for feedback. | Add a safety layer before prompt injection. Do not inject memories that are superficially similar but likely wrong, stale, or repo-incompatible. |
| `context_as_tool` | [Context as a Tool: Context Management for Long-Horizon SWE-Agents](https://openreview.net/forum?id=sN3CHd0MSW) | Treats context management as an explicit action for software-engineering agents, using trajectory-level supervision to decide what context to keep or retrieve. | Use rollouts to learn what context mattered for coding tasks: files read, commands run, errors fixed, and docs consulted. |
| `memrepair_hierarchical` | [MemRepair](https://arxiv.org/abs/2605.17444) | Uses hierarchical memories for repository-level vulnerability repair: history fixes, security patterns, and refinement trajectories. | Adapt the hierarchy beyond security: past fixes, repo patterns, and refinement trajectories for repeated coding failures. |
| `longmemeval_runbook` | [LongMemEval-V2](https://arxiv.org/abs/2605.12493) | Evaluates long-term memory systems that behave like experienced colleagues; AgentRunbook stores trajectories as files that agents can inspect. | Keep rollout memories inspectable as files, not only vector entries. Let future agents cite and inspect prior rollout evidence. |

## Current POC

The implemented method is `reme_refine_poc`.

It is deterministic and local: no LLM call is required. It reads normalized events and emits:

- `failure_trigger` memories,
- `procedure` memories,
- `tool_lesson` memories,
- `risk_rule` memories.

Output paths:

```text
rollout-memory/mined/reme_refine_poc/<host>/<session-id>.json
rollout-memory/mined/reme_refine_poc/<host>/<session-id>.prompt.md
```

Commands:

```bash
retro mine codex <thread-id>
retro mine claude <session-id>
retro mine '*' '*'
```

## Next Methods To Implement

1. `skill_pro`: likely most useful for coding agents because it creates activation/execution/termination/verification blocks.
2. `memp_procedural`: good general baseline for procedure extraction.
3. Scope-indexed memory storage for repo/user memories and skills.
4. Dashboard visualization of scoped memories and provenance.
5. `risk_aware`: should sit after other methods and decide what is safe to inject.

## Retrieval Next Phase

After extraction and dashboard visibility work, implement retrieval as a separate
system. Retrieval should select memories by:

- current repo key / working directory,
- user key,
- task description,
- touched files or likely task area,
- memory kind,
- risk and confidence,
- freshness and supersession status,
- source-session outcomes and signals.

The retrieval layer should be able to abstain when memories are stale,
repo-incompatible, low-confidence, or only superficially similar. Prompt/session
injection should be designed only after retrieval can produce a small,
evidence-backed candidate set.

## Evaluation

Compare methods by running the same rollout through each miner and testing the prompt block on a future or replayed task.

Measure:

- repeated mistake reduction,
- prompt block size,
- evidence quality,
- stale-memory risk,
- user correction rate,
- repo-convention compliance,
- tool-use efficiency.
