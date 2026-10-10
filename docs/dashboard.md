# Dashboard

A static, offline dashboard over the session catalog and the signal store.

```bash
retro dashboard build          # build and publish a new generation
open ~/…/dashboard/index.html  # or: python3 -m http.server, then browse
```

`retro dashboard build` prints where it wrote, how many sessions it indexed, how many shards it
reused, the output size and the build time.

## What it shows

| Page | Answers |
| --- | --- |
| Overview | What happened recently across all agents and projects, and how it compares with the previous period: sessions, active hours, estimated cost, outcome mix, work-kind mix, notable movers, streak |
| Projects | Which projects cost time and money; one row per consolidated project, with the per-agent and per-client split, success rate, tool error rate and last activity |
| Sessions | Everything captured, filterable by text, agent, client, project, tag, outcome, work kind, model, interactive/automated and date; sortable, virtualized, with a toggle for excluded sessions and for rolling subagents up under their parent. Facet lists show only values present in the current scope with their counts; project and model use a type-ahead so an archive with 30,000 scratch projects stays usable |
| Session | One session: header, tags and why they were applied, inline markers, every signal grouped by category with evidence event ids, subagent tree, and links to the transcript, normalized events and raw capture in the archive |
| Signals | Every signal in `signals/definitions.json` with its distribution, and comparisons by agent, client, project, model or work kind with a minimum-sample guard |
| Data quality | Why sessions were excluded (by tag and rule, with examples), cost coverage, interactive vs automated share, unknown-event rate per host, missing or stale signals, and the latest sync run stage by stage |

## Cost is an estimate, and sometimes there is none

Cost comes from the `usage.cost_usd` signal. When a session has no captured usage, or uses models
with no known rate, the dashboard shows `—` with a tooltip explaining why (from the
`quality.cost_basis` signal) instead of `$0.00`. Totals say how many sessions are unpriced and
averages only count priced ones.

## First load stays small

Most archives are dominated by excluded sessions. The fact rows for included sessions load at
boot; excluded rows, their dictionary entries and their catalog-only detail load only when you
turn on "include excluded" or open an excluded session. On the reference archive (31,901
sessions, 732 included) the first load is about 465 KB and the whole directory is 28 MB.

## Where the data comes from

The build reads only derived files:

- `catalog/sessions.jsonl` — sessions, projects, tags, exclusion, fingerprints (`retro catalog update`)
- `signals/definitions.json` + `signals/sessions.jsonl` — signal metadata and values (`retro signal update`)
- `sync/runs/*.json` — the latest sync reports
- `rendered/<host>/<id>.md` — checked for existence and linked, never copied

If the catalog or signal store is missing, the dashboard still builds and tells you which
command to run in its capture-health banner.

## Options

```bash
retro dashboard build --root /path/to/rollout-memory --out /path/to/dashboard
retro dashboard build --mode full     # rebuild every session-detail shard
retro dashboard build --no-publish    # write straight into --out, no generation swap
```

`--mode auto` (the default) reuses the detail shards whose sessions have not changed, keyed by
the catalog fingerprint.

## How publication works

Each build stages into `<dashboard>/.retro-builds/.staging-*`. When the generation is complete it
is renamed into place and the `current` symlink is replaced — a single atomic rename, and the
only pointer that moves. `index.html`, `assets` and `data` are permanent symlinks that resolve
through `current`, so a reader can never mix two generations and a failed build leaves the
previous one serving untouched. At most two generations are kept; staging directories are removed
on failure, and stale ones left by a killed process are reaped on the next build.

## Sync status

The dashboard is built by the dashboard stage of `retro sync`, so the run report it reads is still
open and this generation stays published until the next rebuild. The banner reports that run in
the past tense — "last sync Oct 9, 2026, 7:52 PM (22m ago): capture ✓ catalog ✓ render ✓ signals ✓
memory ✓" — and notes that the dashboard was built during it. Only genuinely failed or partial
stages raise a warning banner; the Data quality page shows every stage with its duration.

Session-detail shards are cached in `<dashboard>/.retro-cache/detail` and hard-linked into each
generation, so unchanged sessions cost no space and no time.

## Offline by construction

`index.html` is a small shell (a couple of kilobytes) that loads hand-written CSS and JS from
`assets/` and data from `data/*.js`. There is no bundler, no package manager and no network
access: shards are classic scripts that register JSON on a global, which is the only mechanism
that works when the page is opened as a `file://` URL.

## Keyboard

| Key | Action |
| --- | --- |
| `/` | focus the session search |
| `g` then `o` `p` `s` `i` `q` | overview, projects, sessions, signals, data quality |
| `↑` `↓` `Enter` | move and open rows in the sessions list |
| `t` | cycle light / dark / auto |
| `?` | keyboard help |

## Terminal view

```bash
retro dashboard view                 # interactive when attached to a TTY
retro dashboard view --no-interactive
```

Commands: `o` overview, `p` projects, `s` sessions, `d <n>` detail, `f <text>` find,
`r <7d|30d|90d|365d|all>` range, `x` toggle excluded sessions, `q` quit.

## Pricing

Cost is read from the `usage.cost_usd` signal; the dashboard does not price tokens itself. The
vendored rate snapshot lives at `src/retro/pricing/litellm-pricing.json` and is refreshed with
`python dashboard/pricing/refresh.py`.
