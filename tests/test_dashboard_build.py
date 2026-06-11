"""End-to-end tests for the packaged dashboard builder."""

from __future__ import annotations

import json
from pathlib import Path

from retro.dashboard_build import PricingMap, build


def test_build_empty_artifact_root(tmp_path: Path):
    artifact_root = tmp_path / "rollout-memory"
    artifact_root.mkdir()
    out_dir = tmp_path / "dashboard"

    index_path = build(artifact_root=artifact_root, out_dir=out_dir)

    assert index_path == out_dir / "index.html"
    assert index_path.exists()
    payload = json.loads((out_dir / "data" / "rollouts.json").read_text(encoding="utf-8"))
    assert payload["sessions"] == []
    assert payload["cost_mode"] == "auto"


def test_build_rejects_unknown_mode(tmp_path: Path):
    try:
        build(mode="bogus", artifact_root=tmp_path, out_dir=tmp_path / "out")
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown mode")


def test_pricing_snapshot_ships_with_package():
    pricing = PricingMap.load()
    # The bundled LiteLLM snapshot must resolve rates without DEFAULT_RATES.
    rates = pricing.rates_for("gpt-5")
    assert rates["input"] > 0
    assert rates["output"] > 0
