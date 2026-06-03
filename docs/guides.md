# Guides

## Capture Before Logs Expire

Claude Code transcripts can age out. Run:

```bash
retro list --host claude
retro import claude --all
```

If `retro list` warns that logs are near the retention window, import them before continuing other work.

## Rebuild Everything From Disk

```bash
retro signal run
retro mine '*' '*' --method all --filter risk_aware
retro memory reindex
retro dashboard build
```

Every stage is designed to be re-run independently.

## Import Hand-Written Memories

Create markdown files:

```markdown
---
id: pytest-policy
kind: tool_lesson
scope: global
status: accepted
risk: low
when_to_use: Use when editing tests.
---
Run pytest after changing retrieval. Link to [[debugging-policy]] when relevant.
```

Then import:

```bash
retro memory import-authored ~/notes/retro-memory
retro memory reindex
```

## Use Memory In A New Session

```bash
retro memory weave --query "fix a failing pytest around storage" --cwd "$PWD"
```

Paste the resulting block into the next agent session when it is relevant.

## Troubleshoot Empty Retrieval

1. Check that memories exist:

   ```bash
   retro memory doctor
   ```

2. Rebuild the index:

   ```bash
   retro memory reindex
   ```

3. Use a query with concrete terms from the memory text, such as file names, commands, error strings, or task names.

4. If you only want promoted memories, use `--accepted-only`; otherwise candidates are included by default while the promotion workflow matures.

## Update Memory Utility

When a memory helped:

```bash
retro memory update-utility --memory-id <id> --reward 1.0 --session-id <session-id>
```

When it hurt or was irrelevant:

```bash
retro memory update-utility --memory-id <id> --reward 0.0 --session-id <session-id>
```

Utility writes append to `rollout-memory/memories/events.jsonl` and are replayed on reindex.
