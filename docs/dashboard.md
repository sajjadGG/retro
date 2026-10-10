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
| Sessions | Everything captured, filterable by text, agent, client, project, tag, outcome, work kind, model, interactive/automated and date; sortable, virtualized, with a toggle for excluded sessions and for rolling subagents up under their parent |
| Session | One session: header, tags and why they were applied, inline markers, every signal grouped by category with evidence event ids, subagent tree, and links to the transcript, normalized events and raw capture in the archive |
| Signals | Every signal in `signals/definitions.json` with its distribution, and comparisons by agent, client, project, model or work kind with a minimum-sample guard |
| Data quality | Why sessions were excluded (by tag and rule, with examples), interactive vs automated share, unknown-event rate per host, missing or stale signals, and the latest sync run with its failures |

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

Each build stages into `<dashboard>/.retro-builds/.staging-*`, then renames the directory into
place and swaps the `current` symlink, so a reader never sees a half-written dashboard. The
top-level `index.html`, `assets` and `data` entries are symlinks into `current`. At most two
generations are kept; staging directories are removed on failure, and stale ones left by a
killed process are reaped on the next build.

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
