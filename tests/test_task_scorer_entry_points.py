"""Tests for the CLI-facing taskset entry points (build / run / report)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer.build import (
    BuildConfigurationError,
    TasksetBuildSummary,
    TasksetPaths,
    build_taskset,
    compute_task_id,
    resolve_taskset_paths,
)
from retro.benchmarks.task_scorer.ghostlab_cli import GhostlabCli
from retro.benchmarks.task_scorer.run import (
    TasksetReportSummary,
    TasksetRunSummary,
    TaskVerificationError,
    report_taskset,
    resolve_run_eval_id,
    run_taskset,
)
from retro.storage import Layout
from tests.task_scorer_harness import (
    BASE_TREE,
    TASK_PROMPT,
    accept_all_lint,
    make_agent_config,
    make_audit_outputs,
    make_builder_outputs,
    make_candidate_overlay,
    make_definer_outputs,
    make_source_bundle,
    write_fake_ghostlab,
    write_plan,
)

SOURCE_ID = "codex__019abc"
NAME = "pilot"


class _Archive:
    """A real ``Layout`` archive wired to the fake ghostlab executable."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.layout = Layout(tmp_path / "archive")
        self.layout.ensure()
        self.paths = TasksetPaths.from_layout(self.layout, NAME)
        self.source_root = make_source_bundle(self.paths.sources_dir(), SOURCE_ID)
        self.task_id = compute_task_id(SOURCE_ID, BASE_TREE, "replay", TASK_PROMPT)

        definer_out = make_definer_outputs(tmp_path / "out" / "definer", SOURCE_ID)
        builder_out = make_builder_outputs(tmp_path / "out" / "builder", self.task_id)
        audit_out = make_audit_outputs(tmp_path / "out" / "auditor")
        solved = make_candidate_overlay(tmp_path / "out" / "solved", solved=True)
        unsolved = make_candidate_overlay(tmp_path / "out" / "unsolved", solved=False)

        self.binary = write_fake_ghostlab(tmp_path / "bin")
        self.plan_path = tmp_path / "plan.json"
        self.plan: dict[str, Any] = {
            "artifact_runs": {
                "retro-task-definer-v1": {"outputs": str(definer_out)},
                "retro-scorer-builder-v1": {"outputs": str(builder_out)},
                "retro-scorer-auditor-v1": {"outputs": str(audit_out)},
                "candidate-good": {"workspace_overlay": str(solved)},
                "candidate-bad": {"workspace_overlay": str(unsolved)},
            }
        }
        write_plan(self.plan_path, self.plan)

        agents = tmp_path / "agents"
        self.definer = make_agent_config(agents / "definer.json", "retro-task-definer-v1", "m-d")
        self.builder = make_agent_config(agents / "builder.json", "retro-scorer-builder-v1", "m-b")
        self.auditor = make_agent_config(agents / "auditor.json", "retro-scorer-auditor-v1", "m-a")
        self.good = make_agent_config(agents / "good.json", "candidate-good", "m-c")
        self.bad = make_agent_config(agents / "bad.json", "candidate-bad", "m-c")

    @property
    def env(self) -> dict[str, str]:
        return {"FAKE_GHOSTLAB_PLAN": str(self.plan_path)}

    def build(self, **overrides: Any) -> TasksetBuildSummary:
        kwargs: dict[str, Any] = {
            "lint": accept_all_lint,
            "repeatability_runs": 1,
            "ghostlab_env": self.env,
        }
        kwargs.update(overrides)
        return build_taskset(
            self.layout,
            NAME,
            self.binary,
            self.definer,
            self.builder,
            self.auditor,
            0,
            **kwargs,
        )

    def run(self, agent: Path | None = None, seeds: Any = "0", **overrides: Any) -> TasksetRunSummary:
        kwargs: dict[str, Any] = {"ghostlab_env": self.env}
        kwargs.update(overrides)
        return run_taskset(self.layout, NAME, agent or self.good, seeds, self.binary, **kwargs)


@pytest.fixture()
def archive(tmp_path: Path) -> _Archive:
    return _Archive(tmp_path)


def test_resolve_taskset_paths_accepts_layout_root_or_paths(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "archive")
    from_layout = resolve_taskset_paths(layout, NAME)
    assert from_layout.root == layout.benchmark_taskset_dir(NAME)
    assert resolve_taskset_paths(tmp_path / "archive", NAME).root == from_layout.root
    assert resolve_taskset_paths(str(tmp_path / "archive"), NAME).root == from_layout.root
    assert resolve_taskset_paths(from_layout, NAME) is from_layout

    with pytest.raises(BuildConfigurationError):
        resolve_taskset_paths(from_layout, "other")
    with pytest.raises(BuildConfigurationError):
        resolve_taskset_paths(layout, "../escape")


def test_build_taskset_matches_the_cli_signature(archive: _Archive) -> None:
    summary = archive.build()

    assert isinstance(summary, TasksetBuildSummary)
    assert summary.name == NAME
    assert summary.published == 1
    assert summary.published_task_ids == (archive.task_id,)
    assert summary.sources_total == 1
    assert summary.sources_ok == 1
    assert summary.sources_failed == 0
    assert summary.tasks_dir == archive.layout.benchmark_taskset_tasks_dir(NAME)
    assert summary.build_dir == archive.layout.benchmark_taskset_build_run_dir(NAME, summary.build_id)
    assert summary.report_path.is_file()
    assert summary.rejection_counts == {"NO_OBSERVABLE_OUTCOME": 1}

    rows = summary.source_rows()
    assert rows == [
        {
            "source_id": SOURCE_ID,
            "stage": "published",
            "status": "ok",
            "published": 1,
            "rejections": 1,
            "reused": 0,
            "code": "",
            "detail": "",
        }
    ]
    task_row = summary.task_rows()[0]
    assert task_row["task_id"] == archive.task_id
    assert task_row["source_id"] == SOURCE_ID
    assert len(task_row["scorer_package_sha256"]) == 64
    assert summary.rejection_rows()[0]["code"] == "NO_OBSERVABLE_OUTCOME"

    payload = summary.to_dict()
    assert payload["schema_version"] == "retro-taskset-build-v1"
    assert payload["published_task_ids"] == [archive.task_id]
    assert json.dumps(payload)  # Rich/JSON renderable


def test_build_taskset_is_resumable_across_invocations(archive: _Archive) -> None:
    first = archive.build()
    second = archive.build()
    assert second.build_id == first.build_id
    assert second.published_task_ids == first.published_task_ids
    assert "selected" in second.reused_stages
    assert f"{archive.task_id}:scorer_built" in second.reused_stages

    provenance = json.loads(
        (archive.paths.task_dir(archive.task_id) / "private" / "provenance.json").read_text()
    )
    assert provenance["build_id"] == first.build_id


def test_build_taskset_validates_its_arguments(archive: _Archive) -> None:
    with pytest.raises(BuildConfigurationError) as excinfo:
        build_taskset(archive.layout, NAME, archive.binary, None, archive.builder, archive.auditor)
    assert "--task-definer-agent is required" in str(excinfo.value)

    with pytest.raises(BuildConfigurationError) as excinfo:
        build_taskset(archive.layout, NAME, archive.binary, archive.definer, archive.builder)
    assert "--scorer-auditor-agent is required" in str(excinfo.value)

    with pytest.raises(BuildConfigurationError) as excinfo:
        build_taskset(
            archive.layout,
            NAME,
            archive.binary,
            archive.definer,
            archive.builder,
            archive.auditor,
            2,
        )
    assert "--adjacent-per-replay must be 0 or 1" in str(excinfo.value)

    with pytest.raises(BuildConfigurationError) as excinfo:
        build_taskset(
            archive.layout,
            NAME,
            archive.binary,
            archive.definer,
            archive.tmp_path / "missing.json",
            archive.auditor,
        )
    assert "--scorer-builder-agent does not exist" in str(excinfo.value)


def test_build_taskset_without_an_auditor_requires_opt_out(archive: _Archive) -> None:
    summary = build_taskset(
        archive.layout,
        NAME,
        archive.binary,
        archive.definer,
        archive.builder,
        None,
        0,
        require_audit=False,
        lint=accept_all_lint,
        repeatability_runs=1,
        ghostlab_env=archive.env,
    )
    assert summary.published_task_ids == (archive.task_id,)


def test_build_taskset_accepts_a_prepared_ghostlab_client(archive: _Archive) -> None:
    client = GhostlabCli(archive.binary, env=archive.env)
    summary = archive.build(ghostlab=client)
    assert summary.published == 1


def test_run_taskset_matches_the_cli_signature(archive: _Archive) -> None:
    archive.build()
    summary = archive.run(seeds="0,1,2")

    assert isinstance(summary, TasksetRunSummary)
    assert summary.agent_id == "candidate-good"
    assert summary.seeds == (0, 1, 2)
    assert summary.requested_attempts == 3
    assert summary.scored_attempts == 3
    assert summary.reused_attempts == 0
    assert summary.failed_attempts == 0
    assert summary.status_counts == {"scored": 3}
    assert summary.benchmark_score == pytest.approx(1.0)
    assert summary.pass_rate == pytest.approx(1.0)
    assert summary.coverage == pytest.approx(1.0)
    assert summary.results_path == archive.layout.benchmark_taskset_results_path(
        NAME, summary.eval_id
    )
    assert summary.eval_dir == archive.layout.benchmark_taskset_eval_dir(NAME, summary.eval_id)

    rows = summary.attempt_rows()
    assert len(rows) == 3
    assert rows[0]["task_id"] == archive.task_id
    assert rows[0]["status"] == "scored"
    assert rows[0]["score"] == pytest.approx(1.0)
    assert rows[0]["tokens"] == 1250
    assert summary.error_rows() == []
    assert summary.task_rows()[0]["agent_id"] == "candidate-good"
    assert json.dumps(summary.to_dict())


def test_run_taskset_preserves_hash_addressed_resume(archive: _Archive) -> None:
    archive.build()
    first = archive.run()
    second = archive.run(eval_id=first.eval_id)

    assert second.eval_id == first.eval_id
    assert second.reused_attempts == 1
    assert second.attempt_rows()[0]["attempt_id"] == first.attempt_rows()[0]["attempt_id"]

    forced = archive.run(eval_id=first.eval_id, force=True)
    assert forced.reused_attempts == 0
    assert forced.attempt_rows()[0]["attempt_id"] == first.attempt_rows()[0]["attempt_id"]


def test_consecutive_runs_share_the_latest_eval(archive: _Archive) -> None:
    archive.build()
    good = archive.run(archive.good)
    bad = archive.run(archive.bad)
    assert bad.eval_id == good.eval_id

    fresh = archive.run(archive.good, eval_id="new")
    assert fresh.eval_id != good.eval_id


def test_run_taskset_reports_failure_statuses_without_scores(archive: _Archive) -> None:
    archive.build()
    archive.plan["artifact_runs"]["candidate-good"]["status"] = "model_unavailable"
    write_plan(archive.plan_path, archive.plan)

    summary = archive.run(seeds=[0, 1])
    assert summary.scored_attempts == 0
    assert summary.failed_attempts == 2
    assert summary.status_counts == {"model_unavailable": 2}
    assert summary.benchmark_score is None
    assert summary.coverage == pytest.approx(0.0)
    assert len(summary.error_rows()) == 2
    assert all(row["score"] is None for row in summary.error_rows())


def test_run_taskset_validates_its_arguments(archive: _Archive) -> None:
    archive.build()
    with pytest.raises(TaskVerificationError) as excinfo:
        run_taskset(archive.layout, NAME, None, "0", archive.binary)
    assert "--agent is required" in str(excinfo.value)
    with pytest.raises(TaskVerificationError):
        archive.run(seeds="not-a-seed")
    with pytest.raises(TaskVerificationError):
        archive.run(eval_id="../escape")
    with pytest.raises(TaskVerificationError):
        archive.run(expected_agent_sha256="0" * 64)


def test_report_taskset_resolves_latest_and_compares_agents(archive: _Archive) -> None:
    archive.build()
    good = archive.run(archive.good, seeds="0,1")
    archive.run(archive.bad, seeds="0,1", eval_id=good.eval_id)

    summary = report_taskset(archive.layout, NAME, "latest")
    assert isinstance(summary, TasksetReportSummary)
    assert summary.eval_id == good.eval_id
    assert summary.results_path.is_file()

    agents = {row["agent_id"]: row for row in summary.agent_rows()}
    assert set(agents) == {"candidate-good", "candidate-bad"}
    assert agents["candidate-good"]["benchmark_score"] == pytest.approx(1.0)
    assert agents["candidate-bad"]["benchmark_score"] == pytest.approx(0.0)
    assert agents["candidate-good"]["scored_attempts"] == 2
    assert agents["candidate-good"]["valid_coverage"] == pytest.approx(1.0)
    assert agents["candidate-good"]["scorer_errors"] == 0

    sources = {(row["agent_id"], row["source_id"]) for row in summary.source_rows()}
    assert sources == {("candidate-good", SOURCE_ID), ("candidate-bad", SOURCE_ID)}
    assert {row["task_id"] for row in summary.task_rows()} == {archive.task_id}
    components = {
        (row["agent_id"], row["component_id"]): row["mean_value"]
        for row in summary.component_rows()
    }
    assert components[("candidate-good", "requested_behavior")] == pytest.approx(1.0)
    assert components[("candidate-bad", "requested_behavior")] == pytest.approx(0.0)

    resources = {row["agent_id"]: row for row in summary.resource_rows()}
    assert resources["candidate-good"]["tokens_mean"]["input"] == pytest.approx(1000.0)
    assert resources["candidate-good"]["cost_usd_total"] == pytest.approx(0.04)
    assert summary.error_rows()[0]["agent_error"] == 0
    assert json.dumps(summary.to_dict())


def test_report_taskset_accepts_budget_conditionals(archive: _Archive) -> None:
    archive.build()
    run = archive.run(seeds="0")
    summary = report_taskset(
        archive.layout,
        NAME,
        run.eval_id,
        token_budgets=(100.0, 100000.0),
        wall_time_budgets_ms=(1.0,),
    )
    budgets = {
        (row["dimension"], row["budget"]): row for row in summary.budget_rows()
    }
    assert budgets[("tokens", 100.0)]["score"] == pytest.approx(0.0)
    assert budgets[("tokens", 100000.0)]["score"] == pytest.approx(1.0)
    assert budgets[("wall_time_ms", 1.0)]["over_budget_attempts"] == 1


def test_report_taskset_explains_a_missing_eval(archive: _Archive) -> None:
    archive.build()
    with pytest.raises(TaskVerificationError) as excinfo:
        report_taskset(archive.layout, NAME, "latest")
    assert "no evals exist" in str(excinfo.value)

    with pytest.raises(TaskVerificationError):
        report_taskset(archive.layout, NAME, "../escape")


def test_resolve_run_eval_id_policy(archive: _Archive) -> None:
    assert resolve_run_eval_id(archive.paths, None).startswith("eval-")
    assert resolve_run_eval_id(archive.paths, "eval-x") == "eval-x"
    archive.paths.eval_dir("eval-a").mkdir(parents=True)
    archive.paths.eval_dir("eval-b").mkdir(parents=True)
    assert resolve_run_eval_id(archive.paths, None) == "eval-b"
    assert resolve_run_eval_id(archive.paths, "latest") == "eval-b"
    assert resolve_run_eval_id(archive.paths, "new").startswith("eval-")
    assert resolve_run_eval_id(archive.paths, "new") not in {"eval-a", "eval-b"}
    with pytest.raises(TaskVerificationError):
        resolve_run_eval_id(archive.paths, "bad id")


def test_full_command_sequence_end_to_end(archive: _Archive) -> None:
    """The spec section 20 acceptance sequence, minus select/bundle."""
    build = archive.build()
    assert build.published == 1

    first = run_taskset(archive.layout, NAME, archive.good, "0,1,2", archive.binary,
                        ghostlab_env=archive.env)
    second = run_taskset(archive.layout, NAME, archive.bad, "0,1,2", archive.binary,
                         ghostlab_env=archive.env)
    assert second.eval_id == first.eval_id

    report = report_taskset(archive.layout, NAME, "latest")
    scores = {row["agent_id"]: row["benchmark_score"] for row in report.agent_rows()}
    assert scores["candidate-good"] == pytest.approx(1.0)
    assert scores["candidate-bad"] == pytest.approx(0.0)

    # Re-running the unchanged build creates no new task or scorer version.
    rebuild = archive.build()
    assert rebuild.build_id == build.build_id
    assert rebuild.published_task_ids == build.published_task_ids
    replay = run_taskset(archive.layout, NAME, archive.good, "0,1,2", archive.binary,
                         eval_id=first.eval_id, ghostlab_env=archive.env)
    assert replay.reused_attempts == 3
