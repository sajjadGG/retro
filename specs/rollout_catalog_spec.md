# Rollout Session Catalog

Status: implemented catalog schema 1.

## Decision

Retro maintains one derived catalog record for every archived session:

```text
raw/<host>/<id>/ + normalized/<host>/<id>.events.jsonl
    -> catalog/sessions.jsonl
```

The catalog is the authoritative input inventory for signals, mining,
dashboard aggregation, memory reindexing, and task-scorer source selection.
Those stages no longer decide independently whether a rollout is real,
interactive, a subagent, or part of the same repository.

Two files have different durability:

- `catalog/sessions.jsonl` is derived and can be deleted and rebuilt.
- `catalog/annotations.jsonl` is canonical user state and is append-only.

Raw capture and normalized events remain evidence. A catalog record stores the
winning interpretation and its evidence; it does not rewrite either source.

## Record contract

`catalog/sessions.jsonl` contains compact JSON objects sorted by
`<host>/<session_id>`. Every schema field is present. Unknown scalar values are
`null`; an unresolved project is `{id: "unknown", name: "Unknown",
source: "unknown", evidence: null}`.

The stable Python API is:

```python
from retro.catalog import Catalog, SessionRecord, load_catalog, update_catalog

catalog = load_catalog(layout)
catalog.included()
catalog.excluded()
result = update_catalog(layout, keys=None, force=False)
```

`load_catalog` never writes. `update_catalog` atomically replaces
`sessions.jsonl`; its caller must hold the archive lock. `changed_keys`
includes additions, `added_keys` is the addition subset, and `removed_keys`
contains archived keys whose raw and normalized sources both disappeared.

## Host metadata and client mapping

Classification uses archived metadata, never source application databases.
An unrecognized provider is retained with `agent=<host>`,
`client="unknown"`, and nullable interaction state.

| Archived evidence | `agent` | `client` | interactive |
| --- | --- | --- | --- |
| Codex `originator=Codex Desktop` | `codex` | `codex-desktop` | true |
| Codex `source=vscode` | `codex` | `codex-vscode` | true |
| Codex `source=exec` or `originator=codex_exec` | `codex` | `codex-exec` | false |
| Codex terminal/CLI source | `codex` | `codex-cli` | true |
| Claude `entrypoint=cli` | `claude-code` | `claude-cli` | true |
| Claude `entrypoint=claude-desktop` | `claude-code` | `claude-desktop` | true |
| Claude `entrypoint=sdk` or `sdk-cli` | `claude-code` | `claude-sdk` | false |
| Copilot `source_kind=vscode-chat` | `copilot` | `copilot-vscode-chat` | true |
| Copilot `client_name=vscode-agent-host` or `vscode` | `copilot` | `copilot-vscode-agent` | true |
| Copilot `client_name=github/cli` | `copilot` | `copilot-cli` | true unless non-interactive evidence wins |

Copilot non-interactive evidence is an archived `copilot -p`/`--prompt`
invocation, a prompt-mode `user.message` envelope (`messageId` absent,
`parentAgentTaskId` present, `delivery=idle`), or a macOS/Linux
temporary-directory cwd used by headless runs.
Subagents are always non-interactive. If neither side can be established,
`interactive` is `null`, not guessed.

Codex subagents come from `session_meta.payload.source.subagent`.
`thread_spawn.parent_thread_id` wins for parent linkage, followed by the
archived `thread.json.spawn_edges.parents`. Guardian subagents have
`is_subagent=true` and no parent when Codex did not persist one. Claude uses
`isSidechain` plus an explicit `parentSessionId`/`parent_session_id` when
present. Copilot and extension providers use explicit archived parent fields;
in-stream Copilot subagent events do not create fake top-level sessions.

Codex metadata precedence is `rollout.jsonl` `session_meta`, then
`thread.json`. Claude scans the archived transcript for `entrypoint`, `cwd`,
`gitBranch`, `isSidechain`, `aiTitle`, and model metadata, with
`import_meta.json.project_slug` as a path fallback. Copilot uses
`import_meta.json` and `sidecars/workspace.yaml`. The workspace reader supports
JSON-like/plain/single-quoted scalars and literal/folded block values without a
YAML dependency.

## Normalized-event summary

Only `actor=user,event_type=message` contributes user messages, first prompt,
or inline markers. Only `actor=assistant,event_type=message` contributes
assistant-message counts. Tool calls are assistant/subagent events typed
`tool_call`, `command`, `file_read`, `file_edit`, or `subagent_start`; tool
results do not count.

Catalog extraction accepts normalization v1 and v2. `usage`, `lifecycle`, and
`metadata` events are non-conversational. Model and usage envelopes are
recognized from payload keys independently of `event_type`, so old
`attachment`-typed token snapshots and new `usage` events behave identically.

`started_at` and `ended_at` are the minimum and maximum valid event timestamps.
`duration_s` is their non-negative difference. `active_s` walks timestamped
events in file order and sums each non-negative gap capped at 300 seconds.
With at least one timestamp, both durations can be zero; without timestamps
they are `null`. First-prompt whitespace is collapsed and capped at 2,000
characters. Titles are capped at 200.

## Inline directives

The case-insensitive directives are:

```text
#retro:ignore
#retro:keep
#retro:tag=<tag>
#retro:project=<id-or-name>
```

They are recognized only in the title and user-authored message text. A marker
must be a standalone token. Markers inside inline backticks, fenced backtick or
tilde blocks, Markdown quote lines beginning with `>`, assistant text,
reasoning, tool output, metadata, lifecycle, and usage payloads are ignored.
Tag names match `[A-Za-z0-9][A-Za-z0-9._/-]{0,63}`.

`#retro:keep` removes only automatic `ignore` sources (`rule:*` and
`dotfile:*`). It does not remove an explicit `#retro:ignore` or manual tag.
The last project directive wins below an annotation.

## `.retroignore`

Extraction checks `cwd_resolved/.retroignore`, then each ancestor, stopping at
the nearest file. Blank and comment-only files mean `ignore`. Otherwise each
valid non-comment line is a tag. The record persists tags and the absolute
`dotfile:<path>` source. If the cwd later disappears, the persisted result is
retained until source evidence supplies a new cwd.

## Rules

Built-ins are always listed and can be disabled:

| id | condition | tags |
| --- | --- | --- |
| `builtin.temp-dir` | cwd under an OS temporary directory | `scratch`, `ignore` |
| `builtin.empty` | zero normalized user messages | `empty`, `ignore` |
| `builtin.automated` | `interactive == false` | `automated` |
| `builtin.subagent` | `is_subagent == true` | `subagent` |

User rule fields are ANDed. A present non-empty list is exact,
case-insensitive any-of; an empty list is unconstrained. `cwd` entries are
path globs matched against recorded and resolved cwd, with `~`, `*`, `?`, and
`**`. `title`, `prompt`, and `session_id` use Python regular-expression search.
Counts are inclusive. `started_after` is an inclusive lower bound;
`started_before` is an exclusive upper bound. A missing session value fails a
field that requires it. Invalid fields, regexes, dates, types, tags, duplicate
ids, and alias cycles make config loading fail with the field path.

`retro rules test` evaluates a supplied rule or all rules against the existing
catalog and performs no writes.

## Tag and exclusion precedence

Sources apply from low to high:

1. rules;
2. nearest `.retroignore`;
3. title/user markers;
4. append-only annotations.

Annotation replay is chronological. `tag` removes a prior suppression;
`untag` removes every lower source and suppresses the tag until a later
manual `tag`. Tag source strings are persisted for exact explanations.

`excluded` is:

```python
bool(set(record.tags) & set(config.exclude_tags))
```

`exclude_tags` defaults to `["ignore"]`. `automated`, `subagent`, `scratch`,
and `empty` remain visible dimensions even where another tag causes exclusion.

## Git and project identity

Recorded cwd is preserved verbatim. `cwd_resolved` expands environment and
home prefixes and resolves existing symlink components, so `~/Dev` and
`/Volumes/SSD/Dev` converge. If Claude no longer has a recorded cwd, its
archived project slug supplies the best-effort resolved cwd.

When cwd exists, Retro ascends to `.git`. It reads directories and `gitdir:`
files directly, follows `commondir` for linked worktrees, reads `HEAD`, loose
or packed refs, and the first `origin`/remote URL from Git config. It never
starts a Git process and never performs network I/O. The current on-disk
remote wins for repository identity. Session-recorded branch and commit remain
the historical authority; on-disk branch/commit fill missing metadata. All Git
fields fall back to archived host metadata when the cwd is gone.

Remote normalization:

- accepts HTTPS, SSH URLs, scp-style `git@host:owner/repo`, and Copilot
  `owner/repo`;
- removes scheme, credentials, leading slash, trailing slash, and `.git`;
- lowercases the resulting `host/owner/repo`.

Project decision precedence is:

1. session annotation;
2. last project marker;
3. configured path glob;
4. normalized Git remote;
5. resolved main Git root;
6. worktree or archived Claude scratch-path inference;
7. resolved cwd path;
8. decoded Claude project slug;
9. `unknown`.

Configured aliases are applied to the winner and change `source` to `config`.
Display names do not change identity. `.worktrees/<branch>` maps to its main
repository; linked worktree `commondir` provides the canonical main root.
Archived Claude scratch paths recover the original `~/Dev/repos/<repo>` when
that repository still exists.

Every project decision stores one source enum and short evidence string.
`retro sessions explain` shows that winner rather than presenting all
candidates as equally authoritative.

## Incremental algorithm

The record fingerprint hashes:

- extractor version;
- normalized-file `(mtime_ns,size)`;
- host metadata file `(mtime_ns,size)` tuples;
- relevant rule/project/exclusion config;
- annotations for that key;
- nearest `.retroignore` stat evidence;
- resolved cwd plus stable `.git` linkage and remote-config stat evidence (not
  moving `HEAD`/ref state).

Unknown provider hosts fingerprint every archived raw file. Known hosts stat
only files their extractor reads. A no-op update stats sources and compares
fingerprints without opening normalized streams. Changed sessions are
re-extracted; deleted keys are removed; the complete sorted catalog is
atomically replaced only when output changed. Stat-preserving out-of-band
content modification is outside this explicitly stat-based invalidation
contract; `force=True` rebuilds regardless.

Failure behavior:

- malformed config or annotations fails before publication;
- malformed catalog JSON fails load rather than silently dropping a record;
- missing optional raw metadata degrades to normalized/path/unknown evidence;
- missing normalized events yields zero counts and triggers `builtin.empty`;
- unreadable Git config or disappeared cwd falls back without mutating source;
- an interrupted write leaves the previous catalog because publication uses
  same-directory temporary files and `os.replace`.

## Downstream boundary

Excluded records do not enter mining, memory reindexing, signals, or task
scorer selection. `retro mine --include-excluded` is the explicit diagnostic
override. Task-scorer selection emits `EXCLUDED_BY_CATALOG` with detail
`excluded by catalog` and tag evidence. Memory reindex ignores mined results
whose top-level host/session points to an excluded record.

GhostLab's import-time filter is intentionally separate. New GhostLab
evaluation sessions carrying `session_meta.originator=ghostlab` or the
reserved Copilot UUID prefix are rejected before raw capture, avoiding archive
growth. Catalog rules handle already-archived traffic and non-GhostLab test,
mimic, prompt-mode, temporary, or user-tagged sessions. The pre-capture filter
does not replace catalog evidence, and rules do not mutate or delete raw data.
