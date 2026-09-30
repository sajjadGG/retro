# Dashboard

The dashboard is a static local HTML report.

Build it from the repo root:

```bash
retro dashboard build
```

Open:

```text
dashboard/index.html
```

## Panels

- KPI strip for sessions, events, tool calls, edits, tokens, and estimated cost.
- Activity-by-day bars.
- Signal aggregates.
- Searchable session table.
- Per-session drill-down with Summary, Models, Signals, Transcript, and Memory tabs.
- Indexed memory summary when `retro memory reindex` has been run.

Portfolio totals include every normalized session. The interactive detail
payload is bounded to the 1,000 newest sessions, and the memory browser keeps
the 2,000 highest-ranked candidates while still reporting full aggregate
counts. This prevents multi-year or evaluation-heavy archives from producing
multi-gigabyte HTML files.

Rendered transcript Markdown remains in the archive. The dashboard links the
archive's `rendered/` tree and loads a selected transcript on demand instead of
embedding every transcript in `index.html` and `rollouts.json`.

Periodic sync publishes dashboard generations atomically and retains the
current and previous generation for rollback.

## Cost Modes

```bash
retro dashboard build --mode auto
retro dashboard build --mode calculate
retro dashboard build --mode display
```

- `auto`: use embedded cost when present; otherwise calculate from token counts.
- `calculate`: always calculate from token counts.
- `display`: only show embedded provider cost.

## Pricing Snapshot

Rates come from:

```text
dashboard/pricing/litellm-pricing.json
```

Refresh the curated snapshot with:

```bash
python dashboard/pricing/refresh.py
```

## Terminal Dashboard

```bash
retro dashboard view
```

This renders an interactive terminal dashboard for quick local inspection.
