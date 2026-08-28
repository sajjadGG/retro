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


def test_build_includes_copilot_agent_host_usage(
    copilot_cli_imported,
    tmp_path: Path,
):
    layout, session_id = copilot_cli_imported
    out_dir = tmp_path / "dashboard"

    index_path = build(artifact_root=layout.root, out_dir=out_dir)

    payload = json.loads((out_dir / "data" / "rollouts.json").read_text(encoding="utf-8"))
    assert payload["summary"]["by_host"] == {"vscode-copilot": 1}
    assert payload["summary"]["by_source"] == {"copilot-cli": 1}
    session = payload["sessions"][0]
    assert session["session_id"] == session_id
    assert session["source_kind"] == "copilot-cli"
    assert session["active"] is True
    assert session["tokens"]["input_tokens"] == 430
    assert session["tokens"]["output_tokens"] == 150
    assert session["tokens"]["cached_input_tokens"] == 1000
    assert session["tokens"]["cache_creation_tokens"] == 70
    assert session["tokens"]["reasoning_output_tokens"] == 35
    assert session["tokens"]["total_tokens"] == 1650
    assert session["tokens"]["copilot_credits"] == 1.5
    assert set(session["tokens_by_model"]) == {
        "gpt-5.6-sol",
        "claude-sonnet-5",
    }
    assert session["estimated_cost_usd"] > 0
    html = index_path.read_text(encoding="utf-8")
    assert "active snapshot" in html
    assert "copilot-cli" in html
