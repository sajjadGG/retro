# Memory Backend

The memory backend is a SQLite index over flat-file memory sources.

## Design

Canonical files:

```text
rollout-memory/memories/items.jsonl
rollout-memory/memories/events.jsonl
```

Derived index:

```text
rollout-memory/memories/index.sqlite
```

Deleting `index.sqlite` is safe. Rebuild it with:

```bash
retro memory reindex
```

## What Gets Indexed

- Records from `memories/items.jsonl`.
- Existing mined candidates from `rollout-memory/mined/<method>/<host>/<session>.json`.
- Authored markdown imported through `retro memory import-authored`.
- Lifecycle/utility events from `memories/events.jsonl`.

## Retrieval

Retrieval currently uses:

- FTS5 keyword recall;
- Reciprocal Rank Fusion scoring for recalled lists;
- repo/scope filtering;
- one-hop `[[wiki-link]]` expansion;
- q-value, quality, recency, scope, and risk reranking.

Embeddings are intentionally optional and not required for the core path.

## Wiki Links

Memory text can link related memories:

```text
Use [[pytest-policy]] when changing retrieval.
```

If `pytest-policy` exists as a memory id, retrieval can pull it into the linked cluster. Dangling links are reported by:

```bash
retro memory doctor
```

## Utility Updates

```bash
retro memory update-utility --memory-id <id> --reward 0.8 --session-id <session-id>
```

The update rule is:

```text
q_new = q_old + 0.2 * (reward - q_old)
```

Events are appended first, then folded into SQLite. Reindexing replays the events.

## Safety

Accepted memories are downgraded to `needs_review` when text contains obvious prompt-injection markers, credential-looking strings, or invisible control characters.

## Commands

```bash
retro memory init
retro memory reindex
retro memory doctor
retro memory import-authored <dir>
retro memory retrieve --query "..." --cwd /repo
retro memory weave --query "..." --cwd /repo
retro memory update-utility --memory-id <id> --reward 0.8 --session-id <session-id>
```
