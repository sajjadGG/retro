# dashboard/

This folder holds dashboard-adjacent tooling that is not part of the installed package.

The dashboard itself lives in `src/retro/dashboard/`:

```bash
retro dashboard build     # build and publish
retro dashboard view      # terminal view
```

See `docs/dashboard.md` for usage and `specs/rollout_dashboard_spec.md` for the design.

## pricing/

`pricing/refresh.py` refreshes the curated LiteLLM rate snapshot at
`src/retro/pricing/litellm-pricing.json`, which the signal pipeline uses to estimate
`usage.cost_usd`. The dashboard only displays that signal.

```bash
python dashboard/pricing/refresh.py
```

The published dashboard generations are written to the configured dashboard directory
(`retro config` → `dashboard-dir`), not here.
