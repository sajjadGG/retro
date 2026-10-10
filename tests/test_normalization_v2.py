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
from retro.utils import event_text, event_tool_input, event_tool_output

FIXTURES = Path(__file__).parent / "fixtures"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


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
    assert len(command_results) == 1
    assert all(event.parent_event_id == command_call.event_id for event in command_results)
    detailed = next(event for event in command_results if "exit_code" in event.payload)
    assert detailed.payload["exit_code"] == 1
    assert detailed.payload["success"] is False
    assert detailed.payload["is_error"] is True
    duplicate = next(
        event
        for event in events
        if event.payload.get("metadata_kind") == "duplicate_tool_result"
    )
    assert duplicate.payload["duplicate_of"] == detailed.event_id
    assert "output" not in duplicate.payload

    inferred = [
        event
        for event in events
        if event.raw_ref.line > 1 and not event.event_id.endswith(":session-end")
    ]
    assert inferred
    assert all(event.timestamp == "2026-10-01T00:00:00Z" for event in inferred)
    assert all(event.payload.get("timestamp_inferred") is True for event in inferred)


def test_codex_adjacent_cross_representation_messages_are_deduplicated(
    tmp_path: Path,
):
    path = tmp_path / "mirror.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-10-01T00:00:00Z",
                "payload": {"id": "mirror", "cwd": "/workspace"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "user-item",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "continue"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "client_id": "user-client",
                    "turn_id": "turn-1",
                    "message": "continue",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "turn_id": "turn-1",
                    "phase": "commentary",
                    "message": "Working",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "assistant-item",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Working"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            },
        ],
    )
    importer = CodexImporter(Layout(tmp_path / "archive"), codex_home=tmp_path / "disabled")

    events, unknown, gaps = importer._normalize(path, "mirror")

    assert unknown == 0
    assert gaps == []
    messages = [
        event
        for event in events
        if event.event_type == "message" and event.actor in {"user", "assistant"}
    ]
    duplicates = [
        event
        for event in events
        if event.payload.get("metadata_kind") == "duplicate_message"
    ]
    assert [(event.actor, event_text(event)) for event in messages] == [
        ("user", "continue"),
        ("assistant", "Working"),
    ]
    assert [event.actor for event in duplicates] == ["user", "assistant"]
    assert {event.payload["duplicate_of"] for event in duplicates} == {
        event.event_id for event in messages
    }


def test_codex_repeated_text_in_separate_turns_is_not_deduplicated(
    tmp_path: Path,
):
    path = tmp_path / "repeated-turns.jsonl"
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-10-01T00:00:00Z",
            "payload": {"id": "repeated-turns", "cwd": "/workspace"},
        }
    ]
    for turn_id in ("turn-1", "turn-2"):
        records.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": f"user-{turn_id}",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "client_id": f"client-{turn_id}",
                        "turn_id": turn_id,
                        "message": "continue",
                    },
                },
            ]
        )
    for turn_id, phase in (("turn-1", "commentary"), ("turn-2", "final_answer")):
        records.extend(
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "turn_id": turn_id,
                        "phase": phase,
                        "message": "Done",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": f"assistant-{turn_id}",
                        "role": "assistant",
                        "phase": phase,
                        "content": [{"type": "output_text", "text": "Done"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id
                        },
                    },
                },
            ]
        )
    _write_jsonl(path, records)
    importer = CodexImporter(Layout(tmp_path / "archive"), codex_home=tmp_path / "disabled")

    events, unknown, gaps = importer._normalize(path, "repeated-turns")

    assert unknown == 0
    assert gaps == []
    messages = [
        event
        for event in events
        if event.event_type == "message" and event.actor in {"user", "assistant"}
    ]
    duplicates = [
        event
        for event in events
        if event.payload.get("metadata_kind") == "duplicate_message"
    ]
    assert [(event.actor, event_text(event)) for event in messages] == [
        ("user", "continue"),
        ("user", "continue"),
        ("assistant", "Done"),
        ("assistant", "Done"),
    ]
    assert [event.actor for event in duplicates] == [
        "user",
        "user",
        "assistant",
        "assistant",
    ]


def test_claude_and_copilot_preserve_repeated_message_text(tmp_path: Path):
    claude_path = tmp_path / "claude.jsonl"
    _write_jsonl(
        claude_path,
        [
            {
                "type": "user",
                "uuid": "claude-1",
                "timestamp": "2026-10-01T00:00:00Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "continue"},
            },
            {
                "type": "user",
                "uuid": "claude-2",
                "timestamp": "2026-10-01T00:01:00Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "continue"},
            },
        ],
    )
    claude = ClaudeImporter(Layout(tmp_path / "claude-archive"), claude_home=tmp_path)
    claude_events, _, _ = claude._normalize(claude_path, "claude-repeated")

    copilot_path = tmp_path / "copilot.jsonl"
    _write_jsonl(
        copilot_path,
        [
            {
                "type": "user.message",
                "id": "copilot-1",
                "timestamp": "2026-10-01T00:00:00Z",
                "data": {"content": "continue"},
            },
            {
                "type": "user.message",
                "id": "copilot-2",
                "timestamp": "2026-10-01T00:01:00Z",
                "data": {"content": "continue"},
            },
        ],
    )
    copilot = CopilotCliImporter(
        Layout(tmp_path / "copilot-archive"),
        session_state_dir=tmp_path,
    )
    copilot_events, _, _ = copilot._normalize(copilot_path, "copilot-repeated")

    assert [
        event_text(event)
        for event in claude_events
        if event.actor == "user" and event.event_type == "message"
    ] == ["continue", "continue"]
    assert [
        event_text(event)
        for event in copilot_events
        if event.actor == "user" and event.event_type == "message"
    ] == ["continue", "continue"]


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
    assert sum(
        event.payload.get("type") == "file-history-snapshot"
        for event in events
    ) == 1
    assert sum(
        isinstance(event.payload.get("message"), dict)
        and "usage" in event.payload["message"]
        for event in events
    ) == 1

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
    assert "content" not in failed.payload
    assert "input" not in calls["tool-bash"].payload

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
    assert sum(event.event_type == "usage" for event in events) == 1
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
    assert "output" not in shell_result.payload
    assert "input" not in shell_call.payload
    assert "source_chunks" not in shell_call.payload
    assert event_tool_output(shell_call) is None
    assert event_tool_input(shell_call) == {"command": "false"}
    assert event_tool_output(shell_result) is None

    external_call = next(
        event for event in events if event.payload.get("call_id") == "call-external"
        and event.actor == "assistant"
    )
    external_result = next(
        event for event in events if event.payload.get("call_id") == "call-external"
        and event.actor == "tool"
    )
    assert external_result.parent_event_id == external_call.event_id
    assert "output" not in external_result.payload
    assert event_tool_output(external_result) == {"ok": True}

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
