# Feature Spec: Full Rollout Capture And Memory Mining

## Purpose

Build a project that captures complete coding-agent rollouts from Codex, Claude Code, and VS Code GitHub Copilot Chat, stores them as durable local artifacts, and mines those artifacts for useful lessons that can improve future sessions.

The first goal is total data capture. The second goal is learning from that captured data.

## Problem

Codex, Claude Code, and VS Code Copilot sessions contain a rich record of how work actually happens: user prompts, assistant messages, tool calls, command outputs, file reads, edits, sub-agent work, failures, corrections, and final answers. This rollout data is valuable because it shows the agent's working style and the user's correction patterns.

Today that data is scattered across host-specific storage. Some of it exists in local rollout or transcript files. Some of it is visible through lifecycle hooks. Some of it may only be available indirectly through file-history sidecars, tool-event hooks, command logs, or host-specific databases.

The project should make this explicit: collect everything available, normalize it, preserve provenance, and then mine it.

## Goals

### Objective 1: Full Rollout Capture

For any supported host session, capture the fullest possible rollout:

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

These observations should be verified during implementation because every host may change storage internals.

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

### VS Code GitHub Copilot Chat

VS Code persists local chat sessions under the platform `User` data directory:

```text
workspaceStorage/<workspace-id>/chatSessions/<session-id>.json
workspaceStorage/<workspace-id>/chatSessions/<session-id>.jsonl
globalStorage/emptyWindowChatSessions/<session-id>.json[l]
```

Legacy `.json` files contain a complete session. Current `.jsonl` files are append-only mutation logs whose entry kinds are initial state, property set, array push/splice, and property delete. Copilot agent sessions can also expose:

```text
workspaceStorage/<workspace-id>/GitHub.copilot-chat/transcripts/<session-id>.jsonl
workspaceStorage/<workspace-id>/chatEditingSessions/<session-id>/
workspaceStorage/<workspace-id>/GitHub.copilot-chat/chat-session-resources/<session-id>/
globalStorage/github.copilot-chat/session-store.db
```

The direct Copilot transcript records session/user/assistant/turn/tool lifecycle events with stable ids, parent ids, timestamps, reasoning, tool arguments, and execution outcomes. The core VS Code chat snapshot supplies model ids, token counts, Copilot credits, edit groups, confirmations, and final tool metadata.

Copilot CLI and VS Code Agent Host sessions persist separately:

```text
~/.copilot/session-state/<session-id>/events.jsonl
~/.copilot/session-state/<session-id>/workspace.yaml
~/.copilot/session-store.db
```

These logs contain the current CLI-based sessions shown in VS Code's Agent Sessions UI, including hooks, permissions, external tools, subagents, model changes, usage checkpoints, errors, shutdown/resume events, and active sessions. The SQLite store adds per-model input/output/cache/reasoning accounting.

Implication: combine core VS Code chat discovery with Agent Host session-state discovery. Prefer direct event logs for normalization, preserve each source verbatim, and label the source so the dashboard can distinguish `vscode-chat` from `copilot-cli`.

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

### VS Code Copilot Capture

V0 should support:

1. Discover non-empty Copilot sessions from workspace, empty-window, profile, and VS Code Server user-data roots.
2. Copy the original `.json` or `.jsonl` session file verbatim.
3. Reconstruct and store the current JSONL mutation state without discarding unknown response parts.
4. Prefer the direct Copilot event transcript when present and preserve its event ids and parent links.
5. Snapshot the matching editing session, tool-result resources, workspace metadata, and session-store rows.
6. Preserve request-level model, token, and Copilot-credit fields for dashboard accounting.
7. Discover Copilot CLI and Agent Host rollouts from `~/.copilot/session-state`.
8. Capture active logs as point-in-time snapshots and allow automatic re-import when they grow.
9. Join detailed usage rows from `~/.copilot/session-store.db`.

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
    vscode-copilot/
      <session-id>/
        session.jsonl
        session.snapshot.json
        transcript.jsonl
        events.jsonl
        import_meta.json
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
  "host": "codex|claude-code|vscode-copilot",
  "timestamp": "iso-8601-or-null",
  "sequence": 123,
  "parent_event_id": "optional",
  "actor": "user|assistant|tool|system|subagent|hook",
  "event_type": "message|tool_call|tool_result|file_read|file_edit|command|error|session_start|session_end|subagent_start|subagent_end|compaction|permission|attachment|reasoning|usage|lifecycle|metadata|unknown",
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

## Normalization Fidelity V2

`attachment` means content injected into the model context. `usage` is token or
cost telemetry, `lifecycle` is a content-free state transition, and `metadata`
is a non-conversational state snapshot or update. A tool invocation can use a
specialized call type (`command`, `file_read`, or `file_edit`), but every
ordinary completion is `tool_result`. Both sides carry `payload.call_id`, and a
completion's `parent_event_id` points to its normalized call. Subagent
lifecycle uses `subagent_start` and `subagent_end`.

Host payload values are retained verbatim, but each large value is serialized
only once. Multi-part messages distribute their source parts across normalized
events instead of copying the complete source record into every part. Normalized
aliases are limited to small linkage/status fields such as `call_id`,
`success`, and `exit_code`; text, input, and output stay under their original
host keys. `retro.utils.event_text`, `event_tool_input`, and
`event_tool_output` provide shape-independent access. Duplicate host
representations use a small metadata pointer to the canonical normalized event;
the complete duplicate remains available through `raw_ref`.

When a raw record lacks a timestamp, the nearest preceding timestamp is copied
and `payload.timestamp_inferred` is `true`. A record before the first timestamp
remains untimestamped. No following timestamp is backfilled.

### Codex mapping

| Raw record | Normalized type | Notes |
| --- | --- | --- |
| `session_meta` | `session_start` | Detects `parent_thread_id`, `agent_path`, or subagent source metadata and attributes that session's agent events to `subagent`. |
| `turn_context` | `metadata` | Turn model, cwd, policy, and instruction snapshot. |
| `world_state` | `metadata` | Preserves the complete world-state payload. |
| `compacted` | `compaction` | `lifecycle=compaction_complete`. |
| `inter_agent_communication_metadata` | `metadata` | Actor is `subagent`. |
| `event_msg.user_message` | `message` | Actor is `user`, or `subagent` in a child session. |
| `event_msg.agent_message` | `message` | Actor is `assistant` or `subagent`. Duplicate host representations become filtered `metadata`. |
| `event_msg.agent_reasoning` | `reasoning` | Source `text` is retained without a second text alias. |
| `event_msg.task_started`, `task_complete` | `lifecycle` | `lifecycle=task_started|task_complete`. |
| `event_msg.token_count` | `usage` | Original `info` and `rate_limits` shapes are unchanged. |
| `event_msg.patch_apply_end`, `exec_command_end`, `mcp_tool_call_end`, `web_search_end` | `tool_result` | Normalizes call ID, output, success/error, and exit code when present. |
| `event_msg.item_completed` | `lifecycle` | Host item acknowledgement; filtered from transcripts by default. |
| `event_msg.thread_settings_applied` | `metadata` | Preserves the complete settings snapshot. |
| `event_msg.context_compacted` | `compaction` | `lifecycle=compaction_complete`. |
| `event_msg.sub_agent_activity` | `subagent_start`, `subagent_end`, or `lifecycle` | `started`/terminal kinds use the existing subagent types; other activity uses `lifecycle`. |
| `event_msg.turn_aborted`, `thread_rolled_back` | `lifecycle` | Interruption/rollback reason remains in the payload. |
| `response_item.message` | `message`, `attachment`, or `metadata` | Developer content is injected `attachment`; user/assistant content is `message`; duplicate host copies are compact metadata pointers. |
| `response_item.reasoning` | `reasoning` | Keeps plaintext summary/content and encrypted envelope. |
| `response_item.function_call`, `custom_tool_call` | specialized call or `tool_call` | `exec_command`/`shell` → `command`; `apply_patch` → `file_edit`; read tools → `file_read`. |
| `response_item.function_call_output`, `custom_tool_call_output` | `tool_result` | Linked to the call by `call_id` and `parent_event_id`. |
| `response_item.web_search_call`, `tool_search_call` | `tool_call` | Uses the source ID only when the host omitted a call ID. |
| `response_item.tool_search_output` | `tool_result` | Linked to `tool_search_call`. |
| `response_item.agent_message` | `message` | Actor follows the source `author` path (`/root` vs child agent). |
| Any unlisted top-level or payload kind | `unknown` | Complete source payload is retained and reported as a capture gap. |

### Claude Code mapping

| Raw record | Normalized type | Notes |
| --- | --- | --- |
| Synthetic first/last boundaries | `session_start`, `session_end` | Boundaries reference the first/last transcript line and carry cwd. |
| `user` string/text | `message` | `isMeta=true` is injected `attachment`; `isSidechain=true` is attributed to `subagent`. |
| `user` `tool_result` block | `tool_result` | Always a result regardless of call specialization; includes `toolUseResult`, normalized status, call ID, and exact call parent. |
| `user` image block | `attachment` | Image envelope is retained. |
| `assistant` text | `message` | Sidechain output is attributed to `subagent`. |
| `assistant` thinking | `reasoning` | Sidechain reasoning is attributed to `subagent`. |
| `assistant` `tool_use` | specialized call or `tool_call` | Read/Edit/Write/Notebook/Bash tools use `file_read`/`file_edit`/`command`. |
| `attachment` | `attachment` | Includes reminders, deferred tools, environment/instructions, skills, queued commands, dates, model/session context, and other injected context. |
| `permission-mode` | `permission` | Preserves the permission mode record. |
| `mode` | `lifecycle` | `lifecycle=mode_change`. |
| `queue-operation` | `lifecycle` | `lifecycle=queue_enqueue|queue_dequeue|queue_remove`. |
| `cost-state` | `usage` | Preserves cost, duration, line, and per-model token fields. |
| `last-prompt`, `ai-title`, `file-history-snapshot`, `file-history-delta`, `pr-link` | `metadata` | Cached prompt/title/file/PR state, not injected content. |
| `bridge-session`, `atis-latch`, `agent-name`, `worktree-state`, `frame-link` | `metadata` | Actor is `subagent` for `agent-name`, otherwise `system`. |
| `system.local_command` | `command` | Preserves slash-command markup. |
| `system.turn_duration` | `lifecycle` | `lifecycle=turn_end`. |
| `system.stop_hook_summary` | `lifecycle` | Actor `hook`, `lifecycle=hook_end`. |
| `system.scheduled_task_fire` | `lifecycle` | Scheduled wake-up transition. |
| `system.away_summary` | `attachment` | Recap injected when resuming. |
| `system.informational` | `message` | User-visible host notice. |
| `system.api_error` | `error` | Preserves retry and structured connection fields. |
| Any unlisted record or content-block kind | `unknown` | Complete source record is retained and reported. |

All currently observed Claude attachment subtypes remain `attachment`:
`total_tokens_reminder`, `task_reminder`, `edited_text_file`,
`deferred_tools_record`, `queued_command`, `skill_listing`,
`deferred_tools_delta`, `environment`, `agent_listing_delta`,
`mcp_instructions_delta`, `date_change`, `silent_turn_reminder`,
`command_permissions`, `date`, `todo_reminder`, `prompt_snapshot`,
`auto_mode`, `plan_mode`, `plan_mode_exit`, `model`, `session_context`,
`instructions`, `credential_org`, `remote_session_change`, and
`hook_system_message`.

### Copilot CLI / VS Code Agent Host mapping

| Raw record | Normalized type | Notes |
| --- | --- | --- |
| `session.start`, `session.shutdown` | `session_start`, `session_end` | Shutdown payload retains aggregate usage and code-change metrics. |
| `session.resume` | `lifecycle` | `lifecycle=session_resume`. |
| `user.message` | `message` | No source means user-authored. `agent-*` is actor `subagent`; `skill-*`, system content, and autopilot continuations are injected `attachment`; empty system steering is `lifecycle`. |
| `assistant.message` | `message`, `reasoning`, and calls | Chunk streams are independently reassembled. Empty encrypted messages remain `attachment`; empty non-content envelopes are `metadata`. |
| `assistant.turn_start`, `assistant.turn_end` | `lifecycle` | Actor follows `agentId`/parent tool attribution. |
| `tool.execution_start` | specialized call or `tool_call` | Call payload includes arguments and stable call ID. Subagent tool invocation remains a call because separate subagent lifecycle records exist. |
| `tool.execution_complete` | `tool_result` | Parent is the exact call; normalizes success/error and `shellExecution.exitCode`. |
| `external_tool.requested`, `external_tool.completed` | call / `tool_result` | Request ID is resolved to the source tool call ID on both sides. |
| `permission.requested`, `permission.completed`, `session.permissions_changed` | `permission` | Completion links to the corresponding tool call when available. |
| `hook.start`, `hook.end` | `lifecycle` | `lifecycle=hook_start|hook_end`; end links to start by invocation ID. |
| `subagent.started`, `subagent.completed`, `subagent.failed` | `subagent_start`, `subagent_end` | Carries call ID, normalized success/error, model, duration, and token totals. |
| `subagent.configured`, `subagent.selected`, `subagent.deselected` | `metadata` | Actor is `subagent`; parent follows `agentId` when available. |
| `session.workspace_file_changed` | `file_edit` | Host-observed workspace mutation. |
| `session.mode_changed`, `session.task_complete` | `lifecycle` | `lifecycle=mode_change|task_complete`. |
| `session.model_change`, `session.auto_mode_resolved`, `session.mode_notice_delivered` | `metadata` | Model/routing/mode state without conversational content. |
| `session.usage_checkpoint` | `usage` | Payload shape remains unchanged for token/cost consumers. |
| `session.compaction_start`, `session.compaction_complete` | `compaction` | Complete summary and token accounting are retained. |
| `session.binary_asset` | `attachment` | Binary data is retained; renderers bound displayed JSON. |
| `skill.invoked` | `attachment` | Full delivered skill content is retained. |
| `skill.context_delivered_ref` | `attachment` | Reference/prefix/suffix describe injected context. |
| `skill.invoked_ref` | `metadata` | Invocation metadata without content. |
| `system.message` | `message` | System-visible conversation content. |
| `system.notification` | `metadata` | Agent-idle and other host notifications are filtered by default. |
| `session.error`, `abort` | `error` | Structured error/reason remains in payload. |
| Any unlisted Agent Host kind | `unknown` | Complete envelope is retained and reported. |

### VS Code Copilot chat mapping

Direct transcript records use the same mappings for `session.start`,
`user.message`, assistant turns/messages, and tool execution. Unlike Agent
Host logs, `run_subagent`/`task` transcript calls use `subagent_start` and
`subagent_end` because no separate lifecycle records exist. Every completion
is linked directly to its call.

Core snapshot response parts map as follows:

| Core response `kind` | Normalized type |
| --- | --- |
| absent string value | `message` |
| `thinking` | `reasoning` |
| `toolInvocationSerialized` | specialized call plus `tool_result` when complete |
| `textEditGroup` | `file_edit` |
| `codeblockUri`, `inlineReference` | `attachment` |
| `confirmation` | `permission` |
| `prepareToolInvocation` | `metadata` |
| `disabledClaudeHooks`, `mcpServersStarting`, `questionCarousel` | `metadata` |
| `undoStop` | `lifecycle` |
| `progressMessage`, `progressTaskSerialized` | `message` |
| Any other kind | `unknown` |

### Rendering and archived renormalization

Rendered transcripts omit `lifecycle`, `metadata`, usage checkpoints, and
legacy no-op attachment markers by default. `retro render --include-noise`
shows them. Tool results render immediately beneath their call, first by
`parent_event_id` and then by call ID for legacy events.

`retro import renormalize` reads only `raw/`, holds the archive lock, and
stages/replaces normalized JSONL and rendered Markdown as one per-session
transaction. `--host`, `--session`, `--since`, `--dry-run`, and `--no-render`
bound the operation. `normalized/normalizer-index.json` records the per-host
normalizer version, per-session raw stat fingerprint, renderer version,
counts, size, and completion time. A session is skipped only when its raw
fingerprint and both requested derived versions are current. Failures leave
the previous normalized/rendered pair intact and are reported without stopping
other sessions.

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

- The same normalized event schema supports Codex, Claude Code, and VS Code Copilot.
- Host-specific raw fields are preserved.
- Host-specific adapters are isolated behind a common importer interface.

## Open Questions

- How complete is Codex rollout JSONL compared with the live desktop conversation?
- Does Codex expose reliable hooks in CLI and Desktop, or should v0 rely on rollout files and SQLite only?
- Which Claude Code sidecar files are necessary to reconstruct file edits beyond what the transcript stores?
- Which VS Code/Copilot persistence fields will remain stable across future storage migrations?
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
3. Build `vscode_copilot_importer`:
   - reconstruct legacy JSON and current JSONL mutation logs,
   - capture Copilot transcripts and editing/resource sidecars,
   - normalize direct event transcripts with core-state fallback.
4. Define normalized event schema.
5. Write raw + normalized + markdown artifacts to `rollout-memory/`.
6. Build a manual miner command:
   - input: normalized event JSONL,
   - output: session lessons JSON + markdown plus scope-indexed repo/user memory records.
7. Add hook integration:
   - Claude `SessionEnd` triggers import + mining.
   - Codex hook integration only after confirming available hook behavior.
