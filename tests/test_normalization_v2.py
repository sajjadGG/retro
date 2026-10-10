from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from retro.importers.claude import ClaudeImporter
from retro.importers.codex import CodexImporter
from retro.importers.copilot_cli import CopilotCliImporter
from retro.renderer import render_markdown
from retro.renormalize import (
    NORMALIZER_VERSIONS,
    parse_since,
    renormalize_archive,
)
from retro.schema import NormalizedEvent, RawRef
from retro.storage import Layout

FIXTURES = Path(__file__).parent / "fixtures"


def test_codex_v2_maps_known_records_and_pairs_results(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    importer = CodexImporter(layout, codex_home=tmp_path / "disabled")

    events, unknown, gaps = importer._normalize(
        FIXTURES / "codex_normalization_v2.jsonl",
        "codex-v2",
    )

    assert unknown == 1
    assert gaps == ["future_codex_record"]
    by_payload_type = {
        event.payload.get("type"): event
        for event in events
        if event.payload.get("type")
    }
    assert by_payload_type["agent_reasoning"].event_type == "reasoning"
    assert by_payload_type["token_count"].event_type == "usage"
    assert by_payload_type["task_started"].event_type == "lifecycle"
    assert by_payload_type["thread_settings_applied"].event_type == "metadata"
    assert by_payload_type["context_compacted"].event_type == "compaction"
    assert by_payload_type["tool_search_call"].event_type == "tool_call"
    assert by_payload_type["tool_search_output"].event_type == "tool_result"
    assert not any(
        event.event_type == "unknown"
        and event.payload.get("type") != "future_codex_record"
        for event in events
    )

    command_call = next(
        event for event in events if event.payload.get("call_id") == "call-command"
        and event.event_type == "command"
    )
    command_results = [
        event for event in events if event.payload.get("call_id") == "call-command"
        and event.event_type == "tool_result"
    ]
    assert len(command_results) == 2
    assert all(event.parent_event_id == command_call.event_id for event in command_results)
    detailed = next(event for event in command_results if "exit_code" in event.payload)
    assert detailed.payload["exit_code"] == 1
    assert detailed.payload["success"] is False
    assert detailed.payload["is_error"] is True

    inferred = [
        event
        for event in events
        if event.raw_ref.line > 1 and not event.event_id.endswith(":session-end")
    ]
    assert inferred
    assert all(event.timestamp == "2026-10-01T00:00:00Z" for event in inferred)
    assert all(event.payload.get("timestamp_inferred") is True for event in inferred)


def test_claude_v2_maps_metadata_injection_and_tool_results(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    importer = ClaudeImporter(layout, claude_home=tmp_path / "disabled")

    events, unknown, gaps = importer._normalize(
        FIXTURES / "claude_normalization_v2.jsonl",
        "claude-v2",
    )

    assert unknown == 1
    assert gaps == ["future-claude-record"]
    top_types = {
        event.payload.get("type"): event.event_type
        for event in events
        if event.payload.get("type")
    }
    assert top_types["mode"] == "lifecycle"
    assert top_types["bridge-session"] == "metadata"
    assert top_types["file-history-delta"] == "metadata"
    assert top_types["cost-state"] == "usage"
    assert top_types["queue-operation"] == "lifecycle"
    assert top_types["frame-link"] == "metadata"

    injected = next(event for event in events if event.event_id == "meta-user")
    assert injected.actor == "system"
    assert injected.event_type == "attachment"
    sidechain = next(event for event in events if event.event_id == "sidechain-1")
    assert sidechain.actor == "subagent"

    calls = {
        str(event.payload["call_id"]): event
        for event in events
        if event.event_type in {"file_read", "command"}
        and event.payload.get("call_id")
    }
    results = [event for event in events if event.event_type == "tool_result"]
    assert {event.payload["call_id"] for event in results} == {
        "tool-read",
        "tool-bash",
    }
    assert all(
        event.parent_event_id == calls[str(event.payload["call_id"])].event_id
        for event in results
    )
    failed = next(event for event in results if event.payload["call_id"] == "tool-bash")
    assert failed.payload["success"] is False
    assert failed.payload["is_error"] is True
    assert failed.payload["tool_use_result"]["stderr"] == "failed"

    assert all(event.timestamp is not None for event in events)
    inferred = [
        event
        for event in events
        if event.raw_ref.line > 1 and not event.event_id.endswith(":session-end")
    ]
    assert all(event.payload.get("timestamp_inferred") is True for event in inferred)


def test_copilot_v2_maps_noise_usage_attribution_and_pairing(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    importer = CopilotCliImporter(
        layout,
        session_state_dir=tmp_path / "disabled",
    )

    events, unknown, gaps = importer._normalize(
        FIXTURES / "copilot_cli_normalization_v2.jsonl",
        "copilot-v2",
    )

    assert unknown == 1
    assert gaps == ["future.copilot.event"]
    by_raw_id = {
        event.event_id.split(":agent-host:", 1)[1].split("#", 1)[0]: event
        for event in events
    }
    assert by_raw_id["turn-start"].event_type == "lifecycle"
    assert by_raw_id["hook-start"].event_type == "lifecycle"
    assert by_raw_id["usage"].event_type == "usage"
    assert by_raw_id["model"].event_type == "metadata"
    assert by_raw_id["compact-start"].event_type == "compaction"
    assert by_raw_id["skill"].event_type == "attachment"
    assert by_raw_id["skill"].payload["content"] == "skill body"

    assert by_raw_id["user"].actor == "user"
    assert by_raw_id["user"].event_type == "message"
    assert by_raw_id["skill-context"].actor == "system"
    assert by_raw_id["skill-context"].event_type == "attachment"
    assert by_raw_id["agent-prompt"].actor == "subagent"
    assert by_raw_id["system-steer"].event_type == "lifecycle"
    assert by_raw_id["auto"].actor == "system"

    shell_call = next(
        event for event in events if event.payload.get("call_id") == "call-shell"
        and event.event_type == "command"
    )
    shell_result = next(
        event for event in events if event.payload.get("call_id") == "call-shell"
        and event.event_type == "tool_result"
    )
    assert shell_result.parent_event_id == shell_call.event_id
    assert shell_result.payload["exit_code"] == 1
    assert shell_result.payload["success"] is False

    external_call = next(
        event for event in events if event.payload.get("call_id") == "call-external"
        and event.actor == "assistant"
    )
    external_result = next(
        event for event in events if event.payload.get("call_id") == "call-external"
        and event.actor == "tool"
    )
    assert external_result.parent_event_id == external_call.event_id

    assert all(event.timestamp is not None for event in events)
    inferred = [event for event in events if event.raw_ref.line > 1]
    assert all(event.payload.get("timestamp_inferred") is True for event in inferred)


def test_renderer_filters_noise_and_inlines_tool_results():
    call = _event(
        "call",
        "assistant",
        "command",
        payload={"name": "bash", "call_id": "c1", "input": {"command": "pytest"}},
    )
    result = _event(
        "result",
        "tool",
        "tool_result",
        parent_event_id="call",
        payload={"call_id": "c1", "output": "1 passed", "success": True, "exit_code": 0},
    )
    lifecycle = _event(
        "turn",
        "assistant",
        "lifecycle",
        summary="turn marker",
        payload={"lifecycle": "turn_start"},
    )
    metadata = _event(
        "settings",
        "system",
        "metadata",
        summary="settings marker",
        payload={"metadata_kind": "settings"},
    )
    usage = _event(
        "usage",
        "system",
        "usage",
        summary="usage marker",
        payload={"tokens": 1},
    )

    rendered = render_markdown([call, lifecycle, result, metadata, usage])

    assert "turn marker" not in rendered
    assert "settings marker" not in rendered
    assert "usage marker" not in rendered
    assert "### Result — success, exit 0" in rendered
    assert "1 passed" in rendered
    assert "## · tool_result" not in rendered

    noisy = render_markdown(
        [call, lifecycle, result, metadata, usage],
        include_noise=True,
    )
    assert "turn marker" in noisy
    assert "settings marker" in noisy
    assert "usage marker" in noisy


def test_renormalize_is_incremental_dry_run_atomic_and_raw_safe(
    tmp_path: Path,
    monkeypatch,
):
    layout = Layout(tmp_path / "archive")
    raw_dir = layout.raw_dir("codex", "codex-v2")
    raw_dir.mkdir(parents=True)
    shutil.copy2(FIXTURES / "codex_normalization_v2.jsonl", raw_dir / "rollout.jsonl")
    raw_before = _tree_digest(layout.root / "raw")

    dry = renormalize_archive(layout, dry_run=True)
    assert dry.scanned == 1
    assert dry.outdated == 1
    assert dry.rebuilt == 0
    assert not layout.normalized_path("codex", "codex-v2").exists()

    normalized_only = renormalize_archive(layout, render=False)
    assert normalized_only.rebuilt == 1
    assert normalized_only.rendered == 0
    assert layout.normalized_path("codex", "codex-v2").is_file()
    assert not layout.rendered_path("codex", "codex-v2").exists()

    first = renormalize_archive(layout)
    assert first.rebuilt == 1
    assert first.rendered == 1
    assert first.failures == []
    normalized = layout.normalized_path("codex", "codex-v2")
    rendered = layout.rendered_path("codex", "codex-v2")
    assert normalized.is_file()
    assert rendered.is_file()
    assert _tree_digest(layout.root / "raw") == raw_before

    second = renormalize_archive(layout)
    assert second.rebuilt == 0
    assert second.skipped == 1

    index_path = layout.root / "normalized" / "normalizer-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    record = index["sessions"]["codex"]["codex-v2"]
    assert record["version"] == NORMALIZER_VERSIONS["codex"]
    record["version"] = 1
    index_path.write_text(json.dumps(index), encoding="utf-8")
    old_normalized = normalized.read_bytes()
    old_rendered = rendered.read_bytes()

    def fail_render(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("retro.renormalize.render_file", fail_render)
    failed = renormalize_archive(layout)
    assert failed.rebuilt == 0
    assert len(failed.failures) == 1
    assert normalized.read_bytes() == old_normalized
    assert rendered.read_bytes() == old_rendered
    assert _tree_digest(layout.root / "raw") == raw_before

    future = parse_since("2999-01-01")
    assert future is not None
    filtered = renormalize_archive(layout, since=future, dry_run=True)
    assert filtered.scanned == 0


def _event(
    event_id: str,
    actor: str,
    event_type: str,
    *,
    summary: str | None = None,
    payload: dict | None = None,
    parent_event_id: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        session_id="renderer-v2",
        host="codex",
        sequence=1,
        actor=actor,  # type: ignore[arg-type]
        event_type=event_type,  # type: ignore[arg-type]
        summary=summary or event_id,
        raw_ref=RawRef(path="raw/codex/renderer-v2/rollout.jsonl", line=1),
        parent_event_id=parent_event_id,
        payload=payload or {},
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
