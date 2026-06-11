# Onboarding

This page gets a new user from zero to a browsable local portfolio and memory index.

## 1. Install

From PyPI:

```bash
pip install retro-agent-memory
retro --help
```

From a clone:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/retro --help
```

## 2. Discover Sessions

```bash
retro list
retro list --host claude
retro list --host codex
```

`retro` discovers Claude Code logs under `~/.claude/projects/` and `~/.config/claude/projects/`, and Codex sessions under `~/.codex`.

Use env overrides for archives or alternate roots:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude,/backup/claude" retro list --host claude
CODEX_HOME="$HOME/.codex,/backup/codex" retro list --host codex
```

## 3. Import Rollouts

```bash
retro import claude --latest
retro import codex --latest
retro import all --limit-per-host 20
```

Imported files land under `rollout-memory/`. Raw captures are immutable unless you pass `--force`.

## 4. Run Signals

```bash
retro signal list
retro signal run
retro signal show codex <thread-id>
```

Signals produce evidence-linked readings under `rollout-memory/signals/`.

## 5. Mine Prompt-Time Memory

```bash
retro methods
retro mine codex <thread-id> --method all --filter risk_aware
retro mine '*' '*' --method all --filter risk_aware
```

Mining writes structured candidates and prompt blocks under `rollout-memory/mined/`.

## 6. Build The Memory Index

```bash
retro memory init
retro memory reindex
retro memory doctor
```

The SQLite index is derived from flat files and mined artifacts. It can be deleted and rebuilt without data loss.

## 7. Retrieve And Weave Memory

```bash
retro memory retrieve --query "pytest retrieval" --cwd /path/to/repo
retro memory weave --query "pytest retrieval" --cwd /path/to/repo
retro memory update-utility --memory-id <id> --reward 0.8 --session-id <session-id>
```

`retrieve` returns ranked rows. `weave` emits a compact markdown block for prompt-time use.

## 8. Build The Dashboard

```bash
retro dashboard build
```

Open `dashboard/index.html` from disk. The dashboard reads rollouts, signals, mined memory, and the SQLite memory index.

## 9. Verify Your Setup

From a clone:

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/ -q
.venv/bin/mypy src/retro/
.venv/bin/retro dashboard build
```
