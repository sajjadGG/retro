# Feature Spec: Full Rollout Capture And Memory Mining

## Purpose

Build a project that captures complete coding-agent rollouts from Codex and Claude Code, stores them as durable local artifacts, and mines those artifacts for useful lessons that can improve future sessions.

The first goal is total data capture. The second goal is learning from that captured data.

## Problem

Codex and Claude Code sessions contain a rich record of how work actually happens: user prompts, assistant messages, tool calls, command outputs, file reads, edits, sub-agent work, failures, corrections, and final answers. This rollout data is valuable because it shows the agent's working style and the user's correction patterns.

Today that data is scattered across host-specific storage. Some of it exists in local rollout or transcript files. Some of it is visible through lifecycle hooks. Some of it may only be available indirectly through file-history sidecars, tool-event hooks, command logs, or host-specific databases.

The project should make this explicit: collect everything available, normalize it, preserve provenance, and then mine it.

## Goals

### Objective 1: Full Rollout Capture

For any Codex or Claude Code session, capture the fullest possible rollout:

- user messages,
- assistant messages,
- system-visible attachments and context events,
- tool calls,
- tool inputs,
- tool outputs,
- shell commands,
- stdout, stderr, exit codes, and timing where available,
- file reads,
- file writes and edits,
- pre-edit and post-edit content or diffs where available,
- sub-agent creation, prompts, outputs, and parent-child relationships,
- permission prompts or approval decisions where available,
- compaction events,
- session start and end metadata,
- final answer,
- interruption, error, or cancellation events.

The captured artifact can be markdown, JSONL, SQLite, or another format. Format is secondary. Completeness and replayability are primary.

### Objective 2: Memory Mining

Run a mining layer over captured rollouts to extract useful future-session knowledge:

- key mistakes the agent made,
- user corrections,
- repeated patterns in the user's preferences,
- repo-specific conventions,
- tool-use habits that worked or failed,
- workflows that led to successful outcomes,
- recurring failure modes,
- "next time, do this" rules,
- "avoid doing this again" rules,
- unresolved follow-up items.

Mining can happen in two modes:

1. **Session-end hook mining:** run immediately when a session closes and extract a small set of lessons.
2. **Async batch mining:** periodically scan all captured rollouts and consolidate higher-confidence patterns across many sessions.

## Non-Goals For V0

- Do not train or fine-tune a model.
- Do not require cloud storage.
- Do not require a perfect universal schema before capture works.
- Do not assume hidden chain-of-thought or model-private reasoning is available.
- Do not depend on one host's undocumented file format as the only capture method.

## Current Host Observations

These observations should be verified during implementation because both Codex and Claude Code may change storage internals.

### Claude Code

Claude Code appears to already write per-project transcript JSONL files under:

```text
~/.claude/projects/<project-slug>/<session-id>.jsonl
```

Local inspection shows these JSONL files include user messages, assistant messages, attachments, tool-related events, `sessionId`, `cwd`, timestamps, parent UUIDs, and `isSidechain` flags. Claude also keeps sidecar directories such as:

```text
~/.claude/file-history/
~/.claude/tasks/
~/.claude/todos/
~/.claude/sessions/
~/.claude/history.jsonl
```

Anthropic's Claude Code hook documentation says hook inputs include `session_id`, `transcript_path`, `cwd`, and `hook_event_name` for events such as `UserPromptSubmit`, `Notification`, `Stop`, and others. It also documents tool hooks including `PreToolUse` and `PostToolUse`, plus `SubagentStop`, `PreCompact`, `SessionStart`, and `SessionEnd`.

Implication: Claude Code v0 capture should use the transcript JSONL as the source of truth, with hooks used to discover transcript paths, capture extra tool metadata, and trigger session-end mining.

### Codex

Local Codex Desktop inspection shows:

```text
~/.codex/state_5.sqlite
~/.codex/logs_2.sqlite
~/.codex/session_index.jsonl
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl
```

The local `threads` table in `state_5.sqlite` includes a `rollout_path` column. Recent thread rows point directly to rollout JSONL files under `~/.codex/sessions/...`.

Codex also has tables for sub-agent/thread relationships, including `thread_spawn_edges`, plus other job/thread state tables. Logs are stored in `logs_2.sqlite`.

Implication: Codex v0 capture should use `state_5.sqlite.threads.rollout_path` to discover rollout files, use the rollout JSONL as the source of truth, and augment it with thread metadata, spawn edges, and logs where useful.

## Capture Strategy

### Principle

Prefer native transcript/rollout files when available. Use hooks as a discovery, enrichment, and trigger mechanism. Use filesystem/database polling as a fallback.

### Claude Code Capture

V0 should support:

1. Register Claude Code hooks:
   - `SessionStart`
   - `UserPromptSubmit`
   - `PreToolUse`
   - `PostToolUse`
   - `PreCompact`
   - `Stop`
   - `SubagentStop`
   - `SessionEnd`
2. Each hook appends its JSON stdin payload to a local raw event log.
3. Each hook records `transcript_path` when present.
4. On `SessionEnd`, copy or index the transcript JSONL.
5. Also snapshot relevant sidecars if they exist:
   - file history entries for the session,
   - task/sub-agent JSON files,
   - todo files,
   - session metadata.

### Codex Capture

V0 should support:

1. Discover active and completed Codex sessions from:
   - `~/.codex/session_index.jsonl`,
   - `~/.codex/state_5.sqlite.threads`,
   - `threads.rollout_path`.
2. Copy or index each rollout JSONL file.
3. Join thread metadata from `state_5.sqlite`.
4. Join parent-child relationships from `thread_spawn_edges`.
5. Optionally join warning/error logs from `logs_2.sqlite` by `thread_id`.
6. If Codex hooks are available in the active environment, add a hook adapter later; do not block v0 on hooks.

## Storage Model

Store raw capture and derived artifacts separately.

```text
rollout-memory/
  raw/
    claude-code/
      <session-id>/
        transcript.jsonl
        hooks.jsonl
        sidecars/
    codex/
      <thread-id>/
        rollout.jsonl
        thread.json
        logs.jsonl
        sidecars/
  normalized/
    <host>/<session-id>.events.jsonl
  rendered/
    <host>/<session-id>.md
  mined/
    session-lessons/
      <host>/<session-id>.json
      <host>/<session-id>.md
    consolidated/
      user-preferences.md
      repo-conventions.md
      recurring-mistakes.md
      workflows.md
      open-questions.md
  memories/
    repos/<repo-key>/memories.jsonl
    users/<user-key>/memories.jsonl
    index.json
```

### Raw Files

Raw files must be immutable by default. They should preserve the host's original event order and original fields.

### Normalized Events

Each normalized event should include:

```json
{
  "event_id": "stable-id",
  "session_id": "host-session-or-thread-id",
  "host": "codex|claude-code",
  "timestamp": "iso-8601-or-null",
  "sequence": 123,
  "parent_event_id": "optional",
  "actor": "user|assistant|tool|system|subagent|hook",
  "event_type": "message|tool_call|tool_result|file_read|file_edit|command|error|session_start|session_end|subagent_start|subagent_end|compaction|permission|unknown",
  "summary": "short human-readable label",
  "raw_ref": {
    "path": "raw file path",
    "line": 42
  },
  "payload": {}
}
```

### Rendered Markdown

For human reading, generate a markdown transcript from normalized events:

- metadata block,
- timeline,
- user/assistant messages,
- tool calls and results,
- command outputs,
- file edits/diffs,
- sub-agent sections,
- errors and warnings,
- final answer,
- mining summary links.

Markdown is a view, not the source of truth.

## Mining Layer

See also: [Rollout Mining Methods](rollout_mining_methods.md) for the paper-backed method menu, including Memp, Memento, LEGOMem, ReMe, MACLA, Skill-Pro, risk-aware retrieval, Context as a Tool, MemRepair, and LongMemEval-V2.

V0 mining is extraction and storage only. It should extract scoped memories and
skills, preserve provenance, and make them visible to the dashboard. Retrieval
and prompt/session injection are separate later phases.

### Session-End Mining

When a session ends, run a lightweight miner that produces:

```json
{
  "session_id": "...",
  "host": "codex|claude-code",
  "source_sessions": [
    {
      "host": "codex|claude-code",
      "session_id": "...",
      "cwd": "/absolute/path/seen/in/session",
      "started_at": "iso-8601-or-null",
      "ended_at": "iso-8601-or-null"
    }
  ],
  "task_summary": "...",
  "outcome": "completed|partial|failed|interrupted|unknown",
  "key_decisions": [],
  "user_preferences": [],
  "repo_conventions": [],
  "mistakes": [],
  "corrections": [],
  "successful_workflows": [],
  "tool_lessons": [],
  "followups": [],
  "candidate_memory_cards": []
}
```

Each candidate memory card should include:

```json
{
  "type": "procedure|case|skill|failure_trigger|user_preference|repo_convention|tool_lesson|risk_rule|followup",
  "claim": "short durable lesson",
  "when_to_use": "activation guidance",
  "do_not_use_when": "optional stale/mismatch guardrail",
  "evidence_refs": [
    {
      "host": "codex|claude-code",
      "session_id": "...",
      "event_id": "...",
      "reason": "user_correction|successful_workflow|failed_command|file_edit|final_outcome"
    }
  ],
  "source_sessions": [
    {"host": "codex|claude-code", "session_id": "...", "cwd": "/absolute/path"}
  ],
  "confidence": 0.0,
  "priority": 3,
  "risk": "low|medium|high",
  "scope": {
    "kind": "repo|user",
    "repo": {
      "cwd": "/absolute/path/seen/in/session",
      "root": "/absolute/git/root/or-cwd",
      "key": "stable-local-repo-key",
      "name": "repo-directory-name",
      "git_remote": "optional-redacted-origin-url",
      "git_head": "optional-commit-sha-at-extraction"
    },
    "user": {
      "key": "local-user-key",
      "source": "local-profile|host-user|default-local-user"
    }
  },
  "expires_at": null,
  "status": "candidate|accepted|rejected|superseded"
}
```

Use repo scope for codebase-specific conventions, workflows, commands, tests,
deployment details, and local constraints. Use user scope for durable
collaborator preferences that should travel across repositories. V0 should
support repo and user scope only; host, global, and task-type scopes can come
later.

### Async Consolidation

The async miner should periodically:

1. Scan new session lessons.
2. Cluster similar lessons.
3. Promote repeated or high-confidence lessons.
4. Detect contradictions and stale memories.
5. Produce concise memory files for future use.
6. Preserve links back to source rollout events.

### Memory Dashboard

The dashboard should show mined memories and skills in addition to sessions and
signals:

- per-session mining status and candidate counts,
- repo-scoped memories grouped by stable repo key and source directory,
- user-scoped preferences and working-style memories,
- memory details including kind, claim, when-to-use, confidence, priority, risk, status, method, source sessions, and evidence refs,
- filters by repo, user, kind, status, risk, confidence, host, method, and date,
- links from each memory back to the rendered rollout and source event when available.

## Retrieval For Future Sessions

Later phases should retrieve relevant lessons at session start based on:

- current repo,
- task description,
- files likely to be touched,
- host agent,
- past failure modes,
- recent user corrections.

V0 only needs to store and mine. Injection back into Codex or Claude Code can come later.

Retrieval should be implemented after scoped extraction and dashboard
visualization. It should be risk-aware, repo-aware, user-aware, and able to
abstain when memories are stale, low-confidence, or incompatible with the
current working directory.

## Privacy And Safety

Rollouts may contain secrets, private code, tokens, customer data, or personal messages. The system must be local-first and conservative.

V0 requirements:

- store locally by default,
- never upload raw rollouts without explicit opt-in,
- support redaction rules,
- detect common secret patterns,
- preserve raw files separately from redacted exports,
- allow session deletion,
- allow project-level ignore rules,
- record provenance for every mined claim.

## Acceptance Criteria

### Capture V0

- Given a Claude Code session, the system can find its transcript JSONL and render a readable markdown rollout.
- Given a Codex session, the system can find `rollout_path` from Codex state and render a readable markdown rollout.
- The rendered rollout includes user messages, assistant messages, tool calls/results, file edits, shell commands, errors, sub-agent events where available, and final answer.
- Raw capture is preserved separately from rendered markdown.
- Missing event types are reported as capture gaps, not silently ignored.

### Mining V0

- A session-end or manual command can mine one captured rollout.
- The miner outputs candidate memory cards with evidence links.
- The miner associates each memory or skill with repo or user scope.
- Repo-scoped memories include the source working directory and stable repo key.
- User-scoped memories include a local user key.
- The miner records source sessions for every candidate memory or skill.
- The miner distinguishes one-off context from durable lessons.
- The miner identifies at least:
  - user corrections,
  - agent mistakes,
  - successful workflows,
  - repo conventions,
  - follow-up items.
- Extracted memories are available through scope-indexed files under `rollout-memory/memories/`.
- The dashboard can visualize mined memories, scope, status, and provenance without parsing raw rollouts.

### Cross-Host V0

- The same normalized event schema supports both Codex and Claude Code.
- Host-specific raw fields are preserved.
- Host-specific adapters are isolated behind a common importer interface.

## Open Questions

- How complete is Codex rollout JSONL compared with the live desktop conversation?
- Does Codex expose reliable hooks in CLI and Desktop, or should v0 rely on rollout files and SQLite only?
- Which Claude Code sidecar files are necessary to reconstruct file edits beyond what the transcript stores?
- How should sub-agent traces be linked if the host records them as separate sessions?
- How aggressively should command output be truncated in markdown views while preserving raw output?
- Should mined memory be stored in markdown first, SQLite first, or both?
- What is the best first eval: repeated user correction reduction, replay task success, or session-start context usefulness?

## Suggested First Implementation Slice

1. Build `claude_importer`:
   - locate transcript JSONL files,
   - parse events,
   - render markdown.
2. Build `codex_importer`:
   - read `state_5.sqlite`,
   - discover `rollout_path`,
   - parse rollout JSONL,
   - render markdown.
3. Define normalized event schema.
4. Write raw + normalized + markdown artifacts to `rollout-memory/`.
5. Build a manual miner command:
   - input: normalized event JSONL,
   - output: session lessons JSON + markdown plus scope-indexed repo/user memory records.
6. Add hook integration:
   - Claude `SessionEnd` triggers import + mining.
   - Codex hook integration only after confirming available hook behavior.
