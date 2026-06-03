# Memory Storage & Retrieval Backend Spec

Status: partially implemented
Author: drafted with Claude Code. The Hermes row in the comparative section is
web-verified (sources linked inline); the remaining systems are from model
knowledge (Jan 2026 cutoff) plus the repo's own `agent_rollout_memory_landscape.md`
and are not all individually re-fetched.

## Purpose

`self_evolving_memory_generation_spec.md` defines *what* a memory is (procedure,
skill, case, failure_trigger, …), its lifecycle (build → retrieve → evaluate →
update → prune → export), and a flat-file store (`memories/items.jsonl` plus
`memories/events.jsonl`). This spec supplies the derived SQLite query engine for
that source of truth.

This spec fills that gap. It answers the open question directly: **is the
flat-file design scalable, or do we need a database — and if so, which one?**
It then specifies the storage + retrieval backend, the bootstrap/rebuild path,
and the migration from `items.jsonl`.

This is infrastructure under the existing spec, not a replacement. The memory
*model*, mining methods, utility update rules, and skill evaluation stay exactly
as written there; this spec changes only how those records are stored, indexed,
linked, and queried.

## Scope

In scope:
- Scalability assessment of the current/proposed flat-file design.
- The storage-engine decision (flat files vs SQLite vs Postgres+pgvector).
- Physical schema (tables, indexes, FTS, vectors, link graph, lifecycle log).
- The retrieval pipeline that realizes the two-phase recall + value-rerank from
  the self-evolving spec.
- Bootstrap / reindex / migration commands.
- Embedding + extension availability fallbacks (grounded in verified env probes).

Out of scope (owned by other specs):
- Memory/skill *content* generation and prompts → `self_evolving_memory_generation_spec.md`.
- Trajectory signals that feed `reward`/`q` → `experimental_trajectory_signals_spec.md`.
- Rollout capture/normalization → `full_rollout_capture_feature_spec.md`.

## Scalability Assessment of the Current Design

The current and proposed storage is filesystem-native: rollouts under
`rollout-memory/{raw,normalized,rendered,mined}/...` (see `src/retro/storage.py`),
and the self-evolving spec adds `memories/items.jsonl` + hand-built JSON indexes.
The hand-saved memory format (markdown + YAML frontmatter, one fact per file,
`[[wiki-links]]`) is the human-authored counterpart.

What this is good at, and should be kept:
- **Git-diffable, human-editable, provenance-friendly.** Every memory is a file
  a person can read, edit, and review in a PR. This is a real product
  differentiator (the landscape doc's "auditable memories with source links").
- **Zero operational burden.** No server, no daemon, no migration tooling.
- **Schema-flexible.** Adding a field to `MemoryCandidate.structured` doesn't
  require an ALTER.

Where it stops scaling — and why this matters before, not after, growth:
1. **Retrieval is O(n) file IO per query.** Recall in the self-evolving spec
   (BM25-ish overlap, scope/kind filters, utility rerank) means reading and
   scanning every record on every `retro memory retrieve`. At today's 27
   sessions this is nothing; at a few thousand mined memories across many repos
   it becomes seconds-per-query and forces a full re-embed/re-scan to rank.
2. **No real query primitives.** Ranking by `score = semantic*0.45 + q*0.35 +
   quality*0.10 + recency*0.05 + scope*0.05 - risk` (the spec's value-rerank)
   over JSONL means re-implementing joins, filters, and top-k in Python every
   call. `indexes/by_*.json` are manual denormalizations that must be rebuilt
   transactionally by hand and drift the moment a write is interrupted.
3. **Concurrent/append correctness.** `items.jsonl` appended from the SessionEnd
   reflection hook, from `retro mine`, and from a future live retrieval-utility
   update is a multi-writer scenario. JSONL append + separate index rewrite has
   no atomic "update this record and its index" — partial writes corrupt the
   index.
4. **The wiki-link graph has no traversal primitive.** `[[links]]` across
   hundreds of files can only be resolved by parsing every file.

Conclusion: the flat layout is the right **source of truth** and the wrong
**query engine**. Keep the files; add an index.

## How Self-Improving Agents Handle This (synthesis)

Grounded in this repo's `agent_rollout_memory_landscape.md` and the lineage in
`self_evolving_memory_generation_spec.md` (MemP, MemRL, SkillRL, SkillNet,
MemGen), plus general model knowledge. The **Hermes** row is web-verified (see
sources below); the rest are not all individually re-fetched.

| System | Storage backend | Lookup | Extraction / consolidation | Skills |
| --- | --- | --- | --- | --- |
| **Hermes (Nous Research)** — the closest analog | **SQLite + FTS5, WAL, single writer; raw transcripts in JSONL files; no server.** Prompt memory in `MEMORY.md`/`USER.md` (hard char caps). | FTS5 keyword `session_search`, results LLM-summarized (Gemini Flash) before injection; prompt memory injected as a frozen block at session start. **No vectors in core.** | periodic self-"nudges" + capacity-threshold (>80%) consolidation: merge/compress/dedup; security scan (prompt-injection/credential/invisible-unicode) before accept | **`~/.hermes/skills/*.md`** (agentskills.io / SKILL.md), **progressive disclosure** (summary by default, full on demand → flat token cost); created on ≥5 tool calls / error recovery / user correction / non-obvious success |
| Generative Agents (Stanford) | in-memory "memory stream" (list of records) | score = recency + importance + relevance (embedding cosine) | periodic LLM "reflection" synthesizes higher-level memories | — |
| MemGPT / Letta | **SQLite/Postgres** + vector store; tiered (core vs archival) | self-issued function calls to page memory in/out | LLM self-edits core memory; archival via embeddings | — |
| Voyager | flat files of **code** (skill = function) | embedding over skill descriptions | adds a skill when a task succeeds; skills compose | skill library is the whole point |
| Reflexion | short verbal memory buffer | last-N reflections in context | LLM writes a self-critique after failure | — |
| A-MEM | vector DB + **Zettelkasten link graph** | semantic recall, then follow links | new note triggers update of linked notes | — |
| Mem0 / Zep (Graphiti) | **Postgres/Neo4j + pgvector**, often hybrid | hybrid vector + keyword + graph | LLM extract → dedup/merge → temporal invalidation | — |
| Claude memory tool / Claude Code auto-memory | **flat markdown files** per project | model reads the index, then opens files | model writes one fact per file; manual dedup | via SKILL.md files |
| codex-mem / total-agent-memory (landscape doc) | **SQLite**-backed MCP | progressive / hybrid retrieval | session-end summarize, procedural workflows | procedural memory tools |

The consensus that is actually relevant to a single-user local CLI:
- Production memory layers converge on **hybrid retrieval** (keyword + vector,
  fused), not pure-vector — keyword catches exact identifiers (filenames,
  commands, error strings) that embeddings blur. This repo's data is *full of*
  such identifiers, so hybrid is not optional.
- The ones at our scale and constraints (local, single-user, file-first:
  Claude Code auto-memory, codex-mem) land on exactly **markdown source of truth
  + SQLite index**. Heavyweight stacks (Postgres/Neo4j/pgvector) are a function
  of multi-tenant scale we do not have.
- Linking (A-MEM Zettelkasten) ≈ our `[[wiki-links]]`; it gives graph-style
  recall *without* a graph DB if links are a join table.

**Independent validation by Hermes.** The most directly comparable system — a
production self-improving local agent — converged on this spec's core choices
without sharing them: SQLite+FTS5 in WAL mode as the index, JSONL transcripts as
source of truth, no server, file-based markdown skills, and **no vectors in the
core path** (keyword + LLM summarization only). That last point is the strongest
signal: it confirms the embedding tier here should stay *optional*, not required.

**Concrete patterns to adopt from Hermes** (additive to the parent spec, cheap to
implement):
- **Progressive disclosure for skills.** Store a short summary per skill loaded by
  default; fetch full `SKILL.md` body only on demand. Keeps injected-context cost
  flat as the skill count grows. Maps onto the parent spec's `memory weave` step.
- **Skill-creation trigger heuristics:** generate a skill when a session had ≥5
  tool calls, an error→recovery sequence, a user correction, or a non-obvious
  success. Useful default gates for the SessionEnd reflection hook and `retro mine`.
- **Capacity-threshold consolidation:** when a scope's memory set grows past a
  budget, run an LLM merge/compress/dedup pass rather than appending forever.
- **Security scan before `accepted`:** reject prompt-injection strings, credential
  exfiltration, and invisible-unicode in memory text — broaden the parent spec's
  secret-exposure guard to cover these at promotion time.

Sources (web-verified, June 2026): Hermes memory architecture —
https://www.glukhov.org/ai-systems/hermes/hermes-agent-memory-system/ and
https://mranand.substack.com/p/inside-hermes-agent-how-a-self-improving

## Decision: SQLite as a Rebuildable Index over File Source-of-Truth

| Option | Verdict | Why |
| --- | --- | --- |
| Pure flat files (status quo) | Keep as **source of truth**, reject as query engine | O(n) IO, no joins/top-k, fragile manual indexes (see assessment). |
| **SQLite index over files** | **Adopt** | Single file, zero server, `sqlite3` in stdlib, FTS5 + sqlite-vec for hybrid search, atomic transactions, link graph as a join. Rebuildable from files, so it's a cache not a liability. |
| Postgres + pgvector | Reject for v1 | Adds a server + ops to a single-user local CLI for zero benefit at hundreds–thousands of rows. Revisit only if this becomes multi-user/hosted. |

This confirms the "SQLite is the best approach" instinct. The non-obvious
commitment that makes it safe: **SQLite is never the source of truth.** Memory
records live as files (JSONL records and/or per-memory markdown); the database
is a derived, `DROP`-and-rebuild index. Consequences:
- A corrupt/locked/stale DB is never data loss — `retro memory reindex` rebuilds it.
- Schema changes are "bump version, rebuild," not migrations on precious data.
- Git still reviews/owns the memories; the `.sqlite` file is gitignored.

### Source of truth, precisely
- **Mined memories:** `rollout-memory/memories/items.jsonl` remains the canonical
  append log (as the self-evolving spec defines), plus `events.jsonl` lifecycle
  log. The `indexes/by_*.json` files in that spec are **replaced** by the DB.
- **Hand-authored memories:** the existing markdown+frontmatter files (e.g. the
  user's `~/.claude/.../memory/*.md`) are imported as first-class rows with
  `source = "authored"`, so retrieval ranks authored and mined memory together.

## Physical Schema

One database file: `rollout-memory/memories/index.sqlite` (gitignored). DDL:

```sql
PRAGMA journal_mode = WAL;          -- safe concurrent reads during writes
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;            -- bump to force a rebuild on schema change

-- Canonical memory records (mirror of MemoryCandidate + lifecycle state).
CREATE TABLE memory (
  id            TEXT PRIMARY KEY,         -- session:method:index, or authored slug
  source        TEXT NOT NULL,            -- 'mined' | 'authored'
  kind          TEXT NOT NULL,            -- MemoryKind
  scope         TEXT NOT NULL,            -- user|repo|task|global
  status        TEXT NOT NULL DEFAULT 'candidate', -- candidate|accepted|needs_review|deprecated
  text          TEXT NOT NULL,
  when_to_use   TEXT DEFAULT '',
  origin_repo   TEXT,                     -- cwd/repo key for repo-scope filtering
  confidence    REAL DEFAULT 0.5,
  priority      INTEGER DEFAULT 3,
  risk          TEXT DEFAULT 'medium',
  -- utility (MemRL) and quality (SkillNet) kept as JSON for schema flexibility
  q_value       REAL DEFAULT 0.5,
  hits          INTEGER DEFAULT 0,
  successes     INTEGER DEFAULT 0,
  failures      INTEGER DEFAULT 0,
  quality_avg   REAL DEFAULT 0.0,
  structured    TEXT,                     -- full structured payload as JSON
  content_hash  TEXT NOT NULL,            -- for dedup (hash of normalized text+scope+kind)
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  last_used_at  TEXT
);
CREATE INDEX idx_memory_scope_repo ON memory(scope, origin_repo, status);
CREATE INDEX idx_memory_kind       ON memory(kind, status);
CREATE UNIQUE INDEX idx_memory_hash ON memory(content_hash);

-- Full-text keyword search (BM25). External-content table backed by `memory`.
CREATE VIRTUAL TABLE memory_fts USING fts5(
  text, when_to_use, content='memory', content_rowid='rowid'
);

-- Provenance: which rollout events back this memory (evidence_refs).
CREATE TABLE memory_evidence (
  memory_id  TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  ref        TEXT NOT NULL                          -- event_id / raw_ref
);
CREATE INDEX idx_evidence_mem ON memory_evidence(memory_id);

-- [[wiki-link]] graph as an edge table → A-MEM-style traversal via JOIN.
CREATE TABLE memory_link (
  src_id  TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  dst_slug TEXT NOT NULL,        -- target [[slug]]; may not resolve yet (dangling ok)
  PRIMARY KEY (src_id, dst_slug)
);
CREATE INDEX idx_link_dst ON memory_link(dst_slug);

-- Lifecycle / utility-update log (mirror of memories/events.jsonl).
CREATE TABLE memory_event (
  memory_id  TEXT NOT NULL,
  event      TEXT NOT NULL,      -- memory_used|memory_rewarded|memory_revised|memory_deprecated
  session_id TEXT,
  reward     REAL,
  reason     TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_event_mem ON memory_event(memory_id, created_at);

-- Embeddings: see "Embeddings & Fallbacks". Either sqlite-vec virtual table
-- or this plain table scanned in Python.
CREATE TABLE memory_vec (
  memory_id  TEXT PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,      -- float32 little-endian
  model      TEXT NOT NULL       -- embedding model id, for invalidation
);
```

## Retrieval Pipeline

Implements the two-phase recall + value rerank from
`self_evolving_memory_generation_spec.md` § Retrieval, now as real queries.

**Phase A — Recall (cheap candidate generation, union of):**
1. Scope/repo prefilter: `scope='global'` OR (`scope='repo'` AND `origin_repo=:repo`)
   OR `scope='user'` OR (`scope='task'` AND kind/task match).
2. Keyword: `memory_fts MATCH :query` ranked by `bm25(memory_fts)`.
3. Semantic: top-k by cosine over `memory_vec` (sqlite-vec KNN, or Python brute
   force — see fallbacks).
4. Graph expansion: pull `memory_link` neighbors of the top recall hits
   (one hop) so a retrieved note drags in its linked cluster (A-MEM / SkillNet
   "load a small cluster, not one isolated note").

Fuse the keyword and semantic lists with **Reciprocal Rank Fusion**
(`1/(k+rank)`, k=60) → a single recall set. RRF avoids having to calibrate BM25
and cosine onto the same scale.

**Phase B — Value rerank (exact formula from the parent spec):**
```
score = semantic_score * 0.45
      + q_value        * 0.35
      + quality_avg    * 0.10
      + recency_score  * 0.05
      + scope_match    * 0.05
      - risk_penalty
```
`q_value`, `quality_avg`, `status`, `last_used_at` are columns → this is a single
`ORDER BY` expression, not Python post-processing. `risk_penalty` from
`risk`/secret-exposure/staleness flags.

**Trigger / abstain** (MemGen-inspired) stays in application code: return
`none|ask|inject` based on top score and conflict detection.

CLI (unchanged surface from the parent spec, now backed by the DB):
```bash
retro memory retrieve --query "..." --cwd /repo      # ranked list
retro memory weave    --query "..." --cwd /repo      # compressed context block
retro memory update-utility --memory-id ... --reward 0.8 --session-id ...
```

## Bootstrap, Reindex & "Startup"

The DB is derived, so startup is "ensure it exists and is current," never a
manual setup step.

```bash
retro memory init       # create rollout-memory/memories/ + empty index.sqlite (idempotent)
retro memory reindex    # DROP + rebuild index.sqlite from all source files
retro memory import-authored <dir>   # ingest hand-written markdown+frontmatter memories
retro memory doctor     # report: counts by kind/scope/status, dangling links, stale embeddings,
                        #          whether sqlite-vec loaded, embedding model drift
```

Rebuild algorithm (`reindex`), the canonical "start up everything" path:
1. If `PRAGMA user_version` < code's `SCHEMA_VERSION`, drop and recreate tables.
2. Stream `memories/items.jsonl`; upsert each record by `content_hash` (dedup).
3. Ingest authored markdown files (frontmatter → columns, body → `text`,
   `[[slug]]` → `memory_link`).
4. Replay `memories/events.jsonl` into `memory_event`, fold into
   `hits/successes/failures/q_value/last_used_at`.
5. Populate `memory_fts` from `memory`.
6. Embeddings: for any row whose `(text, model)` lacks a current `memory_vec`,
   queue for (re)embedding; embedding is incremental and resumable.

Any write path (`retro mine`, the SessionEnd reflection hook, utility updates)
does an **append to the file source of truth first, then an upsert into the DB in
the same call**, inside one SQLite transaction. If the process dies mid-write,
`reindex` reconciles from the files.

## Embeddings & Fallbacks (env-verified)

Verified in this repo's `.venv` (Python 3.13, SQLite 3.51.2): **FTS5 available,
loadable extensions ENABLED (sqlite-vec works), JSON1 available, numpy NOT
installed, ~27 sessions today.** Design to that reality and degrade gracefully on
machines where it differs.

- **Keyword tier always works** (FTS5 is compiled in; pure stdlib). The system is
  fully functional on keyword + value-rerank alone — embeddings are an
  enhancement, not a hard dependency. This keeps `retro` installable with zero
  native deps.
- **Vector tier, preferred:** load `sqlite-vec` and use a `vec0` virtual table
  for KNN. Gate on a runtime probe (`enable_load_extension` + load succeeds);
  `retro memory doctor` reports the result.
- **Vector tier, fallback (no sqlite-vec, or extension load disabled on some
  host):** store float32 in the plain `memory_vec` BLOB and brute-force cosine in
  Python. At a few thousand rows this is milliseconds. Prefer numpy if present;
  ship a pure-Python `array`/`struct` path because **numpy is currently absent**
  and must not become a hard requirement.
- **Embedding provider:** pluggable and optional. Default to a local/cheap model;
  allow "no embeddings" mode. Store `model` per vector so a model change is
  detected and re-embedded by `reindex`. No FAISS/LangChain (matches the parent
  spec's non-goals).

## Migration from `items.jsonl` + JSON Indexes

1. Keep `items.jsonl` / `events.jsonl` exactly as the parent spec defines.
2. Delete the `indexes/by_repo.json` / `by_user.json` / `by_kind.json` /
   `by_skill.json` plan — the DB supersedes them. (They were a manual index; the
   DB is the index.)
3. First `retro memory reindex` builds the DB from existing files; no data
   migration, because files stay the source of truth.
4. Update `self_evolving_memory_generation_spec.md` § Storage to reference this
   spec for the index layer and drop the `indexes/*.json` artifacts.

## Implementation Plan

Phase 1 — **implemented** in `src/retro/memory_store.py`: schema bootstrap, `init`, upsert-by-hash,
file-then-DB write helper, `reindex`. Tests on a temp `rollout-memory/`.

Phase 2 — **implemented**: populate `memory_fts`, Phase-A recall queries, RRF fusion,
`retro memory retrieve` (keyword-only first; correct without embeddings).

Phase 3 — **implemented**: `memory_link`, one-hop expansion,
`import-authored`, dangling-link reporting in `doctor`.

Phase 4 — **future**: embeddings: provider abstraction, `memory_vec`, sqlite-vec path +
pure-Python brute-force fallback, incremental re-embed in `reindex`.

Phase 5 — **partially implemented**: value rerank, `update-utility`, lifecycle event replay,
and security scan before `accepted`. The remaining work is the optional vector score and deeper
reward wiring from `experimental_trajectory_signals_spec.md`.

Phase 6 — **partially implemented**: dashboard counts by kind/status/scope, top utility,
lifecycle counts, and `retro memory weave`. Remaining work is exported-skill progressive
disclosure.

## Acceptance Criteria

- `retro memory init` then `retro memory reindex` builds `index.sqlite` from
  existing `items.jsonl` with zero data loss; deleting the `.sqlite` and
  re-running reproduces identical query results (DB is purely derived).
- `retro memory retrieve --query ... --cwd ...` returns ranked memories using
  FTS5 BM25 **with the embedding tier disabled** (keyword-only must work).
- With sqlite-vec available, the same query fuses keyword + vector via RRF; with
  it unavailable, the Python brute-force path returns the same shape and
  `doctor` reports the degraded mode.
- `memory_link` resolves `[[wiki-links]]`; a retrieved memory pulls in its one-hop
  linked cluster.
- Value rerank uses the parent spec's weighted score; `update-utility` changes
  `q_value` per `q_new = q_old + 0.2*(reward - q_old)` and is reflected on next
  retrieve.
- No secret-flagged memory is promoted to `accepted` (unchanged guard from parent
  spec; enforced at write time, not just at mine time).
- Installs and runs with **no native dependencies** (numpy, sqlite-vec both
  optional); `doctor` truthfully reports which optional tiers are active.

## Non-Goals

- No Postgres/pgvector/Neo4j/server in v1 (revisit only on multi-user/hosted).
- SQLite is never source of truth — no "DB-only" memories.
- No FAISS/LangChain/vLLM; no hard numpy requirement.
- Does not change memory *content* generation, prompts, mining methods, or skill
  evaluation — those remain owned by `self_evolving_memory_generation_spec.md`.
- No automatic injection into live Codex/Claude sessions until retrieval risk
  controls (parent spec) exist.
```
