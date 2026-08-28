# Onboarding

This page gets a new user from zero to a browsable local portfolio and memory index.

## 1. Install

From PyPI:

```bash
python3 -m pip install --upgrade retro-ai
retro --help
```

`retro-ai` is the PyPI distribution name. Installation creates the `retro`
console command. For a persistent per-user command instead of a project virtual
environment:

```bash
python3 -m pip install --user --upgrade retro-ai
```

If pip reports that its scripts directory is not on `PATH`, add that directory
to your shell startup file. On macOS with the system Python 3.9 it is commonly:

```bash
export PATH="$HOME/Library/Python/3.9/bin:$PATH"
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
retro list --host copilot
```

`retro` discovers Claude Code logs under `~/.claude/projects/` and
`~/.config/claude/projects/`, Codex sessions under `~/.codex`, VS Code chat
history, and Copilot CLI / Agent Host sessions under
`~/.copilot/session-state`.

Use env overrides for archives or alternate roots:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude,/backup/claude" retro list --host claude
CODEX_HOME="$HOME/.codex,/backup/codex" retro list --host codex
```

## 3. Import Rollouts

```bash
retro import claude --latest
retro import codex --latest
retro import copilot --latest
retro import all --limit-per-host 20
```

Imported files land under the configured per-user archive. Run `retro doctor`
to see the effective path. Raw captures are immutable unless a newer append-only
source is safely captured.

For periodic machine-wide capture on macOS:

```bash
retro setup \
  --archive-root "$HOME/Library/Application Support/retro/rollout-memory" \
  --dashboard-dir "$HOME/Library/Application Support/retro/dashboard" \
  --periodic 15m \
  --derived-every 6h
```

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

Run `retro config show` to see the configured dashboard path and open its
`index.html`. The dashboard reads rollouts, signals, mined memory, and the
SQLite memory index.

## 9. Verify Your Setup

From a clone:

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/ -q
.venv/bin/mypy src/retro/
.venv/bin/retro dashboard build
```
