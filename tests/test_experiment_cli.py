"""Experiment CLI planning, grading and cleanup use the same durable job contract."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from retro.benchmarks.task_scorer.suite import CurationPolicy, curate_taskset
from retro.catalog import Catalog, SessionRecord
from retro.cli import app
from tests.test_task_scorer_suite import make_published_fixture

runner = CliRunner()


def test_cli_plan_run_inspect_and_cleanup_are_distinct(tmp_path: Path, monkeypatch):
    fixture = make_published_fixture(tmp_path)
    curate_taskset(
        fixture.layout, "pilot", suite_id="cli-v1", policy=CurationPolicy(allow_reconstructed=True)
    )
    monkeypatch.setenv("FAKE_GHOSTLAB_PLAN", str(fixture.plan_path))
    root = ["--root", str(fixture.layout.root)]
    planned = runner.invoke(
        app,
        [
            "experiment",
            "create",
            "--id",
            "cli-comparison",
            "--title",
            "Behavior comparison",
            "--dataset",
            "pilot/cli-v1/test",
            "--agent",
            str(fixture.good_agent),
            "--agent",
            str(fixture.bad_agent),
            "--seeds",
            "0,1",
            "--json",
            *root,
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["status"] == "planned"
    assert plan["requested_attempts"] == 4
    assert all(run["completed_attempts"] == 0 for run in plan["runs"])
    assert "config_path" not in planned.output
    executed = runner.invoke(
        app,
        [
            "experiment",
            "run",
            "cli-comparison",
            "--ghostlab-bin",
            str(fixture.binary),
            "--json",
            *root,
        ],
    )
    assert executed.exit_code == 0, executed.output
    complete = json.loads(executed.output)
    assert complete["status"] == "completed"
    assert sum(run["scored_attempts"] for run in complete["runs"]) == 4
    shown = runner.invoke(app, ["experiment", "show", "cli-comparison", "--json", *root])
    assert shown.exit_code == 0 and json.loads(shown.output)["setup_sha256"] == plan["setup_sha256"]
    listed = runner.invoke(app, ["experiment", "list", "--json", *root])
    assert listed.exit_code == 0 and json.loads(listed.output)[0]["experiment_id"] == "cli-comparison"
    cleaned = runner.invoke(
        app, ["experiment", "cleanup", "cli-comparison", "--prune-artifacts", "--json", *root]
    )
    assert cleaned.exit_code == 0, cleaned.output
    assert json.loads(cleaned.output)["cleanup"]["artifacts_pruned"] is True
    for run in complete["runs"]:
        for seed in (0, 1):
            directory = fixture.paths.attempt_dir(run["eval_id"], fixture.task_id, run["agent_id"], seed)
            assert (directory / "score-report.json").exists()
            assert not (directory / "workspace").exists()


def test_analyze_uses_included_catalog_instead_of_promoting_experiment_traces(tmp_path: Path, monkeypatch):
    import retro.cli as cli
    import retro.signals as signals

    ordinary = SessionRecord.from_dict(
        {"key": "codex/user", "host": "codex", "session_id": "user", "excluded": False}
    )
    experimental = SessionRecord.from_dict(
        {
            "key": "codex/job",
            "host": "codex",
            "session_id": "job",
            "excluded": True,
            "tags": ["experiment", "ignore"],
        }
    )
    catalog = Catalog({ordinary.key: ordinary, experimental.key: experimental})
    monkeypatch.setattr(cli, "update_catalog", lambda _layout: None)
    monkeypatch.setattr(cli, "load_catalog", lambda _layout: catalog)
    selected = []

    def analyze(_layout, source):
        selected.extend(record.key for record in source.included())
        return {}

    monkeypatch.setattr(signals, "analyze_sessions", analyze)
    monkeypatch.setattr(signals, "render_console_report", lambda _stats: None)
    monkeypatch.setattr(signals, "generate_report", lambda _stats, _path: None)
    result = runner.invoke(app, ["analyze", "--root", str(tmp_path / "archive")])
    assert result.exit_code == 0, result.output
    assert selected == ["codex/user"]
