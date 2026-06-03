# Roadmap

## Implemented

- Claude Code and Codex capture.
- Normalized event schema.
- Markdown transcript rendering.
- Signals and aggregate reporting.
- Mining methods and risk-aware filtering.
- SQLite memory schema and rebuildable index.
- FTS5 keyword retrieval with scope filtering.
- `[[wiki-link]]` graph extraction and one-hop expansion.
- Authored markdown import.
- Utility writes and q-value updates.
- Prompt-time memory weave.
- Static dashboard with signals, cost, mined memory, and indexed memory sections.

## Next

- Optional embedding tier with `sqlite-vec` and pure-Python fallback.
- Deeper reward wiring from trajectory signals into memory utility.
- Promotion workflow for candidate to accepted memories.
- Export reviewed memories into Codex-compatible skills.
- Progressive disclosure for exported skills.
- More dashboard sections for deprecated memories and skills needing review.

## Non-Goals For V1

- No Postgres, pgvector, Neo4j, or server requirement.
- No FAISS, LangChain, or hard numpy dependency.
- No automatic live injection into Codex or Claude sessions before retrieval risk controls mature.
