# Repo review — 2026-06-11

Scope: goal/direction, implementation, pipeline/CI, docs. Findings ordered by severity.
Items marked **[fixed]** were addressed in the commit that introduced this file.

## What the project is

Capture Codex / Claude Code rollouts into durable local artifacts, evaluate them with
signals, mine them into prompt-time memory, index that memory in SQLite, and report via
a static dashboard. Local-first, evidence-linked, spec-driven (`specs/`).

## Direction

**Strengths**

- The core loop (capture → normalize → signals → mine → memory → weave) is a real,
  differentiated idea — the README's own framing against ccusage is honest and correct.
- Spec-first discipline is unusual and good: every major surface has a written spec.
- Sound architectural instincts: immutable `raw/`, every derived stage rebuildable from
  disk, evidence refs (`event_id`s) carried through signals and mined memories, capture
  gaps surfaced instead of hidden.

**Concerns**

1. **Scope creep is diluting the core.** Operator Quests/XP/streak-freeze gamification,
   an Operator Diagnostics panel, a 1,200-line terminal dashboard, and an orphaned
   trajectory-experiments builder all landed while the actual differentiator — memory
   that provably helps the next session — is still unproven. The utility loop exists
   (`memories/events.jsonl`, `update-utility`, value-aware reranking) but nothing closes
   it: no flow measures whether woven memories changed outcomes. Recommendation: treat
   quests/diagnostics/terminal-dashboard as `experimental/`, and spend the next effort on
   the retrieval→outcome feedback loop.
2. **Mining quality is weak and the README shows it.** The deterministic methods are
   keyword/heuristic approximations of the papers they cite; the README's own
   `skill_pro` example has `Activation: gi ahead and fix the issues found here` and
   `Verification: (no explicit verification observed)`. That is honest, but it means the
   paste-ready blocks are mostly noise today. The LLM-backed path (`codex_headless`) is
   where extraction quality will come from; consider making signal-gated LLM mining
   (mine only sessions whose signals say they're worth mining) the headline flow.
3. **Naming split-brain.** The distribution was renamed and published to PyPI as
   `retro-ai` (commit 22c1726; confirmed live on PyPI), but README, AGENTS.md, and
   docs/onboarding.md still say `pip install retro-agent-memory`, which 404s on PyPI.
   Stale `retro_agent_memory` wheels sit in `dist/`. **[left as-is per owner decision —
   naming is intentionally not touched in this pass]**

## Implementation

4. **`retro dashboard build` is broken for every PyPI user.** `cli.py` resolved the
   builder as `Path(__file__).parents[2]/dashboard/build_dashboard.py`, which only
   exists in a source checkout — `dashboard/` is not in the wheel. An advertised
   top-level command fails for anyone who installed from PyPI.
   **[fixed — the builder now ships inside the package as `retro.dashboard_build`
   (pricing snapshot bundled as package data), the CLI imports it directly instead of
   shelling out, and `dashboard/build_dashboard.py` is a back-compat shim; verified
   end-to-end from a wheel install in a clean venv]**
5. **The dashboard ignored `--root`.** Every pipeline command takes `--root` for the
   artifact store except the dashboard, which hardcoded `<repo>/rollout-memory`.
   **[fixed — `--artifact-root` / `RETRO_ARTIFACT_ROOT` on the builder, `--root`
   passthrough on `retro dashboard build`]**
6. **`build_dashboard.py` is a 2,581-line monolith with the least scrutiny in the
   repo.** HTML/CSS/JS generated inside a Python f-string (`{{` escaping throughout),
   exempt from E501, exempt from mypy, zero tests, not built in CI. The most complex
   file has the weakest safety net. **[partially fixed — CI smoke-builds both dashboard
   pages on every run, and `tests/test_dashboard_build.py` exercises `build()`
   end-to-end including the bundled pricing snapshot]** Longer term: extract the
   template to a `.html` file with a placeholder-substitution step, and split data
   collection (testable, pure) from rendering.
7. **Stale claim in the builder docstring**: "intentionally separate from the CLI
   package" — it imports `retro.analyzer`, so it requires the package on `sys.path`.
   **[fixed — docstring updated]**
8. **Repo-wide ruff fails (17 × E501) in `dashboard/build_trajectory_experiments.py`**,
   an orphaned script referenced by nothing (no CLI command, no docs). CI passed anyway
   because it lints only `src/retro tests`. **[fixed — per-file ignore added, CI lints
   the whole repo, and the script is no longer orphaned: it moved into the package as
   `retro.dashboard_experiments`, wired up as `retro dashboard experiments`]**
9. **mypy config debt**: an unused `dashboard` override written as a comma-joined string
   (wrong form — mypy wants a list), flagged "unused section" on every run
   **[fixed — removed]**; blanket `disable_error_code = ["arg-type"]` for `retro.cli`
   and `retro.importers.*` masks real type errors. **[fixed — exemptions removed and
   all 66 surfaced errors resolved with real types: `Host`/`EventType` literals on the
   importer maps and CLI host helpers, typed kwargs dicts, narrowed optionals]**
10. **HTML injection in the dashboard.** The committed session table interpolated
    transcript-derived titles into `innerHTML` unescaped; titles come from user prompts,
    so a captured rollout containing markup executes when the dashboard opens. The
    uncommitted working-tree changes already add `escapeHtml` for titles/projects;
    `filters_applied` was still raw. **[fixed — escaped]** Ironic gap given the memory
    backend explicitly sanitizes prompt-injection markers — the same distrust of
    transcript content should apply to the dashboard.

## Pipeline / CI

11. CI breadth is good (3.10–3.13 matrix, compileall, CLI smoke, pytest, build, twine
    check, trusted publishing). Gaps: lint covered only `src/retro tests`
    **[fixed]**; the dashboard builder was never executed **[fixed — smoke build
    step]**; mypy only on 3.13 (fine, deliberate).
12. **Uncommitted work is sitting on `main`'s working tree** (dashboard insights panel +
    risk/memory filters + escaping, and a pricing refresh that changed
    `snapshot_kind` from `curated` to `litellm-upstream`). The dashboard changes look
    finished and good — they should be committed. Left untouched here; review and
    commit them.
13. The pricing refresh rewrites decimals to scientific notation (`1.25e-06`), making
    every future refresh diff noisy. **[fixed — `refresh.py` serializes fixed-point and
    the bundled snapshot was normalized]**

## Docs

14. README "Project layout" omitted half the package (`analyzer.py`, `quest.py`,
    `llm.py`, `trajectory.py`, `dashboard_terminal.py`, `memory_store.py`,
    `signals/trajectory.py`, `importers/base.py`, `build_trajectory_experiments.py`).
    **[fixed]**
15. Install/release sections point at the `retro-agent-memory` name, which is not the
    name in `pyproject.toml`. **[left as-is per owner decision — see item 3]**

## Test suite

99 tests, fast (16 s), good coverage of importers, signals, mining, memory store, quest
logic. Untested: the entire dashboard builder (see 6), `retro dashboard build`
subprocess path, `llm.py` subprocess handling. The fixtures-based importer tests are the
right pattern to extend.

## Recommended next steps (not done in this pass)

- ~~Ship the dashboard builder inside the package~~ **done** (`retro.dashboard_build`).
- Split `build_dashboard.py`: pure data-collection module (unit-testable, mypy-checked)
  + template file.
- Close the memory utility loop: a `retro memory weave` → session → signal →
  `update-utility` round-trip, so reranking learns from real outcomes.
- ~~Decide the fate of `build_trajectory_experiments.py`~~ **done** (wired in as
  `retro dashboard experiments`).
- ~~Burn down the `arg-type` mypy exemptions~~ **done** (zero mypy overrides for
  application code remain; only the two HTML-template modules are exempt).
- Move quests / operator diagnostics / terminal dashboard behind an "experimental"
  boundary in code layout (docs now label them experimental).
