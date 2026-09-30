"""End-to-end tests for the packaged dashboard builder."""

from __future__ import annotations

import json
from pathlib import Path

import retro.dashboard_build as dashboard_build_module
from retro.dashboard_build import PricingMap, build
from retro.dashboard_experiments import build as build_experiments
from retro.dashboard_publish import build_and_publish_dashboard
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.signals import run_signals, write_signal_artifacts
from retro.storage import Layout


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


def test_build_includes_extension_host_in_dynamic_summary(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    write_events(
        layout.normalized_path("opencode", "session-1"),
        [
            NormalizedEvent(
                event_id="message-1",
                session_id="session-1",
                host="opencode",
                sequence=1,
                actor="user",
                event_type="message",
                summary="hello",
                raw_ref=RawRef(
                    path="raw/opencode/session-1/session.jsonl",
                    line=1,
                ),
                timestamp="2026-09-29T20:00:00Z",
                payload={"text": "hello"},
            )
        ],
    )
    out_dir = tmp_path / "dashboard"

    index_path = build(artifact_root=layout.root, out_dir=out_dir)

    payload = json.loads((out_dir / "data" / "rollouts.json").read_text(encoding="utf-8"))
    assert payload["summary"]["by_host"] == {"opencode": 1}
    assert payload["summary"]["by_day"]["2026-09-29"]["sessions_by_host"] == {
        "opencode": 1
    }
    html = index_path.read_text(encoding="utf-8")
    assert "populateHostFilters" in html
    assert "hostColor(host)" in html

    write_signal_artifacts(layout, run_signals(layout))
    experiments_path = build_experiments(
        artifact_root=layout.root,
        out_dir=out_dir,
    )
    experiments = json.loads(
        (out_dir / "data" / "trajectory_experiments.json").read_text(encoding="utf-8")
    )
    assert experiments["sessions"][0]["host"] == "opencode"
    assert "populateHosts" in experiments_path.read_text(encoding="utf-8")


def test_build_bounds_session_details_and_links_transcripts(
    monkeypatch,
    tmp_path: Path,
):
    layout = Layout(tmp_path / "archive")
    for index in (1, 2):
        session_id = f"session-{index}"
        write_events(
            layout.normalized_path("opencode", session_id),
            [
                NormalizedEvent(
                    event_id=f"message-{index}",
                    session_id=session_id,
                    host="opencode",
                    sequence=1,
                    actor="user",
                    event_type="message",
                    summary=f"session {index}",
                    raw_ref=RawRef(
                        path=f"raw/opencode/{session_id}/session.jsonl",
                        line=1,
                    ),
                    timestamp=f"2026-09-29T20:00:0{index}Z",
                )
            ],
        )
        rendered = layout.rendered_path("opencode", session_id)
        rendered.parent.mkdir(parents=True, exist_ok=True)
        rendered.write_text(
            f"TRANSCRIPT-CONTENT-{index}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        dashboard_build_module,
        "DASHBOARD_SESSION_LIMIT",
        1,
    )
    out_dir = tmp_path / "dashboard"

    index_path = build(artifact_root=layout.root, out_dir=out_dir)

    payload = json.loads((out_dir / "data" / "rollouts.json").read_text(encoding="utf-8"))
    assert payload["summary"]["session_count"] == 2
    assert payload["summary"]["sessions_in_payload"] == 1
    assert len(payload["sessions"]) == 1
    assert "rendered_markdown" not in payload["sessions"][0]
    assert payload["sessions"][0]["rendered_href"].startswith("rendered/opencode/")
    html = index_path.read_text(encoding="utf-8")
    assert "TRANSCRIPT-CONTENT" not in html
    assert "Load transcript here" in html
    assert (out_dir / "rendered").is_symlink()


def test_atomic_publication_exposes_rendered_transcripts(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    write_events(
        layout.normalized_path("opencode", "session-1"),
        [
            NormalizedEvent(
                event_id="message-1",
                session_id="session-1",
                host="opencode",
                sequence=1,
                actor="user",
                event_type="message",
                summary="hello",
                raw_ref=RawRef(
                    path="raw/opencode/session-1/session.jsonl",
                    line=1,
                ),
            )
        ],
    )
    transcript = layout.rendered_path("opencode", "session-1")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("hello transcript\n", encoding="utf-8")
    out_dir = tmp_path / "dashboard"

    build_and_publish_dashboard(
        artifact_root=layout.root,
        output_dir=out_dir,
    )

    assert (out_dir / "rendered").is_symlink()
    assert (
        out_dir / "rendered" / "opencode" / "session-1.md"
    ).read_text(encoding="utf-8") == "hello transcript\n"
