"""Private grader inspection stays on demand and bound to saved artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer.ghostlab_cli import sha256_file, sha256_text, write_json
from retro.benchmarks.task_scorer.run import TaskVerificationError, run_taskset, verify_published_task
from retro.benchmarks.task_scorer.suite import CurationPolicy, curate_taskset, load_suite
from retro.dashboard import inspector
from retro.dashboard.inspector import InspectionError, inspect_grader, inspect_run
from tests.test_task_scorer_suite import PublishedFixture, make_published_fixture


@pytest.fixture
def published(tmp_path: Path) -> PublishedFixture:
    fixture = make_published_fixture(tmp_path)
    result = curate_taskset(
        fixture.layout,
        "pilot",
        suite_id="inspect-v1",
        policy=CurationPolicy(allow_reconstructed=True),
    )
    assert result.task_ids("test") == [fixture.task_id]
    return fixture


def test_deterministic_grader_shows_actual_source_and_output_schema(published: PublishedFixture):
    result = inspect_grader(published.layout, "pilot", published.task_id, suite_id="inspect-v1")
    assert result["prompt_status"] == "no_llm_prompt"
    assert result["judge_prompt"] is None
    assert result["manifest"]["mode"] == "deterministic"
    assert result["manifest"]["entrypoint"][1] == "/scorer/score.py"
    assert result["public_task"]["task_id"] == published.task_id
    source = next(item for item in result["files"] if item["path"] == "score.py")
    assert "requested_behavior" in source["content"]
    assert source["sha256"] == sha256_file(
        load_suite(published.paths, "inspect-v1").paths.task_dir(published.task_id)
        / "private"
        / "scorer"
        / "score.py"
    )
    assert result["input_contract"]["repo_path"] == "/candidate/repo"
    assert "score_total" in result["output_schema"]["properties"]


def test_frozen_grader_is_not_replaced_by_changed_live_publication(published: PublishedFixture):
    (published.paths.task_dir(published.task_id) / "private" / "scorer" / "score.py").write_text(
        "raise RuntimeError('new live implementation')\n", encoding="utf-8"
    )
    result = inspect_grader(published.layout, "pilot", published.task_id, suite_id="inspect-v1")
    assert "new live implementation" not in json.dumps(result)
    with pytest.raises(TaskVerificationError):
        inspect_grader(published.layout, "pilot", published.task_id)


def test_judge_prompt_is_visible_but_config_and_environment_credentials_are_not(
    published: PublishedFixture, monkeypatch
):
    task = verify_published_task(load_suite(published.paths, "inspect-v1").paths, published.task_id)
    package = task.scorer_manifest_path.parent
    manifest = json.loads(task.scorer_manifest_path.read_text(encoding="utf-8"))
    manifest["mode"] = "judge"
    manifest["runtime"]["env"] = {"CUSTOM_VALUE": "RUNTIME_SECRET"}
    manifest["judge"] = {
        "enabled": True,
        "agent_config": "/scorer/judge-agent.json",
        "prompt": "/scorer/judge.prompt.md",
        "output_schema": "/scorer/judge.schema.json",
        "criteria": ["requested_behavior"],
    }
    write_json(task.scorer_manifest_path, manifest)
    write_json(package / "judge-agent.json", {"env": {"TOKEN": "JUDGE_CONFIG_SECRET"}})
    (package / "judge.prompt.md").write_text(
        "Review the candidate's observable behavior.\napi_key=sk-fakecredential123456789\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(inspector, "_published_task", lambda *_args, **_kwargs: task)
    result = inspect_grader(published.layout, "pilot", published.task_id, suite_id="inspect-v1")
    assert result["prompt_status"] == "pinned_judge_prompt"
    assert "Review the candidate's observable behavior" in result["judge_prompt"]
    serialized = json.dumps(result)
    for secret in ("JUDGE_CONFIG_SECRET", "RUNTIME_SECRET", "sk-fakecredential123456789"):
        assert secret not in serialized
    assert not any(item["path"] == "judge-agent.json" for item in result["files"])


@pytest.mark.parametrize("name,task,suite", [("../outside", "task", None), ("pilot", "../task", None)])
def test_inspection_ids_cannot_escape_the_archive(published: PublishedFixture, name, task, suite):
    with pytest.raises(InspectionError, match="Invalid"):
        inspect_grader(published.layout, name, task, suite_id=suite)


def test_grader_symlink_is_rejected_without_reading_its_target(
    published: PublishedFixture, tmp_path: Path, monkeypatch
):
    task = verify_published_task(load_suite(published.paths, "inspect-v1").paths, published.task_id)
    target = tmp_path / "unrelated-secret.txt"
    target.write_text("PRIVATE_EXTERNAL_CONTENT", encoding="utf-8")
    (task.scorer_manifest_path.parent / "leaked.txt").symlink_to(target)
    monkeypatch.setattr(inspector, "_published_task", lambda *_args, **_kwargs: task)
    with pytest.raises(InspectionError, match="symlink"):
        inspect_grader(published.layout, "pilot", published.task_id, suite_id="inspect-v1")


@pytest.fixture
def scored(published: PublishedFixture) -> tuple[PublishedFixture, Path]:
    run_taskset(
        published.layout,
        "pilot",
        published.bad_agent,
        seeds=(0,),
        eval_id="inspect-run",
        suite_id="inspect-v1",
        ghostlab=published.client(),
    )
    path = published.paths.attempt_dir("inspect-run", published.task_id, "candidate-bad", 0)
    return published, path


def test_each_run_exposes_actual_zero_grade_and_recorded_failure_evidence(scored):
    published, path = scored
    result = inspect_run(published.layout, "pilot", "inspect-run", published.task_id, "candidate-bad", 0)
    actual = json.loads((path / "score-report.json").read_text(encoding="utf-8"))
    assert result["score_report"] == actual
    assert result["score_report"]["score_total"] == 0
    assert result["score_report"]["passed"] is False
    assert result["cohort"]["suite_id"] == "inspect-v1"
    assert result["input_status"] == "not_retained"
    assert result["score_input"] is None
    assert "score_input" in result["missing_artifacts"]
    assert result["inspection_only"] is True


def _save_input(published: PublishedFixture, path: Path) -> Path:
    attempt = json.loads((path / "attempt.json").read_text(encoding="utf-8"))
    source = path / "scorer" / "scorer-staging" / "input" / "score-input.json"
    write_json(
        source,
        {
            "schema_version": "retro-score-input-v1",
            "task_id": published.task_id,
            "attempt_id": attempt["attempt_id"],
            "repo_path": "/candidate/repo",
            "task_path": "/input/task.json",
            "trace_path": None,
            "resource_usage_path": None,
            "seed": 0,
        },
    )
    report = path / "scorer" / "scorer-run.json"
    attestation = json.loads(report.read_text(encoding="utf-8"))
    attestation["hashes"]["score_input_sha256"] = sha256_text(
        json.dumps(json.loads(source.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":"))
    )
    write_json(report, attestation)
    return source


def test_saved_input_is_shown_exactly_and_checksum_verified(scored):
    published, path = scored
    source = _save_input(published, path)
    result = inspect_run(published.layout, "pilot", "inspect-run", published.task_id, "candidate-bad", 0)
    assert result["input_status"] == "recorded"
    assert result["score_input"] == json.loads(source.read_text(encoding="utf-8"))
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert (
        inspect_run(published.layout, "pilot", "inspect-run", published.task_id, "candidate-bad", 0)[
            "score_input"
        ]
        == result["score_input"]
    )
    tampered = json.loads(source.read_text(encoding="utf-8"))
    tampered["trace_path"] = "/input/different-trace.json"
    write_json(source, tampered)
    with pytest.raises(InspectionError, match="checksum"):
        inspect_run(published.layout, "pilot", "inspect-run", published.task_id, "candidate-bad", 0)


def test_other_attempts_input_or_result_is_never_substituted(scored):
    published, path = scored
    source = _save_input(published, path)
    data = json.loads(source.read_text(encoding="utf-8"))
    data["attempt_id"] = "another-attempt"
    write_json(source, data)
    with pytest.raises(InspectionError, match="different attempt"):
        inspect_run(published.layout, "pilot", "inspect-run", published.task_id, "candidate-bad", 0)
