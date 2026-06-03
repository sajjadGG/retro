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
