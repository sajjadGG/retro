"""End-to-end Git-backed rollout task, scorer, candidate, and aggregate fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer.build import build_taskset
from retro.benchmarks.task_scorer.bundle import bundle_taskset
from retro.benchmarks.task_scorer.run import report_taskset, run_taskset
from retro.benchmarks.task_scorer.schema import (
    ProjectEnvironment,
    compute_task_id,
)
from retro.benchmarks.task_scorer.selection import select_taskset
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout
from tests.task_scorer_harness import (
    BASE_LEGACY,
    CHANGING_FEATURE,
    OUTCOME_FEATURE,
    PRESERVING_FEATURE,
    make_agent_config,
    make_audit_outputs,
    make_builder_outputs,
    make_definer_outputs,
    write_fake_ghostlab,
    write_plan,
    write_text,
)
from tests.task_scorer_helpers import git, project_environment, repo_state_json

SOURCE_ID = "codex__019abc"
SESSION_ID = "019abc"


def _event(
    sequence: int,
    *,
    actor: str,
    event_type: str,
    payload: dict,
    timestamp: str,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"{SOURCE_ID}:{sequence}",
        session_id=SESSION_ID,
        host="codex",
        sequence=sequence,
        actor=actor,  # type: ignore[arg-type]
        event_type=event_type,  # type: ignore[arg-type]
        summary=str(payload.get("text") or payload.get("command") or event_type),
        raw_ref=RawRef(path=f"raw/codex/{SESSION_ID}/rollout.jsonl", line=sequence),
        timestamp=timestamp,
        payload=payload,
    )


def _git_backed_rollout(tmp_path: Path) -> tuple[Layout, Path, dict[str, str]]:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "retro@example.invalid")
    git(repo, "config", "user.name", "Retro Tests")
    write_text(repo / "README.md", "# greeting demo\n")
    write_text(repo / "src" / "legacy.py", BASE_LEGACY)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial project")
    base_sha = git(repo, "rev-parse", "HEAD")
    base_tree = git(repo, "rev-parse", "HEAD^{tree}")

    write_text(repo / "src" / "feature.py", OUTCOME_FEATURE)
    write_text(
        repo / "tests" / "test_feature.py",
        "from src.feature import greet\n\n\n"
        "def test_greet():\n"
        "    assert 'hello, world' in greet('Ada')\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "add greet helper")
    outcome_sha = git(repo, "rev-parse", "HEAD")
    outcome_tree = git(repo, "rev-parse", "HEAD^{tree}")

    layout = Layout(tmp_path / "archive")
    layout.ensure()
    raw_dir = layout.raw_dir("codex", SESSION_ID)
    raw_dir.mkdir(parents=True)
    (raw_dir / "thread.json").write_text(
        json.dumps({"cwd": str(repo), "thread_id": SESSION_ID}),
        encoding="utf-8",
    )
    (raw_dir / "repo_start.json").write_text(
        repo_state_json(repo, base_sha, base_tree),
        encoding="utf-8",
    )
    events = [
        _event(
            1,
            actor="user",
            event_type="message",
            payload={
                "text": (
                    "Add a greet(name) helper in src/feature.py that returns a "
                    "hello, world message including the supplied name."
                )
            },
            timestamp="2026-08-01T18:12:03Z",
        ),
        _event(
            2,
            actor="assistant",
            event_type="file_edit",
            payload={"file_path": "src/feature.py"},
            timestamp="2026-08-01T18:15:00Z",
        ),
        _event(
            3,
            actor="tool",
            event_type="command",
            payload={
                "command": "git commit -m 'add greet helper'",
                "output": f"[main {outcome_sha[:7]}] add greet helper",
                "exit_code": 0,
            },
            timestamp="2026-08-01T18:18:00Z",
        ),
        _event(
            4,
            actor="assistant",
            event_type="message",
            payload={"text": "Implemented the helper and its test."},
            timestamp="2026-08-01T18:19:00Z",
        ),
        _event(
            5,
            actor="user",
            event_type="message",
            payload={"text": "Looks good, thanks."},
            timestamp="2026-08-01T18:20:00Z",
        ),
    ]
    write_events(layout.normalized_path("codex", SESSION_ID), events)
    return layout, repo, {
        "base_sha": base_sha,
        "base_tree": base_tree,
        "outcome_sha": outcome_sha,
        "outcome_tree": outcome_tree,
    }


def test_git_rollout_to_alternative_implementation_score_is_reproducible(tmp_path: Path):
    layout, _repo, shas = _git_backed_rollout(tmp_path)
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))

    selection = select_taskset(
        layout=layout,
        name="pilot",
        sessions=[("codex", SESSION_ID)],
        branch="main",
        environment_resolver=lambda _candidate: environment,
    )
    assert len(selection.selected) == 1
    first_bundle = bundle_taskset(layout=layout, name="pilot")
    assert len(first_bundle.bundled) == 1
    bundle_hash = first_bundle.bundled[0].content_sha256

    task_id = compute_task_id(
        SOURCE_ID,
        shas["base_tree"],
        "replay",
        (
            "Add a greet(name) helper in src/feature.py that returns a hello, world "
            "message including the supplied name."
        ),
    )
    outputs = tmp_path / "outputs"
    definer = make_definer_outputs(outputs / "definer", SOURCE_ID)
    builder = make_builder_outputs(outputs / "builder", task_id)
    auditor = make_audit_outputs(outputs / "auditor")
    alternative = outputs / "alternative"
    write_text(alternative / "src" / "feature.py", PRESERVING_FEATURE)
    targeted = outputs / "targeted"
    write_text(targeted / "src" / "feature.py", CHANGING_FEATURE)

    binary = write_fake_ghostlab(tmp_path / "bin")
    plan_path = tmp_path / "plan.json"
    plan = {
        "artifact_runs": {
            "retro-task-definer-v1": {"outputs": str(definer)},
            "retro-scorer-builder-v1": {"outputs": str(builder)},
            "retro-scorer-auditor-v1": {"outputs": str(auditor)},
            "candidate-alternative": {"workspace_overlay": str(alternative)},
            "candidate-targeted": {"workspace_overlay": str(targeted)},
        }
    }
    write_plan(plan_path, plan)
    env = {"FAKE_GHOSTLAB_PLAN": str(plan_path)}
    agents = tmp_path / "agents"
    definer_agent = make_agent_config(
        agents / "definer.json", "retro-task-definer-v1", "definer-model"
    )
    builder_agent = make_agent_config(
        agents / "builder.json", "retro-scorer-builder-v1", "builder-model"
    )
    auditor_agent = make_agent_config(
        agents / "auditor.json", "retro-scorer-auditor-v1", "auditor-model"
    )
    alternative_agent = make_agent_config(
        agents / "alternative.json", "candidate-alternative", "candidate-model"
    )
    targeted_agent = make_agent_config(
        agents / "targeted.json", "candidate-targeted", "candidate-model"
    )

    built = build_taskset(
        layout,
        "pilot",
        binary,
        definer_agent,
        builder_agent,
        auditor_agent,
        repeatability_runs=3,
        ghostlab_env=env,
    )
    assert built.published_task_ids == (task_id,)
    validation = json.loads(
        (
            layout.benchmark_taskset_task_dir("pilot", task_id)
            / "private"
            / "scorer-validation.json"
        ).read_text(encoding="utf-8")
    )
    cases = {case["kind"]: case for case in validation["cases"]}
    assert cases["base"]["score_total"] <= 0.20
    assert cases["oracle"]["score_total"] >= 0.90
    assert (
        cases["oracle"]["component_values"]["requested_behavior"]
        - cases["construct_changing"]["component_values"]["requested_behavior"]
        >= 0.50
    )
    assert cases["regression"]["score_total"] == 0

    alternative_run = run_taskset(
        layout,
        "pilot",
        alternative_agent,
        "0",
        binary,
        eval_id="e2e",
        ghostlab_env=env,
    )
    assert alternative_run.benchmark_score is not None
    assert alternative_run.benchmark_score >= 0.80
    attempt = alternative_run.attempts[0]
    assert attempt.candidate_state_sha256
    assert (attempt.attempt_dir / "agent" / "candidate-state.tar.zst").is_file()

    targeted_run = run_taskset(
        layout,
        "pilot",
        targeted_agent,
        "0",
        binary,
        eval_id="e2e",
        ghostlab_env=env,
    )
    assert targeted_run.benchmark_score == pytest.approx(0.0)
    report = report_taskset(layout, "pilot", "e2e")
    scores = {row["agent_id"]: row["benchmark_score"] for row in report.agent_rows()}
    assert scores == {
        "candidate-alternative": pytest.approx(1.0),
        "candidate-targeted": pytest.approx(0.0),
    }

    second_bundle = bundle_taskset(layout=layout, name="pilot")
    rebuilt = build_taskset(
        layout,
        "pilot",
        binary,
        definer_agent,
        builder_agent,
        auditor_agent,
        repeatability_runs=3,
        ghostlab_env=env,
    )
    assert second_bundle.bundled[0].content_sha256 == bundle_hash
    assert rebuilt.build_id == built.build_id
    assert rebuilt.published_task_ids == built.published_task_ids
