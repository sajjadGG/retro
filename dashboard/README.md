# Rollout Dashboard

Static dashboard for browsing captured rollout sessions and their metrics.

Build it from the repo root:

```bash
.venv/bin/python dashboard/build_dashboard.py
```

Then open:

```text
dashboard/index.html
```

The dashboard is deliberately separate from the `retro` package. It reads the artifacts under `rollout-memory/` and writes a self-contained HTML file plus `dashboard/data/rollouts.json`.

