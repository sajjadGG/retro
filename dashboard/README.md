# Rollout Dashboard

Static dashboard for browsing captured rollout sessions and their metrics.

Build it from the repo root:

```bash
.venv/bin/retro dashboard build
```

Then open:

```text
dashboard/index.html
```

The builder ships inside the `retro` package (`retro.dashboard_build`); `build_dashboard.py` here is a back-compat shim. It reads the artifacts under `rollout-memory/` and writes a self-contained HTML file plus `dashboard/data/rollouts.json`.

