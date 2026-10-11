"""Durable experiment plans, verified inputs, recovery, and narrowly owned cleanup."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer import experiments
from retro.benchmarks.task_scorer.experiments import (
    EXPERIMENT_JOB_ARTIFACTS,
    ExperimentError,
    cleanup_experiment,
    create_experiment,
    list_experiments,
    load_experiment,
    run_experiment,
)
from retro.benchmarks.task_scorer.ghostlab_cli import sha256_file, sha256_json, write_json
from retro.benchmarks.task_scorer.run import AgentSpec
from retro.benchmarks.task_scorer.suite import CuratedSuite, curate_taskset
from retro.locking import LockUnavailableError, exclusive_lock
from retro.storage import Layout
from tests.test_task_scorer_suite import PublishedFixture, make_published_fixture


@pytest.fixture
def frozen(tmp_path: Path) -> tuple[PublishedFixture, CuratedSuite]:
    published = make_published_fixture(tmp_path)
    return published, curate_taskset(published.layout, "pilot", suite_id="personal-v1")


def plan(frozen: tuple[PublishedFixture, CuratedSuite], **overrides: Any) -> dict[str, Any]:
    published, suite = frozen
    values: dict[str, Any] = {
        "title": "Compare candidate changes",
        "hypothesis": "The candidate improves real held-out tasks",
        "datasets": [{"name": "pilot", "suite_id": suite.suite_id}],
        "agents": [AgentSpec.from_path(published.good_agent), AgentSpec.from_path(published.bad_agent)],
        "experiment_id": "trial-v1",
    }
    values.update(overrides)
    return create_experiment(published.layout, **values)


def job_state(job: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = Path(job["path"]) / "job.json"
    return path, json.loads(path.read_text())


def test_creation_is_planned_atomic_immutable_and_does_not_resolve_ghostlab(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, suite = frozen

    def no_execution(*_args, **_kwargs):
        pytest.fail("creating a job must never resolve or run Ghostlab")

    monkeypatch.setattr(experiments, "GhostlabCli", no_execution)
    job = plan(frozen, description="Explicit start required", seeds=(0, 1))
    assert job["status"] == "planned"
    assert job["hypothesis"] == "The candidate improves real held-out tasks"
    assert job["requested_attempts"] == 4
    assert job["summary"]["completed_attempts"] == 0
    assert job["datasets"][0]["task_ids"] == [published.task_id]
    assert job["datasets"][0]["suite_content_sha256"] == suite.content_sha256
    assert job["agents"][0]["config_sha256"] == sha256_file(published.good_agent)
    assert job["agents"][0]["model"] == "m-candidate"
    assert job["agents"][0]["runtime"] == {"backend": "opencode", "model": "m-candidate"}
    assert len({run["eval_id"] for run in job["runs"]}) == 1
    assert len({run["run_id"] for run in job["runs"]}) == 2
    assert all(not Path(run["eval_dir"]).exists() for run in job["runs"])
    setup_path = Path(job["path"]) / "setup.json"
    setup_bytes = setup_path.read_bytes()
    assert load_experiment(published.layout, "trial-v1")["setup_sha256"] == job["setup_sha256"]
    assert list_experiments(published.layout)[0]["experiment_id"] == "trial-v1"
    with pytest.raises(ExperimentError, match="immutable"):
        plan(frozen, title="Different hypothesis")
    assert setup_path.read_bytes() == setup_bytes
    assert not list((published.layout.root / "experiments").glob("*.creating-*"))


def test_foreign_legacy_experiment_namespace_is_not_reported_as_a_failed_job(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "archive")
    foreign = layout.root / "experiments" / "Mem"
    foreign.mkdir(parents=True)
    experiment = foreign / "Qwen-model-experiment.json"
    logs = foreign / "logs"
    logs.mkdir()
    log = logs / "Qwen-model.log"
    experiment.write_text('{"model": "Qwen", "legacy": true}\n')
    log.write_text("unrelated historical experiment output\n")
    assert list_experiments(layout) == []
    assert experiment.read_text() == '{"model": "Qwen", "legacy": true}\n'
    assert log.read_text() == "unrelated historical experiment output\n"
    assert not any((foreign / name).exists() for name in EXPERIMENT_JOB_ARTIFACTS)


@pytest.mark.parametrize("artifact", EXPERIMENT_JOB_ARTIFACTS)
def test_recognized_incomplete_jobs_remain_visible_beside_foreign_directories(
    tmp_path: Path, artifact: str,
) -> None:
    layout = Layout(tmp_path / "archive")
    foreign = layout.root / "experiments" / "Mem"
    foreign.mkdir(parents=True)
    (foreign / "Qwen.json").write_text("{}")
    recognized = foreign.parent / "incomplete-job"
    recognized.mkdir()
    (recognized / artifact).write_text("{}")
    jobs = list_experiments(layout)
    assert [job["experiment_id"] for job in jobs] == ["incomplete-job"]
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["error"]["code"] == "invalid_job"
    assert list(recognized.iterdir()) == [recognized / artifact]


def test_broken_symlinked_job_artifact_is_recognized_and_reported(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "archive")
    recognized = layout.root / "experiments" / "incomplete-job"
    recognized.mkdir(parents=True)
    (recognized / "job.json").symlink_to(recognized / "missing.json")
    jobs = list_experiments(layout)
    assert [job["experiment_id"] for job in jobs] == ["incomplete-job"]
    assert jobs[0]["status"] == "failed"


def test_datasets_share_cohorts_across_agents_but_not_across_jobs_or_datasets(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, suite = frozen
    datasets = [
        {"dataset_id": "first", "name": "pilot", "suite_id": suite.suite_id, "split": "test"},
        {"dataset_id": "second", "name": "pilot", "suite_id": suite.suite_id, "split": "test"},
    ]
    job = plan(frozen, datasets=datasets, seeds=(0, 1))
    another = plan(frozen, datasets=datasets, experiment_id="trial-v2", seeds=(0, 1))
    assert {run["eval_id"] for run in job["runs"]}.isdisjoint(
        run["eval_id"] for run in another["runs"]
    )
    assert len({run["eval_id"] for run in job["runs"]}) == 2
    setup_path = Path(job["path"]) / "setup.json"
    setup_hash = sha256_file(setup_path)
    frozen_prompt = suite.task(published.task_id).prompt_path
    prompt_hash = sha256_file(frozen_prompt)
    result = run_experiment(published.layout, "trial-v1", ghostlab=published.client())
    assert result["status"] == "completed"
    assert result["summary"]["requested_attempts"] == 8
    assert result["summary"]["scored_attempts"] == 8
    assert [run["benchmark_score"] for run in result["runs"]] == [1, 0, 1, 0]
    assert all(run["valid_coverage"] == 1 for run in result["runs"])
    for run in result["runs"]:
        cohort = json.loads((Path(run["eval_dir"]) / "cohort.json").read_text())
        assert cohort["suite_content_sha256"] == suite.content_sha256
        assert cohort["task_ids"] == [published.task_id]
        assert cohort["seeds"] == [0, 1]
        assert Path(run["results_path"]).is_file()
        for attempt in run["attempts"]:
            assert attempt["components"]
            metadata = json.loads(Path(attempt["metadata_path"]).read_text())
            assert metadata["experiment_id"] == "trial-v1"
            assert metadata["experiment_job_id"] == "trial-v1"
            assert metadata["tags"] == ["experiment", "experiment/trial-v1"]
            assert metadata["execution_prompt_sha256"] != prompt_hash
            assert "#retro:tag=experiment/trial-v1" in (
                Path(attempt["attempt_path"]).parent / "prompt.txt"
            ).read_text()
            assert not (Path(attempt["attempt_path"]).parent / "workspace").exists()
            assert not (Path(attempt["attempt_path"]).parent / ".runtime-inputs").exists()
    repeated = run_experiment(published.layout, "trial-v1", ghostlab=published.binary.parent / "missing")
    assert repeated == result
    assert sha256_file(setup_path) == setup_hash
    assert sha256_file(frozen_prompt) == prompt_hash
    assert load_experiment(published.layout, "trial-v2")["status"] == "planned"


@pytest.mark.parametrize("damage", ["config", "instruction", "mcp"])
def test_changed_pinned_agent_inputs_fail_visibly_before_any_attempt(
    frozen: tuple[PublishedFixture, CuratedSuite], damage: str,
) -> None:
    published, _suite = frozen
    payload = json.loads(published.good_agent.read_text())
    instruction = published.good_agent.parent / "candidate.md"
    instruction.write_text("Original pinned instruction\n")
    payload["runtime"]["instructions"] = ["candidate.md"]
    dependency = published.good_agent.parent / "mcp" / "server.py"
    dependency.parent.mkdir()
    dependency.write_text("print('first')\n")
    mcp = published.good_agent.parent / "mcp.json"
    write_json(mcp, {"mcpServers": {"example": {"command": "python3", "args": [str(dependency)]}}})
    payload["inputs"]["mcps"] = [{"name": "example", "config_ref": "mcp.json", "server": "example"}]
    write_json(published.good_agent, payload)
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    assert job["agents"][0]["asset_sha256"]["mcp.json"]
    if damage == "config":
        payload["runtime"]["model"] = "a newer model"
        write_json(published.good_agent, payload)
    elif damage == "instruction":
        instruction.write_text("Changed pinned instruction\n")
    else:
        dependency.write_text("print('newer MCP server')\n")
    failed = run_experiment(published.layout, "trial-v1", ghostlab=published.client())
    assert failed["status"] == "failed"
    assert "changed" in failed["error"]["message"] or "expected" in failed["error"]["message"]
    assert failed["summary"]["completed_attempts"] == 0
    assert all(not Path(run["eval_dir"]).exists() for run in failed["runs"])
    assert load_experiment(published.layout, "trial-v1")["error"] == failed["error"]


def test_frozen_dataset_mutation_is_not_silently_rebound(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, suite = frozen
    job = plan(frozen)
    manifest_path = suite.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = "2099-01-01T00:00:00Z"
    manifest["content_sha256"] = sha256_json(
        {key: value for key, value in manifest.items() if key != "content_sha256"}
    )
    write_json(manifest_path, manifest)
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "failed"
    assert "suite changed" in result["error"]["message"]
    assert result["summary"]["completed_attempts"] == 0


def test_damaged_frozen_task_cannot_execute_before_any_candidate_starts(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, suite = frozen
    job = plan(frozen)
    suite.task(published.task_id).prompt_path.write_text("mutated frozen prompt")
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "failed"
    assert "changed after publication" in result["error"]["message"]
    assert result["summary"]["completed_attempts"] == 0


def test_active_task_changes_do_not_change_the_frozen_dataset(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    (published.paths.task_dir(published.task_id) / "public" / "prompt.txt").write_text("new active task")
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "completed"
    assert result["runs"][0]["benchmark_score"] == 1


@pytest.mark.parametrize("failure", ["scorer_error", "harness_crash"])
def test_grading_failures_finish_jobs_without_manufacturing_zero_scores(
    frozen: tuple[PublishedFixture, CuratedSuite], failure: str,
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    payload = json.loads(published.plan_path.read_text())
    payload["scorer"] = {"force_status": "scorer_error"} if failure == "scorer_error" else {"no_report": True}
    write_json(published.plan_path, payload)
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "completed"
    assert result["error"] is None
    run = result["runs"][0]
    expected = "scorer_error" if failure == "scorer_error" else "harness_error"
    assert run["status"] == "completed"
    assert run["benchmark_score"] is None
    assert run["valid_coverage"] == 0
    assert run["status_counts"] == {expected: 1}
    assert run["attempts"][0]["score"] is None
    assert run["attempts"][0]["error"]
    assert Path(run["results_path"]).is_file()
    assert load_experiment(published.layout, job["experiment_id"])["status"] == "completed"


def test_orchestration_error_is_persisted_separately_from_grading_errors(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen)

    def broken_orchestration(*_args, **_kwargs):
        raise RuntimeError("controller execution unavailable")

    monkeypatch.setattr(experiments, "run_taskset", broken_orchestration)
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "failed"
    assert result["error"]["code"] == "orchestration_error"
    assert result["runs"][0]["error"]["message"] == "controller execution unavailable"
    assert result["summary"]["completed_attempts"] == 0
    assert load_experiment(published.layout, job["experiment_id"])["status"] == "failed"


def test_orphaned_running_job_is_recovered_and_can_be_explicitly_resumed(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    state_path, state = job_state(job)
    state["status"] = "running"
    state["worker"] = {"pid": 999999999}
    state["runs"][0]["status"] = "running"
    write_json(state_path, state)
    recovered = list_experiments(published.layout)[0]
    assert recovered["status"] == "interrupted"
    assert recovered["runs"][0]["status"] == "interrupted"
    assert recovered["worker"] is None
    assert json.loads(state_path.read_text())["status"] == "interrupted"
    resumed = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert resumed["status"] == "completed"


def test_keyboard_interrupt_before_candidate_checkpoint_retains_owned_outputs_and_cleans_inputs(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    client = published.client()
    artifact = client.artifact_run

    def interrupted_artifact(request):
        artifact(request)
        raise KeyboardInterrupt("after export, before local checkpoint")

    monkeypatch.setattr(client, "artifact_run", interrupted_artifact)
    stopped = run_experiment(published.layout, job["experiment_id"], ghostlab=client)
    assert stopped["status"] == "interrupted"
    run = stopped["runs"][0]
    directory = published.paths.attempt_dir(run["eval_id"], published.task_id, "candidate-good", 0)
    assert not (directory / "workspace").exists()
    assert not (directory / ".runtime-inputs").exists()
    assert (directory / "agent" / "candidate-state.tar.zst").is_file()

    def no_execution(_request):
        pytest.fail("published candidate export must survive interruption before checkpoint")

    monkeypatch.setattr(client, "artifact_run", no_execution)
    resumed = run_experiment(published.layout, job["experiment_id"], ghostlab=client)
    assert resumed["status"] == "completed"
    assert resumed["summary"]["scored_attempts"] == 1
    assert (directory / "agent" / "invocations.log").read_text().splitlines() == ["candidate-good"]


def test_crash_after_all_attempts_recovers_completion_without_launching_again(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    done = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    state_path, state = job_state(done)
    state["status"] = "running"
    state["runs"][0]["status"] = "running"
    state["summary"] = experiments._summary(state)
    write_json(state_path, state)
    recovered = load_experiment(published.layout, job["experiment_id"])
    assert recovered["status"] == "completed"
    assert recovered["summary"]["scored_attempts"] == 1

    def no_execution(*_args, **_kwargs):
        pytest.fail("completed crash recovery must not invoke Ghostlab")

    monkeypatch.setattr(experiments, "GhostlabCli", no_execution)
    assert run_experiment(published.layout, job["experiment_id"])["status"] == "completed"


def test_interrupt_preserves_attempts_and_resume_reuses_them(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    runner = experiments.run_taskset

    def interrupt_after_attempts(*args, **kwargs):
        runner(*args, **kwargs)
        raise KeyboardInterrupt("worker interrupted after persisting the first agent")

    monkeypatch.setattr(experiments, "run_taskset", interrupt_after_attempts)
    stopped = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert stopped["status"] == "interrupted"
    assert stopped["summary"]["completed_attempts"] == 1
    first_attempt = Path(stopped["runs"][0]["attempts"][0]["attempt_path"])
    digest = sha256_file(first_attempt)
    monkeypatch.setattr(experiments, "run_taskset", runner)
    resumed = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert resumed["status"] == "completed"
    assert resumed["runs"][0]["reused_attempts"] == 1
    assert sha256_file(first_attempt) == digest
    assert (first_attempt.parent / "agent" / "invocations.log").read_text().splitlines() == ["candidate-good"]


def test_live_worker_lock_prevents_start_cleanup_and_false_crash_recovery(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    state_path, state = job_state(job)
    state["status"] = "running"
    write_json(state_path, state)
    with exclusive_lock(Path(job["path"]) / ".job.lock"):
        assert load_experiment(published.layout, job["experiment_id"])["status"] == "running"
        with pytest.raises(LockUnavailableError):
            run_experiment(published.layout, job["experiment_id"])
        with pytest.raises(LockUnavailableError):
            cleanup_experiment(published.layout, job["experiment_id"], prune_artifacts=True)
    assert json.loads(state_path.read_text())["status"] == "running"


def test_job_lock_is_cross_process(frozen: tuple[PublishedFixture, CuratedSuite]) -> None:
    published, _suite = frozen
    job = plan(frozen)
    code = (
        "import sys\nfrom pathlib import Path\nfrom retro.locking import exclusive_lock\n"
        "with exclusive_lock(Path(sys.argv[1])):\n"
        " print('ready', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(Path(job["path"]) / ".job.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        with pytest.raises(LockUnavailableError):
            run_experiment(published.layout, job["experiment_id"])
        child.communicate("\n", timeout=10)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_finished_cleanup_prunes_only_exact_candidate_exports_and_preserves_evidence(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, suite = frozen
    job = plan(frozen)
    done = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    evidence: dict[Path, str] = {}
    candidates: list[Path] = []
    for run in done["runs"]:
        for row in run["attempts"]:
            directory = Path(row["attempt_path"]).parent
            for name in (
                "attempt.json", "score-report.json", "candidate-phase.json", "attempt-metadata.json",
            ):
                evidence[directory / name] = sha256_file(directory / name)
            for name in ("agent/events.jsonl", "scorer/scorer-run.json", "agent/artifact-run.json"):
                evidence[directory / name] = sha256_file(directory / name)
            for name in ("score-input.json", "task.json", "resources.json"):
                path = directory / "scorer" / "scorer-staging" / "input" / name
                write_json(path, {"retained": name})
                evidence[path] = sha256_file(path)
            (directory / "agent" / "unrelated.tar.zst").write_text("not a requested candidate export")
            candidates.extend(Path(export["path"]) for export in row["candidate_exports"])
    frozen_prompt = suite.task(published.task_id).prompt_path
    evidence[frozen_prompt] = sha256_file(frozen_prompt)
    cleaned = cleanup_experiment(published.layout, job["experiment_id"], prune_artifacts=True)
    assert cleaned["status"] == "completed"
    assert cleaned["artifacts_pruned"] is True
    assert cleaned["cleanup"]["errors"] == []
    assert all(not path.exists() for path in candidates)
    assert all(sha256_file(path) == digest for path, digest in evidence.items())
    assert all((path.parent / "unrelated.tar.zst").is_file() for path in candidates)
    again = cleanup_experiment(published.layout, job["experiment_id"], prune_artifacts=True)
    assert again["cleanup"]["removed"] == []
    assert len(again["cleanup_history"]) == 2


@pytest.mark.parametrize("damage", ["escape", "symlink", "changed", "permission"])
def test_cleanup_refuses_unowned_changed_or_undeletable_exports_and_reports_errors(
    frozen: tuple[PublishedFixture, CuratedSuite], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    done = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    row = done["runs"][0]["attempts"][0]
    metadata_path = Path(row["metadata_path"])
    metadata = json.loads(metadata_path.read_text())
    candidate = Path(metadata["candidate_exports"][0]["path"])
    witness = tmp_path / "unowned.txt"
    witness.write_text("preserve me")
    if damage == "escape":
        metadata["candidate_exports"][0]["path"] = str(witness)
        write_json(metadata_path, metadata)
    elif damage == "symlink":
        candidate.unlink()
        candidate.symlink_to(witness)
    elif damage == "changed":
        candidate.write_text("replaced after execution")
    else:
        unlink = Path.unlink

        def denied(path, *args, **kwargs):
            if path == candidate:
                raise PermissionError("candidate is locked")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", denied)
    result = cleanup_experiment(published.layout, job["experiment_id"], prune_artifacts=True)
    assert result["cleanup"]["errors"]
    assert result["status"] == "completed"
    assert witness.read_text() == "preserve me"
    assert candidate.exists()
    assert load_experiment(published.layout, job["experiment_id"])["cleanup"]["errors"]


def test_tampered_setup_and_result_paths_are_visible_not_silently_ignored(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    state_path, state = job_state(job)
    state["runs"][0]["results_path"] = str(published.layout.root / "raw" / "not-an-evaluation.json")
    write_json(state_path, state)
    with pytest.raises(ExperimentError, match="immutable plan"):
        load_experiment(published.layout, job["experiment_id"])
    assert list_experiments(published.layout)[0]["error"]["code"] == "invalid_job"


def test_changed_ghostlab_resume_fails_without_overwriting_previous_attempts(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    runner = experiments.run_taskset

    def stop_after_first(*args, **kwargs):
        runner(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(experiments, "run_taskset", stop_after_first)
    stopped = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    attempt_path = Path(stopped["runs"][0]["attempts"][0]["attempt_path"])
    digest = sha256_file(attempt_path)
    published.binary.write_text(published.binary.read_text() + "\n# changed runner\n")
    monkeypatch.setattr(experiments, "run_taskset", runner)
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "failed"
    assert "Ghostlab changed" in result["error"]["message"]
    assert sha256_file(attempt_path) == digest


def test_finished_attempt_damage_fails_resume_without_discarding_prior_evidence(
    frozen: tuple[PublishedFixture, CuratedSuite], monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    runner = experiments.run_taskset

    def stop_after_first(*args, **kwargs):
        runner(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(experiments, "run_taskset", stop_after_first)
    stopped = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    attempt_path = Path(stopped["runs"][0]["attempts"][0]["attempt_path"])
    payload = json.loads(attempt_path.read_text())
    payload["score"] = 0.0
    payload["passed"] = False
    write_json(attempt_path, payload)
    damaged_hash = sha256_file(attempt_path)
    monkeypatch.setattr(experiments, "run_taskset", runner)
    result = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    assert result["status"] == "failed"
    assert "invalid" in result["error"]["message"]
    assert sha256_file(attempt_path) == damaged_hash


def test_changed_cohort_is_not_accepted_during_crash_recovery(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    done = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    cohort_path = Path(done["runs"][0]["eval_dir"]) / "cohort.json"
    cohort = json.loads(cohort_path.read_text())
    cohort["suite_content_sha256"] = "0" * 64
    write_json(cohort_path, cohort)
    state_path, state = job_state(done)
    state["status"] = "running"
    state["runs"][0]["status"] = "running"
    state["summary"] = experiments._summary(state)
    write_json(state_path, state)
    recovered = load_experiment(published.layout, job["experiment_id"])
    assert recovered["status"] == "interrupted"
    assert "immutable dataset pin" in recovered["runs"][0]["error"]["message"]


def test_frozen_labels_do_not_persist_secrets_environment_or_runner_arguments(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    payload = json.loads(published.good_agent.read_text())
    payload["runtime"]["env"] = {"PRIVATE_TOKEN": "never-persist-this"}
    payload["runner"] = {"executable": "/opt/my-runner", "args": ["--token", "never-persist-this"]}
    write_json(published.good_agent, payload)
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    assert job["agents"][0]["runtime"]["runner"] == "my-runner"
    setup = (Path(job["path"]) / "setup.json").read_text()
    assert "never-persist-this" not in setup
    assert "--token" not in setup
    assert "PRIVATE_TOKEN" not in setup


def test_frozen_process_labels_bind_only_safe_named_argv_fields(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    payload = json.loads(published.good_agent.read_text())
    payload.pop("runtime")
    payload["runner"] = {
        "command": [
            "python3", "/opt/agents/native-agent.py", "--model", "mai-code-1.1",
            "--api-key", "never-persist-this", "--endpoint", "https://private.example",
        ],
        "parser": "copilot-json",
    }
    write_json(published.good_agent, payload)
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    assert job["agents"][0]["model"] == "mai-code-1.1"
    assert job["agents"][0]["runtime"] == {
        "model": "mai-code-1.1", "backend": "openshell", "parser": "copilot-json",
        "runner": "python3", "script": "native-agent.py",
    }
    setup = (Path(job["path"]) / "setup.json").read_text()
    assert "never-persist-this" not in setup
    assert "private.example" not in setup
    assert "--api-key" not in setup

def test_dataset_and_job_symlinks_are_rejected_without_touching_targets(
    frozen: tuple[PublishedFixture, CuratedSuite], tmp_path: Path,
) -> None:
    published, suite = frozen
    suite_target = tmp_path / "relocated-suite"
    suite.path.rename(suite_target)
    suite.path.symlink_to(suite_target, target_is_directory=True)
    with pytest.raises(ExperimentError, match="symlink"):
        plan(frozen)
    assert suite_target.is_dir()
    suite.path.unlink()
    suite_target.rename(suite.path)
    job = plan(frozen)
    target = tmp_path / "relocated-job"
    Path(job["path"]).rename(target)
    Path(job["path"]).symlink_to(target, target_is_directory=True)
    with pytest.raises(ExperimentError, match="symlink"):
        load_experiment(published.layout, job["experiment_id"])
    with pytest.raises(ExperimentError, match="symlink"):
        cleanup_experiment(published.layout, job["experiment_id"])
    assert (target / "setup.json").is_file()


def test_cleanup_respects_a_live_attempt_lock(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    done = run_experiment(published.layout, job["experiment_id"], ghostlab=published.client())
    row = done["runs"][0]["attempts"][0]
    candidate = Path(row["candidate_exports"][0]["path"])
    with exclusive_lock(Path(row["attempt_path"]).parent / ".attempt.lock"):
        result = cleanup_experiment(published.layout, job["experiment_id"], prune_artifacts=True)
    assert result["cleanup"]["errors"]
    assert candidate.is_file()


def test_setup_mutation_is_visible_and_planned_job_cleanup_is_refused(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, _suite = frozen
    job = plan(frozen)
    with pytest.raises(ExperimentError, match="finished"):
        cleanup_experiment(published.layout, job["experiment_id"])
    setup_path = Path(job["path"]) / "setup.json"
    setup = json.loads(setup_path.read_text())
    setup["title"] = "silently changed setup"
    write_json(setup_path, setup)
    with pytest.raises(ExperimentError, match="immutable content hash"):
        load_experiment(published.layout, job["experiment_id"])
    assert list_experiments(published.layout)[0]["status"] == "failed"


@pytest.mark.parametrize("identity", ["../escape", "bad/id", "", "x" * 54])
def test_unsafe_experiment_ids_are_rejected_before_writes(tmp_path: Path, identity: str) -> None:
    layout = Layout(tmp_path / "archive")
    with pytest.raises(ExperimentError, match="path-safe"):
        create_experiment(layout, title="test", datasets=[], agents=[], experiment_id=identity)
    assert not layout.root.exists()


def test_runtime_has_no_unexplained_attempt_cap(frozen: tuple[PublishedFixture, CuratedSuite]) -> None:
    published, _suite = frozen
    job = plan(frozen, agents=[AgentSpec.from_path(published.good_agent)], seeds=tuple(range(101)))
    assert job["requested_attempts"] == 101
    assert job["status"] == "planned"


def test_empty_split_missing_assets_and_invalid_seeds_cannot_be_planned(
    frozen: tuple[PublishedFixture, CuratedSuite],
) -> None:
    published, suite = frozen
    with pytest.raises(ExperimentError, match="frozen split"):
        plan(frozen, datasets=[{"name": "pilot", "suite_id": suite.suite_id, "split": "train"}])
    with pytest.raises(ExperimentError, match="seeds"):
        plan(frozen, seeds=(True,))
    payload = json.loads(published.good_agent.read_text())
    payload["runtime"]["instructions"] = ["missing.md"]
    write_json(published.good_agent, payload)
    with pytest.raises(ExperimentError, match="missing"):
        plan(frozen, agents=[AgentSpec.from_path(published.good_agent)])
    assert not (published.layout.root / "experiments").exists()
