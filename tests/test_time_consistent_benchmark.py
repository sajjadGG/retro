"""Tests for the time-consistent rollout benchmark."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from retro.benchmarks import (
    PROMPT_LEVELS,
    build_time_consistent_benchmark,
    evaluate_time_consistent_benchmark,
    file_localization_metrics,
    parse_timestamp,
)
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert process.returncode == 0, process.stderr
    return process.stdout.strip()


def _event(
    sequence: int,
    *,
    actor: str,
    event_type: str,
    timestamp: str,
    payload: dict | None = None,
    summary: str = "",
    parent_event_id: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"session-1:{sequence}",
        session_id="session-1",
        host="codex",
        sequence=sequence,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="raw/codex/session-1/rollout.jsonl", line=sequence),
        timestamp=timestamp,
        parent_event_id=parent_event_id,
        payload=payload or {},
    )


@pytest.fixture
def benchmark_source(tmp_path: Path) -> tuple[Layout, Path]:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "retro@example.invalid")
    _git(project, "config", "user.name", "Retro Tests")
    (project / "src").mkdir()
    (project / "tests").mkdir()
    (project / "docs").mkdir()
    (project / "workspace").mkdir()
    (project / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (project / "tests" / "test_app.py").write_text(
        "def test_run():\n    assert True\n",
        encoding="utf-8",
    )
    (project / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    _git(project, "add", ".")
    commit_env = {
        **dict(os.environ),
        "GIT_AUTHOR_DATE": "2026-01-01T12:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T12:00:00Z",
    }
    _git(project, "commit", "-m", "initial", env=commit_env)

    events = [
        _event(
            1,
            actor="system",
            event_type="session_start",
            timestamp="2026-01-02T00:00:00Z",
            payload={"cwd": str(project / "workspace"), "model": "test-model"},
        ),
        _event(
            2,
            actor="user",
            event_type="message",
            timestamp="2026-01-02T00:00:01Z",
            payload={"text": "Update the old setup."},
        ),
        _event(
            3,
            actor="assistant",
            event_type="file_edit",
            timestamp="2026-01-02T00:00:02Z",
            payload={
                "name": "apply_patch",
                "arguments": "*** Update File: docs/guide.md\n@@\n-old\n+new\n",
            },
        ),
        _event(
            4,
            actor="user",
            event_type="message",
            timestamp="2026-07-01T00:00:00Z",
            payload={
                "text": (
                    "<codex_delegation>\n"
                    "<source_thread_id>private-thread</source_thread_id>\n"
                    "<input>\n"
                    "Fix src/app.py when the widget fails and update tests/test_app.py.\n"
                    "api_key=do-not-copy\n"
                    "Create a branch, push it, and open a pull request.\n"
                    "</input>\n"
                    "</codex_delegation>"
                )
            },
        ),
        _event(
            5,
            actor="assistant",
            event_type="command",
            timestamp="2026-07-01T00:00:01Z",
            payload={
                "name": "exec_command",
                "arguments": {
                    "cmd": (
                        "sed -n '1,80p' ../src/app.py && "
                        "rg 'app.py|2>/dev/null' ../tests/test_app.py"
                    )
                },
            },
        ),
        _event(
            6,
            actor="assistant",
            event_type="file_edit",
            timestamp="2026-07-01T00:00:01.500Z",
            payload={
                "tool_id": "failed-edit",
                "input": {"file_path": str(project / "src" / "failed.py")},
            },
        ),
        _event(
            7,
            actor="tool",
            event_type="file_edit",
            timestamp="2026-07-01T00:00:01.600Z",
            parent_event_id="session-1:6",
            payload={
                "tool_use_id": "failed-edit",
                "is_error": True,
                "content": (
                    f"The file {project / 'src' / 'failed.py'} could not be updated."
                ),
            },
        ),
        _event(
            8,
            actor="assistant",
            event_type="file_edit",
            timestamp="2026-07-01T00:00:02Z",
            payload={
                "name": "apply_patch",
                "arguments": (
                    "*** Begin Patch\n"
                    "*** Update File: src/app.py\n"
                    "@@\n-old\n+new\n"
                    "*** Add File: tests/test_app.py\n"
                    "+test\n"
                    "*** End Patch\n"
                ),
            },
        ),
        _event(
            9,
            actor="user",
            event_type="message",
            timestamp="2026-07-02T00:00:00Z",
            payload={
                "text": (
                    "<codex_delegation><input>"
                    "Run the documentation critique once now. "
                    "Automation: Daily docs critique "
                    "Automation ID: daily-docs "
                    "Automation memory: $CODEX_HOME/automations/daily-docs/memory.md "
                    "Last run: 2026-07-01T00:00:00Z (12345) "
                    "Update from the latest `main`, then review docs/guide.md for clarity. "
                    "Make focused fixes directly in the guide. "
                    "Create a branch, commit the improvements, push it, and open a pull request."
                    "</input></codex_delegation>"
                )
            },
        ),
        _event(
            10,
            actor="assistant",
            event_type="file_read",
            timestamp="2026-07-02T00:00:01Z",
            payload={"file_path": str(project / "docs" / "guide.md")},
        ),
        _event(
            11,
            actor="assistant",
            event_type="file_edit",
            timestamp="2026-07-02T00:00:02Z",
            payload={
                "input": {"file_path": str(project / "docs" / "guide.md")},
            },
        ),
        _event(
            12,
            actor="user",
            event_type="message",
            timestamp="2026-07-02T00:00:03Z",
            payload={"text": "thanks"},
        ),
    ]
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    write_events(layout.normalized_path("codex", "session-1"), events)
    return layout, project


def _build(layout: Layout, project: Path, benchmark_id: str = "temporal-test"):
    return build_time_consistent_benchmark(
        layout,
        benchmark_id=benchmark_id,
        project_root=project,
        cutoff_time="2026-06-01T00:00:00Z",
        end_time="2026-08-01T00:00:00Z",
        hosts=("codex",),
    )


def _jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_file_localization_metrics():
    metrics = file_localization_metrics(
        {"src/app.py", "tests/test_app.py"},
        {"src/app.py", "src/extra.py"},
    )
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5
    assert not metrics.exact_match


def test_timestamp_requires_timezone():
    assert parse_timestamp("2026-01-01T00:00:00-07:00").isoformat().endswith("+00:00")
    with pytest.raises(ValueError, match="timezone"):
        parse_timestamp("2026-01-01T00:00:00")


def test_builds_immutable_leakage_filtered_benchmark(benchmark_source):
    layout, project = benchmark_source
    result = _build(layout, project)

    assert result.task_count == 2
    assert result.path.is_dir()
    assert result.observed_predictions_path.is_file()
    assert result.observed_predictions_path.stat().st_mode & 0o777 == 0o600
    assert result.observed_predictions_path.parent.stat().st_mode & 0o777 == 0o700
    manifest = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["method"] == "time_consistent_file_localization_v1"
    assert manifest["temporal_contract"]["task_interval"] == "(cutoff, end]"
    assert manifest["source"]["eligible_knowledge_event_count"] == 3
    assert manifest["source"]["rejected_episode_counts"]["at_or_before_cutoff"] == 1
    assert manifest["source"]["rejected_episode_counts"]["non_task_goal"] == 1

    prompt_tasks = {
        level: _jsonl(result.path / "tasks" / "prompts" / f"{level}.jsonl")
        for level in PROMPT_LEVELS
    }
    private_tasks = _jsonl(result.path / "private" / "ground-truth.jsonl")
    assert all(len(records) == 2 for records in prompt_tasks.values())
    assert len(private_tasks) == 2
    assert set(prompt_tasks["minimal"][0]) == {
        "instruction",
        "prompt_level",
        "schema_version",
        "snapshot_commit",
        "task_id",
    }
    assert private_tasks[0]["expected_files"] == ["src/app.py", "tests/test_app.py"]
    for prompt in (prompt_tasks[level][0]["instruction"] for level in PROMPT_LEVELS):
        assert "src/app.py" not in prompt
        assert "test_app.py" not in prompt
        assert "do-not-copy" not in prompt
        assert "codex_delegation" not in prompt
        assert "pull request" not in prompt
    for prompt in (prompt_tasks[level][1]["instruction"] for level in PROMPT_LEVELS):
        assert "Automation ID" not in prompt
        assert "Last run" not in prompt
        assert "latest" not in prompt
        assert "pull request" not in prompt
    assert "focused fixes" in prompt_tasks["minimal"][1]["instruction"].lower()
    assert "review" in prompt_tasks["guided"][1]["instruction"].lower()

    observed = _jsonl(result.observed_predictions_path)
    assert observed[0]["prompt_level"] == "source"
    assert observed[0]["predicted_files"] == ["src/app.py", "tests/test_app.py"]
    assert observed[1]["predicted_files"] == ["docs/guide.md"]

    with pytest.raises(FileExistsError):
        _build(layout, project)


def test_evaluates_file_sets_and_matched_delta(benchmark_source, tmp_path: Path):
    layout, project = benchmark_source
    build = _build(layout, project)
    task_ids = [
        task["task_id"]
        for task in _jsonl(build.path / "tasks" / "prompts" / "contextual.jsonl")
    ]
    predictions = tmp_path / "predictions.jsonl"
    records = [
        {
            "task_id": task_ids[0],
            "condition": "baseline",
            "model": "test-model",
            "prompt_level": "contextual",
            "predicted_files": ["src/app.py"],
        },
        {
            "task_id": task_ids[1],
            "condition": "baseline",
            "model": "test-model",
            "prompt_level": "contextual",
            "predicted_files": [],
        },
        {
            "task_id": task_ids[0],
            "condition": "augmented",
            "model": "test-model",
            "prompt_level": "contextual",
            "predicted_files": ["src/app.py", "tests/test_app.py"],
        },
        {
            "task_id": task_ids[1],
            "condition": "augmented",
            "model": "test-model",
            "prompt_level": "contextual",
            "predicted_files": ["docs/guide.md"],
        },
    ]
    predictions.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = evaluate_time_consistent_benchmark(
        layout,
        benchmark_id=build.benchmark_id,
        predictions_path=predictions,
        run_id="matched-run",
    )
    by_condition = {row["condition"]: row for row in result.aggregate}
    assert by_condition["baseline"]["macro_f1"] == 0.333333
    assert by_condition["augmented"]["macro_f1"] == 1.0
    assert result.paired_comparisons == [
        {
            "augmented_condition": "augmented",
            "baseline_condition": "baseline",
            "losses": 0,
            "matched_task_count": 2,
            "mean_file_f1_delta": 0.666667,
            "model": "test-model",
            "prompt_level": "contextual",
            "ties": 0,
            "wins": 2,
        }
    ]
    assert (result.path / "results.json").is_file()
    assert (result.path / "private" / "task-details.jsonl").stat().st_mode & 0o777 == 0o600

    with pytest.raises(FileExistsError):
        evaluate_time_consistent_benchmark(
            layout,
            benchmark_id=build.benchmark_id,
            predictions_path=predictions,
            run_id="matched-run",
        )


def test_rejects_incomplete_prediction_group_by_default(benchmark_source, tmp_path: Path):
    layout, project = benchmark_source
    build = _build(layout, project)
    task_id = _jsonl(
        build.path / "tasks" / "prompts" / "minimal.jsonl"
    )[0]["task_id"]
    predictions = tmp_path / "partial.jsonl"
    predictions.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "condition": "baseline",
                "model": "test-model",
                "prompt_level": "minimal",
                "predicted_files": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing 1 task"):
        evaluate_time_consistent_benchmark(
            layout,
            benchmark_id=build.benchmark_id,
            predictions_path=predictions,
            run_id="strict-partial",
        )

    result = evaluate_time_consistent_benchmark(
        layout,
        benchmark_id=build.benchmark_id,
        predictions_path=predictions,
        run_id="allowed-partial",
        allow_partial=True,
    )
    assert result.aggregate[0]["task_count"] == 1


def test_detects_benchmark_artifact_tampering(benchmark_source, tmp_path: Path):
    layout, project = benchmark_source
    build = _build(layout, project)
    prompt_path = build.path / "tasks" / "prompts" / "minimal.jsonl"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        evaluate_time_consistent_benchmark(
            layout,
            benchmark_id=build.benchmark_id,
            predictions_path=predictions,
            run_id="tampered",
        )
