#!/usr/bin/env python3
"""Refresh the vendored LiteLLM pricing snapshot used by the dashboard.

LiteLLM's `model_prices_and_context_window.json` is the canonical model
pricing source. We don't ship the whole file (~megabytes of models we will
never see); instead we keep a curated snapshot at
`dashboard/pricing/litellm-pricing.json` that contains only the models we
expect to encounter from Claude Code and Codex.

Usage:
    python dashboard/pricing/refresh.py

The script:
  1. Reads the list of models to keep from the existing snapshot (top-level
     keys minus `_meta`). Add a model by adding an empty entry to the JSON.
  2. Downloads the LiteLLM upstream JSON.
  3. Replaces each known entry's pricing fields with the upstream values when
     present, leaving unknown models alone with a "no upstream pricing" note.
  4. Writes the snapshot back, updating `_meta.snapshot_taken`.

The downloaded file is never written to disk; only the curated subset is
persisted.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
SNAPSHOT_PATH = Path(__file__).resolve().parent / "litellm-pricing.json"

PRICING_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "input_cost_per_token_above_200k_tokens",
    "output_cost_per_token_above_200k_tokens",
    "litellm_provider",
)


def main() -> int:
    if not SNAPSHOT_PATH.exists():
        print(f"snapshot not found: {SNAPSHOT_PATH}", file=sys.stderr)
        return 1
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    meta = snapshot.pop("_meta", {})
    targets = list(snapshot.keys())
    print(f"refreshing {len(targets)} models from {UPSTREAM_URL}")
    try:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=20) as resp:
            upstream = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network or parse failure
        print(f"failed to fetch upstream: {exc}", file=sys.stderr)
        return 2

    updated = 0
    missing: list[str] = []
    for model in targets:
        src = upstream.get(model)
        if not isinstance(src, dict):
            missing.append(model)
            continue
        for field in PRICING_FIELDS:
            value = src.get(field)
            if value is not None:
                snapshot[model][field] = value
        updated += 1

    meta["snapshot_taken"] = dt.date.today().isoformat()
    meta["snapshot_kind"] = "litellm-upstream"
    meta["models_updated"] = updated
    meta["models_missing_upstream"] = missing

    out = {"_meta": meta, **snapshot}
    SNAPSHOT_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"updated {updated}/{len(targets)} models")
    if missing:
        print(f"  no upstream entry for: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
