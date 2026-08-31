"""Tests for the public Ghostlab subprocess adapter."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer.ghostlab_cli import (
    ArtifactRunRequest,
    CommandOutcome,
    ExportSpec,
    GhostlabBinaryError,
    GhostlabCli,
    GhostlabContractError,
    GhostlabInvocationError,
    GhostlabTimeoutError,
    ScorerRunRequest,
    resolve_ghostlab_binary,
    sha256_path,
    tree_manifest,
    validate_scorer_run_attestation,
)
from tests.task_scorer_harness import (
    make_agent_config,
    make_definer_outputs,
    make_scorer_package,
    make_source_bundle,
    write_fake_ghostlab,
    write_json,
    write_plan,
    write_text,
)


@pytest.fixture()
def ghostlab_bin(tmp_path: Path) -> Path:
    return write_fake_ghostlab(tmp_path / "bin")


def _client(ghostlab_bin: Path, plan_path: Path) -> GhostlabCli:
    return GhostlabCli(ghostlab_bin, env={"FAKE_GHOSTLAB_PLAN": str(plan_path)})


def _definer_request(tmp_path: Path, source_root: Path, run_dir: Path) -> ArtifactRunRequest:
    agent = make_agent_config(
        tmp_path / "agents" / "definer.json", "retro-task-definer-v1", "definer-model"
    )
    prompt = write_text(tmp_path / "prompt.md", "Build task definitions.\n")
    return ArtifactRunRequest(
        agent_config=agent,
        workspace=source_root,
        prompt_file=prompt,
        run_dir=run_dir,
        exports=(ExportSpec("/sandbox/output/task-definitions.json", "task-definitions.json"),),
    )


def test_resolve_binary_prefers_explicit_then_env(tmp_path: Path, ghostlab_bin: Path) -> None:
    assert resolve_ghostlab_binary(ghostlab_bin) == str(ghostlab_bin)
    assert resolve_ghostlab_binary(env={"RETRO_GHOSTLAB_BIN": str(ghostlab_bin)}) == str(ghostlab_bin)
    with pytest.raises(GhostlabBinaryError) as excinfo:
        resolve_ghostlab_binary(tmp_path / "nope" / "ghostlab", env={})
    assert "--ghostlab-bin" in str(excinfo.value)


def test_version_is_captured_and_cached(tmp_path: Path, ghostlab_bin: Path) -> None:
    client = _client(ghostlab_bin, write_plan(tmp_path / "plan.json", {}))
    version = client.version()
    assert version.raw == "ghostlab 9.9.9-fake"
    assert version.version == "9.9.9-fake"
    assert version.binary_sha256
    assert client.version() is version


def test_version_failure_is_actionable(tmp_path: Path) -> None:
    broken = tmp_path / "bin" / "ghostlab"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 7\n")
    broken.chmod(0o755)
    client = GhostlabCli(broken)
    with pytest.raises(GhostlabBinaryError) as excinfo:
        client.version()
    message = str(excinfo.value)
    assert "exit code: 7" in message
    assert "artifact-run" in message


def test_artifact_run_records_hashes_and_exports(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"outputs": str(outputs)}}},
    )
    client = _client(ghostlab_bin, plan)
    request = _definer_request(tmp_path, source_root, tmp_path / "run")
    result = client.artifact_run(request)

    assert result.status == "completed"
    assert result.completed
    assert not result.workspace_mutated
    assert result.export_path("task-definitions.json") == tmp_path / "run" / "task-definitions.json"
    assert result.export_sha256["task-definitions.json"]
    assert result.agent_config_sha256 and result.prompt_sha256
    assert result.model == "definer-model"
    assert result.events_path == tmp_path / "run" / "events.jsonl"

    record = result.to_record()
    assert record["ghostlab"]["version"] == "9.9.9-fake"
    assert record["input_sha256"] == result.input_sha256

    second = client.artifact_run(
        _definer_request(tmp_path, source_root, tmp_path / "run-again")
    )
    assert second.input_sha256 == result.input_sha256
    assert second.output_sha256 == result.output_sha256


def test_artifact_run_fingerprint_and_argv_include_environment_and_settings(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"outputs": str(outputs)}}},
    )
    client = _client(ghostlab_bin, plan)
    request = replace(
        _definer_request(tmp_path, source_root, tmp_path / "run"),
        sandbox_image="demo@sha256:" + "d" * 64,
        setup_commands=(("python3", "-m", "venv", ".venv"),),
        timeout_seconds=30.0,
    )
    result = client.artifact_run(request)
    report = json.loads(result.report_path.read_text())
    assert report["sandbox_image"] == request.sandbox_image
    assert report["setup_commands"] == [["python3", "-m", "venv", ".venv"]]

    timeout_changed = client.artifact_run(
        replace(request, run_dir=tmp_path / "run-timeout", timeout_seconds=31.0)
    )
    export_changed = client.artifact_run(
        replace(request, run_dir=tmp_path / "run-export", export_workspace="state.tar.zst")
    )
    assert timeout_changed.input_sha256 != result.input_sha256
    assert export_changed.input_sha256 != result.input_sha256


def test_artifact_run_accepts_the_documented_workspace_archive_fallback(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "workspace_export_name": "state.tar.gz",
                }
            }
        },
    )
    request = replace(
        _definer_request(tmp_path, source_root, tmp_path / "run"),
        exports=(),
        export_workspace="state.tar.zst",
    )

    result = _client(ghostlab_bin, plan).artifact_run(request)

    assert request.workspace_exports() == ("state.tar.zst", "state.tar.gz")
    assert result.export_path("state.tar.gz") == tmp_path / "run" / "state.tar.gz"


def test_artifact_run_detects_source_mutation(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {"outputs": str(outputs), "mutate_source": True}
            }
        },
    )
    result = _client(ghostlab_bin, plan).artifact_run(
        _definer_request(tmp_path, source_root, tmp_path / "run")
    )
    assert result.workspace_mutated
    assert result.workspace_input_sha256 != result.workspace_output_sha256


def test_completed_artifact_run_requires_both_workspace_hashes(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "outputs": str(outputs),
                    "omit_report_fields": ["workspace_output_sha256"],
                }
            }
        },
    )

    with pytest.raises(GhostlabContractError, match="missing required workspace hashes"):
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )


def test_artifact_run_rejects_report_bound_to_different_inputs(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "outputs": str(outputs),
                    "report_overrides": {"prompt_sha256": "0" * 64},
                }
            }
        },
    )

    with pytest.raises(GhostlabContractError, match="does not match the current input hash"):
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )


def test_artifact_run_rejects_mutation_of_the_host_workspace(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "outputs": str(outputs),
                    "mutate_host": True,
                }
            }
        },
    )

    with pytest.raises(GhostlabContractError, match="caller's workspace"):
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )


def test_artifact_run_rejects_undeclared_export(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "outputs": str(outputs),
                    "extra_exports": [{"path": "oracle.patch", "sha256": "deadbeef"}],
                }
            }
        },
    )
    with pytest.raises(GhostlabContractError) as excinfo:
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )
    assert "undeclared artifacts: oracle.patch" in str(excinfo.value)


def test_artifact_run_rejects_wrong_schema(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {
            "artifact_runs": {
                "retro-task-definer-v1": {
                    "outputs": str(outputs),
                    "schema_version": "ghostlab-artifact-run-v0",
                }
            }
        },
    )
    with pytest.raises(GhostlabContractError) as excinfo:
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )
    assert "ghostlab-artifact-run-v1" in str(excinfo.value)


def test_artifact_run_rejects_unknown_status(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"outputs": str(outputs), "status": "weird"}}},
    )
    with pytest.raises(GhostlabContractError) as excinfo:
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )
    assert "unsupported status" in str(excinfo.value)


def test_artifact_run_missing_report_is_an_invocation_error(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    plan = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"no_report": True}}},
    )
    with pytest.raises(GhostlabInvocationError) as excinfo:
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )
    message = str(excinfo.value)
    assert "wrote no artifact-run.json" in message
    assert "exit code: 3" in message
    assert "run dir:" in message


def test_artifact_run_rerun_clears_stale_report_and_exports(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    outputs = make_definer_outputs(tmp_path / "outputs", source_root.name)
    plan_path = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"outputs": str(outputs)}}},
    )
    request = _definer_request(tmp_path, source_root, tmp_path / "run")
    client = _client(ghostlab_bin, plan_path)
    first = client.artifact_run(request)
    stale_export = first.export_path("task-definitions.json")
    assert stale_export is not None and stale_export.is_file()

    write_plan(
        plan_path,
        {"artifact_runs": {"retro-task-definer-v1": {"no_report": True}}},
    )
    with pytest.raises(GhostlabInvocationError, match="wrote no artifact-run.json"):
        client.artifact_run(request)

    assert not first.report_path.exists()
    assert not stale_export.exists()


def test_artifact_run_missing_required_export_is_a_contract_error(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    empty = tmp_path / "empty-outputs"
    empty.mkdir()
    plan = write_plan(
        tmp_path / "plan.json",
        {"artifact_runs": {"retro-task-definer-v1": {"outputs": str(empty)}}},
    )
    with pytest.raises(GhostlabContractError) as excinfo:
        _client(ghostlab_bin, plan).artifact_run(
            _definer_request(tmp_path, source_root, tmp_path / "run")
        )
    assert "required export 'task-definitions.json'" in str(excinfo.value)


def test_adapter_timeout_raises_timeout_error(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    calls: list[list[str]] = []
    run_dir = tmp_path / "run"
    write_json(run_dir / "artifact-run.json", {"stale": True})
    write_text(run_dir / "task-definitions.json", "stale\n")

    def runner(argv, timeout, env, cwd):  # noqa: ANN001 - test double
        calls.append(list(argv))
        if argv[1:2] == ["--version"]:
            return CommandOutcome(tuple(argv), 0, "ghostlab 1.0\n", "", 1, False)
        return CommandOutcome(tuple(argv), 124, "", "deadline exceeded", 10, True)

    client = GhostlabCli(ghostlab_bin, runner=runner)
    with pytest.raises(GhostlabTimeoutError) as excinfo:
        client.artifact_run(_definer_request(tmp_path, source_root, run_dir))
    assert "adapter deadline" in str(excinfo.value)
    assert calls[-1][1] == "artifact-run"
    assert not (run_dir / "artifact-run.json").exists()
    assert not (run_dir / "task-definitions.json").exists()


def test_scorer_run_executes_the_package_and_validates(tmp_path: Path, ghostlab_bin: Path) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(
        tmp_path / "task.json",
        {"schema_version": "retro-benchmark-task-v1", "task_id": "task123", "prompt": "x"},
    )
    plan = write_plan(tmp_path / "plan.json", {})
    client = _client(ghostlab_bin, plan)

    result = client.scorer_run(
        ScorerRunRequest(
            task_path=task,
            scorer_path=scorer / "scorer.json",
            candidate_path=source_root / "repo" / "outcome",
            output_path=tmp_path / "reports" / "oracle.json",
            attempt_id="oracle-attempt",
            seed=0,
        )
    )
    assert result.scored
    assert result.score_total == pytest.approx(1.0)
    assert result.passed is True
    assert result.component_value("requested_behavior") == pytest.approx(1.0)

    base = client.scorer_run(
        ScorerRunRequest(
            task_path=task,
            scorer_path=scorer / "scorer.json",
            candidate_path=source_root / "repo" / "base",
            output_path=tmp_path / "reports" / "base.json",
            attempt_id="base-attempt",
            seed=0,
        )
    )
    assert base.score_total == pytest.approx(0.0)
    assert base.passed is False
    assert base.input_sha256 != result.input_sha256


def test_scorer_run_uses_the_merged_ghostlab_cli_contract(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    resources = write_json(tmp_path / "resources.json", {"attempt_id": "ignored"})
    output = tmp_path / "report.json"
    calls: list[list[str]] = []

    def runner(argv, timeout, env, cwd):  # noqa: ANN001 - test double
        calls.append(list(argv))
        if argv[1:2] == ["--version"]:
            return CommandOutcome(tuple(argv), 0, "ghostlab 1.0\n", "", 1, False)
        write_json(
            output,
            {
                "schema_version": "retro-score-report-v1",
                "task_id": "task123",
                "attempt_id": "retro-attempt-1",
                "status": "scored",
                "score_total": 1.0,
                "passed": True,
                "valid": True,
                "pass_threshold": 0.8,
                "unscored_weight": 0.0,
                "components": [
                    {
                        "id": "requested_behavior",
                        "value": 1.0,
                        "weight": 1.0,
                        "hard_gate": True,
                        "gate_passed": True,
                        "evidence": [],
                    }
                ],
                "hard_gate_failures": [],
                "commands": [],
                "judge": None,
                "warnings": [],
                "scorer_package_sha256": "0" * 64,
                "duration_ms": 1,
            },
        )
        return CommandOutcome(tuple(argv), 0, "", "", 5, False)

    GhostlabCli(ghostlab_bin, runner=runner).scorer_run(
        ScorerRunRequest(
            task_path=task,
            scorer_path=scorer / "scorer.json",
            candidate_path=candidate,
            output_path=output,
            attempt_id="retro-attempt-1",
            resource_usage_path=resources,
        )
    )

    argv = calls[-1]
    assert argv[argv.index("--attempt-id") + 1] == "retro-attempt-1"
    assert argv[argv.index("--resources") + 1] == str(resources)
    assert "--resource-usage" not in argv
    assert argv[argv.index("--run-dir") + 1] == str(output.parent)


def test_scorer_run_failure_status_carries_no_score(tmp_path: Path, ghostlab_bin: Path) -> None:
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    plan = write_plan(tmp_path / "plan.json", {"scorer": {"force_status": "scorer_error"}})
    result = _client(ghostlab_bin, plan).scorer_run(
        ScorerRunRequest(
            task_path=task,
            scorer_path=scorer / "scorer.json",
            candidate_path=candidate,
            output_path=tmp_path / "report.json",
            attempt_id="a1",
        )
    )
    assert result.status == "scorer_error"
    assert result.score_total is None
    assert result.scored is False


def test_scorer_run_rejects_numeric_zero_for_a_failed_scorer(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output = tmp_path / "report.json"

    def runner(argv, timeout, env, cwd):  # noqa: ANN001 - test double
        if argv[1:2] == ["--version"]:
            return CommandOutcome(tuple(argv), 0, "ghostlab 1.0\n", "", 1, False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema_version": "retro-score-report-v1",
                    "task_id": "task123",
                    "attempt_id": "a1",
                    "status": "scorer_error",
                    "valid": False,
                    "score_total": 0.0,
                    "components": [],
                    "hard_gate_failures": [],
                    "commands": [],
                    "judge": None,
                    "warnings": [],
                    "scorer_package_sha256": "0" * 64,
                    "duration_ms": 1,
                }
            )
        )
        return CommandOutcome(tuple(argv), 0, "", "", 5, False)

    client = GhostlabCli(ghostlab_bin, runner=runner)
    with pytest.raises(GhostlabContractError) as excinfo:
        client.scorer_run(
            ScorerRunRequest(
                task_path=task,
                scorer_path=scorer / "scorer.json",
                candidate_path=candidate,
                output_path=output,
                attempt_id="a1",
            )
        )
    message = str(excinfo.value)
    assert "violates retro-score-report-v1" in message
    assert "score_total" in message
    assert "score-report.schema.json" in message


def test_scorer_run_without_report_is_an_invocation_error(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    plan = write_plan(tmp_path / "plan.json", {"scorer": {"no_report": True}})
    with pytest.raises(GhostlabInvocationError) as excinfo:
        _client(ghostlab_bin, plan).scorer_run(
            ScorerRunRequest(
                task_path=task,
                scorer_path=scorer / "scorer.json",
                candidate_path=candidate,
                output_path=tmp_path / "report.json",
                attempt_id="a1",
            )
        )
    assert "scorer-run wrote no score report" in str(excinfo.value)


def test_scorer_run_rerun_clears_stale_report(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    plan_path = write_plan(tmp_path / "plan.json", {})
    output = tmp_path / "report.json"
    request = ScorerRunRequest(
        task_path=task,
        scorer_path=scorer / "scorer.json",
        candidate_path=source_root / "repo" / "outcome",
        output_path=output,
        attempt_id="a1",
    )
    client = _client(ghostlab_bin, plan_path)
    first = client.scorer_run(request)
    assert first.scored
    assert first.run_report_path.is_file()

    write_plan(plan_path, {"scorer": {"no_report": True}})
    with pytest.raises(GhostlabInvocationError, match="wrote no score report"):
        client.scorer_run(request)
    assert not output.exists()
    assert not first.run_report_path.exists()


def test_scorer_run_timeout_ignores_stale_report(tmp_path: Path, ghostlab_bin: Path) -> None:
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output = write_json(tmp_path / "report.json", {"stale": True})

    def runner(argv, timeout, env, cwd):  # noqa: ANN001 - test double
        if argv[1:2] == ["--version"]:
            return CommandOutcome(tuple(argv), 0, "ghostlab 1.0\n", "", 1, False)
        return CommandOutcome(tuple(argv), 124, "", "deadline exceeded", 10, True)

    client = GhostlabCli(ghostlab_bin, runner=runner)
    with pytest.raises(GhostlabTimeoutError, match="adapter deadline"):
        client.scorer_run(
            ScorerRunRequest(
                task_path=task,
                scorer_path=scorer / "scorer.json",
                candidate_path=candidate,
                output_path=output,
                attempt_id="a1",
            )
        )
    assert not output.exists()


def test_scorer_run_rejects_output_when_an_input_changes(
    tmp_path: Path, ghostlab_bin: Path
) -> None:
    source_root = make_source_bundle(tmp_path / "sources")
    scorer = make_scorer_package(tmp_path / "scorer", "task123")
    task = write_json(tmp_path / "task.json", {"task_id": "task123"})
    output = tmp_path / "report.json"
    request = ScorerRunRequest(
        task_path=task,
        scorer_path=scorer / "scorer.json",
        candidate_path=source_root / "repo" / "outcome",
        output_path=output,
        attempt_id="a1",
    )
    report = _client(ghostlab_bin, write_plan(tmp_path / "plan.json", {})).scorer_run(
        request
    ).report

    def runner(argv, timeout, env, cwd):  # noqa: ANN001 - test double
        if argv[1:2] == ["--version"]:
            return CommandOutcome(tuple(argv), 0, "ghostlab 1.0\n", "", 1, False)
        write_text(request.candidate_path / "changed.txt", "changed during scoring\n")
        write_json(Path(argv[argv.index("--output") + 1]), report)
        return CommandOutcome(tuple(argv), 0, "", "", 5, False)

    with pytest.raises(GhostlabContractError, match="candidate state changed during invocation"):
        GhostlabCli(ghostlab_bin, runner=runner).scorer_run(request)


@pytest.mark.parametrize(
    ("mode", "status", "judge_launcher", "accepted"),
    [
        ("deterministic", "scored", "not_run", True),
        ("deterministic", "scored", "landlock", False),
        ("judge", "scorer_error", "landlock", True),
        ("judge", "scorer_error", "not_run", False),
        ("hybrid", "scored", "landlock", True),
        ("hybrid", "scored", "not_run", False),
        ("hybrid", "scorer_error", "not_run", True),
        ("agentic", "judge_unavailable", "landlock", True),
    ],
)
def test_scorer_run_attestation_enforces_mode_and_error_isolation(
    tmp_path: Path,
    mode: str,
    status: str,
    judge_launcher: str,
    accepted: bool,
) -> None:
    path = write_json(
        tmp_path / f"{mode}-{status}-{judge_launcher}" / "scorer-run.json",
        {
            "schema_version": "ghostlab-scorer-run-v1",
            "task_id": "task123",
            "attempt_id": "attempt-1",
            "status": status,
            "hashes": {
                "task_sha256": "a" * 64,
                "scorer_package_sha256": "b" * 64,
                "candidate_sha256": "c" * 64,
                "seed": 0,
            },
            "isolation": {
                "schema_version": "ghostlab-scorer-isolation-v1",
                "scorer_launcher": "landlock",
                "candidate_mount": "read_only",
                "secure_exec_available": True,
                "judge_launcher": judge_launcher,
            },
        },
    )
    def validate() -> dict[str, object]:
        return validate_scorer_run_attestation(
            path,
            task_id="task123",
            attempt_id="attempt-1",
            status=status,
            task_sha256="a" * 64,
            scorer_package_sha256="b" * 64,
            mode=mode,
        )
    if accepted:
        assert validate()["judge_launcher"] == judge_launcher
    else:
        with pytest.raises(GhostlabContractError, match="exact GHOSTLAB_SECURE_EXEC"):
            validate()


def test_scorer_run_attestation_rejects_extra_isolation_fields(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "scorer-run.json",
        {
            "schema_version": "ghostlab-scorer-run-v1",
            "task_id": "task123",
            "attempt_id": "attempt-1",
            "status": "scored",
            "hashes": {
                "task_sha256": "a" * 64,
                "scorer_package_sha256": "b" * 64,
            },
            "isolation": {
                "schema_version": "ghostlab-scorer-isolation-v1",
                "scorer_launcher": "landlock",
                "candidate_mount": "read_only",
                "secure_exec_available": True,
                "judge_launcher": "not_run",
                "untrusted_extension": True,
            },
        },
    )
    with pytest.raises(GhostlabContractError, match="exact GHOSTLAB_SECURE_EXEC"):
        validate_scorer_run_attestation(
            path,
            task_id="task123",
            attempt_id="attempt-1",
            status="scored",
            task_sha256="a" * 64,
            scorer_package_sha256="b" * 64,
            mode="deterministic",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "ghostlab-scorer-run-v0", "does not declare"),
        ("task_id", "other-task", "task_id"),
        ("attempt_id", "other-attempt", "attempt_id"),
        ("status", "scorer_error", "status"),
        ("task_sha256", "0" * 64, "task_sha256"),
        ("scorer_package_sha256", "1" * 64, "scorer_package_sha256"),
    ],
)
def test_scorer_run_attestation_is_bound_to_current_run(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    record: dict[str, object] = {
        "schema_version": "ghostlab-scorer-run-v1",
        "task_id": "task123",
        "attempt_id": "attempt-1",
        "status": "scored",
        "hashes": {
            "task_sha256": "a" * 64,
            "scorer_package_sha256": "b" * 64,
        },
        "isolation": {
            "schema_version": "ghostlab-scorer-isolation-v1",
            "scorer_launcher": "landlock",
            "candidate_mount": "read_only",
            "secure_exec_available": True,
            "judge_launcher": "not_run",
        },
    }
    if field in ("task_sha256", "scorer_package_sha256"):
        hashes = record["hashes"]
        assert isinstance(hashes, dict)
        hashes[field] = value
    else:
        record[field] = value
    path = write_json(tmp_path / "scorer-run.json", record)

    with pytest.raises(GhostlabContractError, match=message):
        validate_scorer_run_attestation(
            path,
            task_id="task123",
            attempt_id="attempt-1",
            status="scored",
            task_sha256="a" * 64,
            scorer_package_sha256="b" * 64,
            mode="deterministic",
        )


def test_tree_hash_is_stable_and_skips_caches(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        write_text(root / "a.py", "print('a')\n")
        write_text(root / "pkg" / "b.py", "print('b')\n")
    write_text(left / "__pycache__" / "a.pyc", "junk")
    write_text(right / ".pytest_cache" / "log", "junk")

    assert sha256_path(left) == sha256_path(right)
    assert [record["path"] for record in tree_manifest(left)] == ["a.py", "pkg/b.py"]

    write_text(right / "pkg" / "b.py", "print('changed')\n")
    assert sha256_path(left) != sha256_path(right)


def test_score_reports_are_validated_against_the_packaged_schema(tmp_path: Path) -> None:
    from retro.benchmarks.task_scorer.ghostlab_cli import (
        SCORE_REPORT_CONTRACT,
        packaged_contract_errors,
        schema_path,
        validate_score_report_contract,
    )

    contract = schema_path(SCORE_REPORT_CONTRACT)
    assert contract.name == "score-report.schema.json"
    assert contract.is_file()
    with pytest.raises(GhostlabContractError) as excinfo:
        schema_path("not-a-contract")
    assert "unknown packaged schema" in str(excinfo.value)

    good = {
        "schema_version": "retro-score-report-v1",
        "task_id": "t1",
        "attempt_id": "a1",
        "status": "scored",
        "score_total": 1.0,
        "passed": True,
        "valid": True,
        "pass_threshold": 0.8,
        "unscored_weight": 0.0,
        "components": [
            {
                "id": "requested_behavior",
                "value": 1.0,
                "weight": 1.0,
                "hard_gate": True,
                "gate_passed": True,
                "evidence": [],
            }
        ],
        "hard_gate_failures": [],
        "commands": [],
        "judge": None,
        "warnings": [],
        "scorer_package_sha256": "0" * 64,
        "duration_ms": 4,
        "repeatability": {
            "runs": 2,
            "deterministic_stable": True,
            "unstable_components": [],
            "max_total_spread": 0.0,
            "totals": [1.0, 1.0],
        },
    }
    assert packaged_contract_errors(good, SCORE_REPORT_CONTRACT) == []
    assert validate_score_report_contract(good, tmp_path / "report.json") == "scored"

    extra = {**good, "components": [{**good["components"][0], "kind": "deterministic"}]}
    with pytest.raises(GhostlabContractError) as excinfo:
        validate_score_report_contract(extra, tmp_path / "report.json")
    assert "kind is not an allowed property" in str(excinfo.value)


def test_report_invariants_survive_a_missing_schema_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from retro.benchmarks.task_scorer import ghostlab_cli as module

    monkeypatch.setattr(module, "PACKAGED_SCHEMAS", {})
    report = {
        "schema_version": "retro-score-report-v1",
        "status": "scorer_error",
        "valid": False,
        "score_total": 0.0,
    }
    with pytest.raises(GhostlabContractError) as excinfo:
        module.validate_score_report_contract(report, tmp_path / "report.json")
    assert "must not carry score_total" in str(excinfo.value)

    ok = {
        "schema_version": "retro-score-report-v1",
        "status": "scorer_error",
        "valid": False,
    }
    assert module.validate_score_report_contract(ok, tmp_path / "report.json") == "scorer_error"
