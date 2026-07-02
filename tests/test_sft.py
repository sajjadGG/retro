from __future__ import annotations

import json
from pathlib import Path

from retro.sft import compare_benchmark_results, export_curated_dataset


def test_export_curated_dataset_masks_only_assistant_messages(claude_imported, codex_imported):
    layout, _ = claude_imported
    export_curated_dataset(layout, dataset_name="distill")

    train_path = layout.sft_dataset_path("distill", "train")
    rows = [json.loads(line) for line in train_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows
    assert {row["host"] for row in rows} == {"claude-code", "codex"}

    roles = {message["role"] for row in rows for message in row["messages"]}
    assert "assistant" in roles
    assert "tool_call" in roles
    assert "tool" in roles

    for row in rows:
        for message in row["messages"]:
            assert message["loss_mask"] is (message["role"] == "assistant")


def test_export_curated_dataset_rejects_failed_rollout(tmp_path: Path, tmp_layout):
    normalized_dir = tmp_layout.root / "normalized" / "codex"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = normalized_dir / "broken.events.jsonl"
    normalized_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "broken:1",
                        "session_id": "broken",
                        "host": "codex",
                        "sequence": 1,
                        "actor": "user",
                        "event_type": "message",
                        "summary": "Fix auth",
                        "raw_ref": {"path": "broken.jsonl", "line": 1},
                        "payload": {"text": "Fix auth"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "broken:2",
                        "session_id": "broken",
                        "host": "codex",
                        "sequence": 2,
                        "actor": "assistant",
                        "event_type": "message",
                        "summary": "Running tests",
                        "raw_ref": {"path": "broken.jsonl", "line": 2},
                        "payload": {"text": "I will run the tests now."},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "broken:4",
                        "session_id": "broken",
                        "host": "codex",
                        "sequence": 4,
                        "actor": "assistant",
                        "event_type": "tool_call",
                        "summary": "exec_command(cmd=pytest)",
                        "raw_ref": {"path": "broken.jsonl", "line": 4},
                        "payload": {"name": "exec_command", "arguments": {"cmd": "pytest"}},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "broken:4",
                        "session_id": "broken",
                        "host": "codex",
                        "sequence": 3,
                        "actor": "tool",
                        "event_type": "tool_result",
                        "summary": "tool_result: exec_command",
                        "raw_ref": {"path": "broken.jsonl", "line": 3},
                        "payload": {"output": "SyntaxError: invalid syntax\nProcess exited with code 1"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = export_curated_dataset(tmp_layout, dataset_name="distill")
    assert result.manifest["selected_sessions"] == 0
    assert result.manifest["rejected_sessions"] == 1
    assert result.rejected[0].reason == "contains failed tool output or syntax errors"


def test_compare_benchmark_results(tmp_path: Path):
    base_path = tmp_path / "base.jsonl"
    tuned_path = tmp_path / "tuned.jsonl"
    base_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "benchmark": "swe-bench-verified",
                        "passed": True,
                        "trajectory_success": True,
                        "tool_format_adherence": True,
                    }
                ),
                json.dumps(
                    {
                        "benchmark": "swe-bench-verified",
                        "passed": False,
                        "trajectory_success": False,
                        "tool_format_adherence": True,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    tuned_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "benchmark": "swe-bench-verified",
                        "passed": True,
                        "trajectory_success": True,
                        "tool_format_adherence": True,
                    }
                ),
                json.dumps(
                    {
                        "benchmark": "swe-bench-verified",
                        "passed": True,
                        "trajectory_success": True,
                        "tool_format_adherence": False,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    summary = compare_benchmark_results(base_path, tuned_path)
    assert summary["metrics"]["pass_at_1"]["base"] == 0.5
    assert summary["metrics"]["pass_at_1"]["tuned"] == 1.0
    assert summary["metrics"]["pass_at_1"]["delta"] == 0.5
