"""CLI execution of curated releases and framework-neutral training episodes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from retro.benchmarks.task_scorer.suite import CurationPolicy, curate_taskset
from retro.cli import app
from tests.task_scorer_harness import PRESERVING_FEATURE
from tests.test_task_scorer_suite import PublishedFixture, make_published_fixture

runner = CliRunner()


@pytest.fixture
def published(tmp_path: Path) -> PublishedFixture:
    return make_published_fixture(tmp_path)


@pytest.mark.parametrize("command", ["curate", "export", "reset", "reward"])
def test_environment_commands_have_help(command: str):
    result = runner.invoke(app, ["benchmark", "taskset", command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--suite" in result.output


def test_cli_curates_exports_resets_and_grades_a_real_candidate(
    published: PublishedFixture, tmp_path: Path, monkeypatch
):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(CurationPolicy(split_ratios={"train": 1, "validation": 0, "test": 0}).to_dict())
    )
    base = ["benchmark", "taskset"]
    scope = ["--name", "pilot", "--root", str(published.layout.root), "--suite", "training-v1"]
    result = runner.invoke(app, base + ["curate", "--policy", str(policy)] + scope)
    assert result.exit_code == 0, result.output
    assert "Frozen suite" in result.output
    result = runner.invoke(app, base + ["export", "--out", str(tmp_path / "export")] + scope)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "export" / "episodes.jsonl").is_file()
    episode = tmp_path / "episode"
    result = runner.invoke(
        app, base + ["reset", "--task-id", published.task_id, "--out", str(episode)] + scope
    )
    assert result.exit_code == 0, result.output
    (episode / "workspace" / "src" / "feature.py").write_text(PRESERVING_FEATURE)
    monkeypatch.setenv("FAKE_GHOSTLAB_PLAN", str(published.plan_path))
    reward_dir = tmp_path / "reward"
    result = runner.invoke(
        app,
        base + [
            "reward", "--task-id", published.task_id, "--candidate", str(episode / "workspace"),
            "--out", str(reward_dir), "--ghostlab-bin", str(published.binary),
        ] + scope,
    )
    assert result.exit_code == 0, result.output
    reward = json.loads((reward_dir / "episode-reward.json").read_text())
    assert reward["status"] == "scored"
    assert reward["reward"] == pytest.approx(1)


def test_cli_reward_reports_missing_runner_without_a_fake_zero(published: PublishedFixture, tmp_path: Path):
    suite = curate_taskset(published.layout, "pilot")
    episode = tmp_path / "episode"
    scope = [
        "--name", "pilot", "--root", str(published.layout.root),
        "--suite", suite.suite_id, "--split", "test", "--task-id", published.task_id,
    ]
    reset = runner.invoke(app, ["benchmark", "taskset", "reset", "--out", str(episode)] + scope)
    assert reset.exit_code == 0, reset.output
    out = tmp_path / "reward"
    result = runner.invoke(
        app,
        ["benchmark", "taskset", "reward", "--out", str(out),
         "--candidate", str(episode / "workspace"), "--ghostlab-bin", str(tmp_path / "no-runner")]
        + scope,
    )
    assert result.exit_code == 1
    saved = json.loads((out / "episode-reward.json").read_text())
    assert saved["status"] == "harness_error"
    assert saved["reward"] is None
