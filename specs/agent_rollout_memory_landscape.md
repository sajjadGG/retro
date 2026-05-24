# Agent Rollout Memory Landscape

Search date: 2026-05-21

## Summary

There is already a fast-growing ecosystem of persistent memory tools for coding agents. Most projects focus on storing and retrieving facts, decisions, project context, or conversation summaries through MCP, local files, SQLite, vector search, or knowledge graphs.

The less-solved opportunity is rollout-derived learning: mining full coding-agent traces for mistakes, corrections, successful workflows, project-specific habits, and user working style, then evaluating whether those lessons reduce repeated failures in future sessions.

Important distinction: persistent memory is not the same as whole-rollout storage. Most tools below store extracted observations, summaries, decisions, or selected hook events. Only a subset appears to preserve replayable event-level traces, and even those do not capture hidden model reasoning that the host agent never exposes.

## Existing OSS Projects

| Project | What it appears to do | Relevance |
| --- | --- | --- |
| [agentmemory](https://github.com/rohitg00/agentmemory) | Persistent memory for Claude Code, Cursor, Gemini CLI, Codex CLI, and MCP clients; advertises hooks, skills, confidence scoring, lifecycle, knowledge graphs, hybrid search, raw observations, and replayable sessions with prompts, tool calls, tool results, and responses. | Closest broad competitor for cross-agent persistent coding memory and the closest to whole-rollout event capture, though Codex Desktop hook support is described as incomplete. |
| [jayzeng/agentmemory](https://github.com/jayzeng/agentmemory) | CLI-oriented memory with long-term notes, daily logs, scratchpad, topics, and context injection. | Useful baseline for lightweight markdown-first memory. |
| [MemoryGraph](https://github.com/memory-graph/memory-graph) | Graph-based MCP memory server for coding agents; stores patterns and relationships across sessions. | Strong fit for relationship tracking, but the agent must be prompted/configured to use it. |
| [mcp-memory-keeper](https://github.com/mkreyman/mcp-memory-keeper) | Persistent context management for Claude Code sessions, including checkpoints and topic channels. | Good reference for session continuity and compaction recovery. |
| [memory-store-plugin](https://github.com/julep-ai/memory-store-plugin) | Claude Code plugin that tracks development flow, session context, commits, corrections, patterns, and decisions. Its docs emphasize session summaries, files changed, commits, quality score, and queued session events rather than full raw rollout replay. | Very close to the user problem for automatic development tracking, but not clearly full rollout storage. |
| [codex-mem](https://github.com/Just-Boring-Cat/codex-mem) | Local-first SQLite-backed MCP memory server for Codex with progressive retrieval and optional automatic workflow helpers. Docs describe saving key decisions, fixes, constraints, docs, logs, and selected entries. | Direct Codex-specific memory reference, but not whole-rollout capture. |
| [total-agent-memory](https://github.com/vbcherepanov/total-agent-memory) | Self-hosted memory for Claude Code and Codex CLI with episodic/session tools, procedural workflow tools, KG, embeddings, and visualization. It has hooks for file edits, bash errors, prompts, post-tool-use, and session end; `session_end` can accept a transcript. | Interesting because it explicitly includes procedural/workflow memory and some transcript/hook capture, but full rollout storage appears optional/partial rather than the central abstraction. |
| [memorix](https://github.com/AVIDS2/memorix) | Cross-agent memory layer via MCP for many coding agents. | Useful for portability and protocol design. |
| [icarus-memory-infra](https://github.com/esaradev/icarus-memory-infra) | Memory infrastructure with provenance, rollback, lifecycle/supersession, working memory, session archive, and wiki. | Useful for trust, stale-memory handling, and source-backed memory. |
| [claude-code-memory-setup](https://github.com/lucasrosati/claude-code-memory-setup) | Claude Code memory setup with Obsidian, knowledge graphs, and chat import pipeline. | Useful for developer-facing knowledge graph workflows. |

## Relevant Papers And Benchmarks

| Work | Main idea | Relevance |
| --- | --- | --- |
| [Memory Matters](https://ojs.aaai.org/index.php/AAAI-SS/article/view/27688) | Reviews long-term memory in LLM agents and calls out procedural, episodic, semantic memory separation and lifetime memory management. | Good conceptual framing. |
| [Memory for Autonomous LLM Agents](https://arxiv.org/abs/2603.07670) | Surveys memory as a write-manage-read loop; covers compression, retrieval stores, reflective self-improvement, hierarchical context, and policy-learned management. | Good taxonomy for architecture. |
| [Experiential Reflective Learning](https://huggingface.co/papers/2603.24639) | Reflects on task trajectories and outcomes to extract transferable heuristics, then retrieves those heuristics at test time. | Very close to the desired rollout-to-lesson pipeline. |
| [Learning to Retrieve from Agent Trajectories](https://huggingface.co/papers/2604.04949) | Mines agent search trajectories for retrieval supervision: chosen documents, skipped documents, and reasoning traces. | Strong example of turning rollout data into training/evaluation signal. |
| [MemoryArena](https://arxiv.org/abs/2602.16313) | Evaluates memory in interdependent multi-session agent-environment tasks where agents must learn from previous actions and feedback. | Good benchmark design inspiration for multi-session coding tasks. |
| [AMA-Bench](https://arxiv.org/abs/2602.22769) | Uses real and synthetic agentic trajectories; argues memory systems need causality and objective information, not just similarity retrieval. | Important for trace causality and objective event modeling. |
| [StructMemEval](https://arxiv.org/abs/2602.11243) | Tests whether agents organize memory into useful structures, not only recall facts. | Relevant to deciding memory schemas: tasks, ledgers, rules, workflows, corrections. |
| [EvoMemBench](https://arxiv.org/abs/2605.18421) | Evaluates in-episode vs cross-episode and knowledge-oriented vs execution-oriented memory; finds procedural memory helps execution-oriented tasks when experiences match task structure. | Highly relevant to coding-agent mistake reduction. |
| [LoCoMo](https://aclanthology.org/anthology-files/pdf/acl/2024.acl-long.747.pdf) and [LongMemEval](https://openreview.net/pdf?id=wIonk5yTDq) | Long-term conversational memory benchmarks. | Useful baselines, but not enough for coding-agent workflow learning. |

## Gap For This Project

The existing tools mostly answer: "How can an agent remember and retrieve context?"

This project should answer: "How can an agent learn working behavior from previous rollouts, avoid repeating mistakes, and adapt to a specific human collaborator?"

After checking the closest repos, the sharper gap is: whole-rollout capture plus learning. The target artifact should be a normalized event log, not only extracted memory cards. That log should preserve enough of the session to replay and re-analyze it later: prompts, assistant messages, tool calls, tool inputs, tool outputs, file edits, command outputs, errors, user corrections, final answer, and post-session assessment.

That suggests a sharper project scope:

1. Capture coding-agent rollouts from Codex and Claude.
2. Normalize them into an event schema: user request, plan, reads, edits, commands, failures, corrections, tests, final outcome.
3. Extract candidate memories into explicit types: preference, repo convention, failure mode, workflow, tool habit, evaluation rule.
4. Preserve provenance and confidence for each memory.
5. Retrieve memories by task and repo context.
6. Evaluate on replay tasks where the baseline agent repeats known mistakes.

## Initial Differentiator

The differentiator should not be "another memory store." It should be a behavioral learning layer over rollout traces, with measurable outcomes:

- fewer repeated corrections from the user,
- fewer repo-convention violations,
- better first-test choice,
- better recovery from known failure modes,
- less irrelevant memory injection,
- auditable memories with source rollout links.
