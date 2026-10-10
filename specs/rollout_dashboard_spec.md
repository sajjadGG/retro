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
  index.html -> current/index.html      # permanent symlink; < 300 KB shell
  assets     -> current/assets          # permanent symlink; app.css, core.js, views.js, app.js
  data       -> current/data            # permanent symlink; shards
  current    -> .retro-builds/<generation>   # the only pointer a publish moves
  .retro-builds/<generation>/…          # at most 2 kept
  .retro-cache/detail/…                 # per-bucket caches + index.json
  .retro-cache/detail-x/…
```

`index.html`, `assets` and `data` are created once and resolve through `current`, so publishing a
generation is a single atomic rename of `current` after the generation is fully written: a reader
can never observe a mix of two generations, and a failure before the swap leaves the previous
generation serving untouched.

Every shard is a JavaScript file that calls `RETRO_DATA("<name>", <json>)` on a global defined
by an inline bootstrap in `index.html`. This works under `file://`, where `fetch` and ES modules
are blocked, and under `http://` without changes. `<`, `>` and `&` are escaped so a shard can
never terminate its own `<script>` element.

| Shard | Name | Loaded | Contents |
| --- | --- | --- | --- |
| `data/manifest.js` | `manifest` | boot | schema, dictionaries (included prefix), column names, flag bits, shard index, cost-basis names, signal definitions/encodings/summaries, quality block, sync block, warnings |
| `data/sessions-NNN.js` | `sessions:N` | boot | fact rows for **included** sessions, 2,000 per shard, newest first |
| `data/signals-NNN.js` | `signals:N` | signals page | sparse `[rowIndex, value, …]` columns per signal, aligned with the included rows |
| `data/detail/d-NNNN.js` | `detail:N` | on demand | full per-session payloads for one bucket of included sessions |
| `data/excluded-NNN.js` | `excluded:N` | on demand | fact rows for **excluded** sessions, 4,000 per shard |
| `data/dict-tail.js` | `dict-tail` | on demand | dictionary entries only excluded rows reference |
| `data/detail-x/x-NNNN.js` | `detailx:N` | on demand | catalog-only payloads for excluded sessions (the browser merges the fact row in) |
| `data/build.js` | `build` | boot | the build report (timings, sizes, counts) |

An archive is dominated by excluded sessions (31,169 of 31,901 on the reference
archive), so the default first load carries only the included set: the shell,
assets, manifest and included rows total ~465 KB. The excluded set — rows,
dictionary tail and minimal detail — is fetched only when the user turns on
"include excluded" or opens an excluded session.

### Fact table

One row per session: a positional array whose column order is published in `manifest.columns`.

```text
key, title, prompt, project, agent, client, started, duration, active, cost, tokens,
turns, tools, edits, events, unknown, outcome, work, flags, tags, models, corrections,
errors, basis, bucket, parent
```

Rows are emitted included-first, so every dictionary index, fact row and signal-matrix position
an included session uses lives in the prefix the browser loads at boot. Strings are interned into
`manifest.dict` (agents, clients, projects, models, tags, outcomes, work kinds, cost bases);
money is integer micro-USD and **null when the session has no cost estimate** — never zero;
`basis` points at `manifest.cost_basis` (the `quality.cost_basis` signal) and explains why;
ratios are integer per-mille; `flags` is a bitfield
documented in `manifest.flags` (`excluded`, `interactive`, `automated`, `subagent`, `child`,
`parent`, `committed`, `validated`, `secrets`, `signals`, `transcript`, `worktree`).

The browser treats this table as a fact table and performs all slicing, grouping and trend
maths locally, so any filter combination is instant and no aggregation has to be guessed at
build time. Shards load progressively; the UI reports indexing progress and re-renders as they
arrive.

### Session detail buckets

Included sessions are assigned to `crc32(key) % bucket_count` buckets, where `bucket_count` is
the power of two nearest `sessions / 64` (max 4096); excluded sessions use their own bucket space
sized for 320 sessions per bucket and carry only what the fact row lacks (cwd, counts, tag
provenance, markers). The bucket index is stored in the row, so the client
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
| total output | < 30 MB | 27.8 MB on the real archive (31,901 sessions, 732 included) |
| first load | small | 465 KB (shell + assets + manifest + included rows) |
| build from catalog + signals | < 60 s | 1.9 s cold, 1.7 s incremental on the real archive |
| generations kept | ≤ 2 | 2; older ones removed on publish |
| transcripts copied | none | none |

## Cost semantics

`usage.cost_usd` is often absent: a session may have no captured usage at all, or use models with
no rate. The dashboard never renders that as `$0.00`. A missing estimate shows `—` with a tooltip
naming the reason from `quality.cost_basis` (`exact`, `family`, `mixed`, `embedded`, `unpriced`,
`none`, or `unscored` when the session has no signal record), totals are qualified with the number
of unpriced sessions, and averages divide by priced sessions only.

## Sync health

The dashboard stage runs *inside* the sync, so the run report it reads is normally still
`status: "running"`, and the generation it produces stays published until the next rebuild. The
banner therefore always reports a past run — "last sync `<absolute time>` (`<age>`): capture ✓
catalog ✓ …" — plus a note that the dashboard was built during that sync. Stages that had not
finished when the build read the report are listed as "pending at build" on the Data quality
page, never as failures. Stage statuses come from the run report and from `state/sync.json`,
whichever describes the newer run. Counts such as `unchanged` are accepted as a list of keys
(old reports) or an integer (current reports).

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
