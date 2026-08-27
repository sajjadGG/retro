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


def test_build_includes_vscode_copilot_usage(copilot_imported, tmp_path: Path):
    layout, session_id = copilot_imported
    out_dir = tmp_path / "dashboard"

    index_path = build(artifact_root=layout.root, out_dir=out_dir)

    payload = json.loads((out_dir / "data" / "rollouts.json").read_text(encoding="utf-8"))
    assert payload["summary"]["by_host"] == {"vscode-copilot": 1}
    session = payload["sessions"][0]
    assert session["session_id"] == session_id
    assert session["host"] == "vscode-copilot"
    assert session["tokens"]["input_tokens"] == 120
    assert session["tokens"]["output_tokens"] == 30
    assert session["tokens"]["total_tokens"] == 150
    assert session["tokens"]["copilot_credits"] == 1.25
    assert session["models"] == ["copilot/gpt-5.4"]
    assert session["project_name"] == "demo"
    assert session["estimated_cost_usd"] > 0
    html = index_path.read_text(encoding="utf-8")
    assert "VS Code Copilot" in html
    assert "badge.vscode-copilot" in html
