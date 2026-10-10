# Rollout Dashboard Spec (v2)

## Purpose

A static, offline dashboard that answers the questions a heavy agent user actually has:

- What have my agents been doing lately, and is it getting better or worse?
- Which projects consume my time and money, and where is the friction?
- Which session was that, and what exactly happened in it?
- How do agents, clients, models and work kinds compare on a given signal?
- Can I trust the data — what was excluded, what failed to capture, when did sync last run?

The dashboard is a pure view layer. It owns no extraction logic: it reads the session
catalog and the signal store from disk and renders them.

## Inputs (read-only, on disk)

| Path | Contract | Used for |
| --- | --- | --- |
| `catalog/sessions.jsonl` | catalog spec | every session row, projects, tags, exclusion, fingerprints |
| `signals/definitions.json` | signals spec | signal metadata (category, kind, unit, polarity, categories) |
| `signals/sessions.jsonl` | signals spec | per-session values, evidence event ids, confidence |
| `sync/runs/*.json` | `retro.sync.SyncReport` | capture health, failures, last run |
| `rendered/<host>/<id>.md` | renderer | existence check and outbound link (never copied) |

The builder never reads `normalized/` or `raw/`. If the catalog or signal store is missing,
the build still succeeds and the dashboard shows an empty state plus a capture-health banner
naming the command to run.

Readers are defensive: unknown fields are ignored, wrong types are coerced or dropped, and
unusable records are counted and surfaced on the Data quality page.

## Information architecture

| Route | Page | Content |
| --- | --- | --- |
| `#/overview` | Overview | KPI strip with trend vs the previous period, sessions and cost per day, outcome and work-kind mix, agent and client tables, notable project movers, momentum (streak, active days), recent sessions |
| `#/projects` | Projects | consolidated projects (one row per `project.id`): sessions, active time, cost, success rate, tool error rate, last activity, per-agent chips; sortable |
| `#/project/<id>` | Project detail | KPIs, per agent/client breakdown, outcome and work mix, activity chart, recent sessions |
| `#/sessions` | Sessions explorer | virtualized list over every session with filters (text, agent, client, project, tag, outcome, work kind, model, interactive/automated, date range), sorting, include-excluded toggle, subagent roll-up toggle |
| `#/session/<key>` | Session detail | header (project, client, models, branch, duration, cost), tags with `tag_sources`, inline markers, signals grouped by category with polarity coloring and evidence ids, parent/subagent links, artifact links into the archive |
| `#/signals` | Signals | every signal from `definitions.json` with its population summary, grouped by category |
| `#/signal/<id>` | Signal detail | distribution (histogram or category mix) and comparison by agent/client/project/model/work kind with a minimum-sample guard, plus the highest-value sessions |
| `#/quality` | Data quality and capture | included/excluded counts, exclusion reasons by tag and rule with examples, interactive vs automated share, unknown-event rate per host, missing and stale signals, catalog and signal-store paths, latest sync status and failures |

All state (range, filters, sort, comparison dimension) lives in the URL hash, so every view is
a shareable deep link.

## Output format

```text
<dashboard_dir>/
  index.html -> current/index.html      # < 300 KB shell, classic <script> tags only
  assets     -> current/assets          # app.css, core.js, views.js, app.js
  data       -> current/data            # shards
  current    -> .retro-builds/<generation>
  .retro-builds/<generation>/…          # at most 2 kept
  .retro-cache/detail/…                 # per-bucket detail cache + index.json
```

Every shard is a JavaScript file that calls `RETRO_DATA("<name>", <json>)` on a global defined
by an inline bootstrap in `index.html`. This works under `file://`, where `fetch` and ES modules
are blocked, and under `http://` without changes. `<`, `>` and `&` are escaped so a shard can
never terminate its own `<script>` element.

| Shard | Name | Contents |
| --- | --- | --- |
| `data/manifest.js` | `manifest` | schema, dictionaries, column names, flag bits, shard index, signal definitions/encodings/summaries, quality block, sync block, warnings |
| `data/sessions-NNN.js` | `sessions:N` | 2,000 fact rows each, newest first |
| `data/signals-NNN.js` | `signals:N` | sparse `[rowIndex, value, …]` columns per signal, aligned with the session shards |
| `data/detail/d-NNNN.js` | `detail:N` | full per-session payloads for one bucket |
| `data/build.js` | `build` | the build report (timings, sizes, counts) |

### Fact table

One row per session: a positional array whose column order is published in `manifest.columns`.

```text
key, title, prompt, project, agent, client, started, duration, active, cost, tokens,
turns, tools, edits, events, unknown, outcome, work, flags, tags, models, corrections,
errors, bucket, parent
```

Strings are interned into `manifest.dict` (agents, clients, projects, models, tags, outcomes,
work kinds); money is integer micro-USD; ratios are integer per-mille; `flags` is a bitfield
documented in `manifest.flags` (`excluded`, `interactive`, `automated`, `subagent`, `child`,
`parent`, `committed`, `validated`, `secrets`, `signals`, `transcript`, `worktree`).

The browser treats this table as a fact table and performs all slicing, grouping and trend
maths locally, so any filter combination is instant and no aggregation has to be guessed at
build time. Shards load progressively; the UI reports indexing progress and re-renders as they
arrive.

### Session detail buckets

Sessions are assigned to `crc32(key) % bucket_count` buckets, where `bucket_count` is the power
of two nearest `sessions / 64` (max 4096). The bucket index is stored in the row, so the client
never recomputes the hash. A bucket is re-rendered only when its member set or any member
fingerprint changes; otherwise it is hard-linked out of `.retro-cache/detail`. Power-of-two
bucket counts keep assignments stable as sessions are added.

## Non-goals

- No build toolchain, bundler, package manager or CDN. The assets are hand-written and shipped
  as package data.
- No cost or token extraction. `usage.*` values come from the signal store.
- No transcript copies. The dashboard links to `rendered/` inside the archive.
- No server. `file://` is a first-class target.

## Performance budget

| Budget | Target | Measured |
| --- | --- | --- |
| `index.html` | < 300 KB | 1.9 KB |
| total output | < 100 MB | 5.5 MB (2,000 real-derived sessions), 73 MB (35,000 synthetic) |
| build from catalog + signals | < 60 s | 0.2 s and 3.3 s cold; 0.1 s and 2.4 s incremental |
| generations kept | ≤ 2 | 2; older ones removed on publish |
| transcripts copied | none | none |

## Accessibility and UX

Semantic landmarks, a skip link, `aria-current` navigation, sortable headers exposed as
column-header buttons with `aria-sort`, focus-visible outlines, an `aria-live` load status,
keyboard shortcuts (`/` search, `g` then `o p s i q`, arrows and Enter in the session list,
`t` theme, `?` help), light/dark/auto themes and a responsive grid.

## Entry points

```python
from retro.dashboard import build_and_publish, build_dashboard, run_terminal_dashboard

build_and_publish(layout, output_dir, mode="auto")               # -> Path to index.html
build_dashboard(layout, output_dir, mode="auto", publish=True)   # -> BuildReport
run_terminal_dashboard(layout, interactive=None)                 # rich terminal view
```

`mode` is `auto` (reuse unchanged detail buckets) or `full` (rebuild everything). Legacy cost
modes (`calculate`, `display`) are accepted and mapped to `auto`.

```bash
retro dashboard build [--root ARCHIVE] [--out DIR] [--mode auto|full] [--no-publish]
retro dashboard view  [--root ARCHIVE] [--limit N] [--interactive/--no-interactive]
```
