"""Release gates, grouped splits, immutable snapshots and evaluation cohorts."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer.build import TasksetPaths, build_taskset
from retro.benchmarks.task_scorer.bundle import bundle_taskset
from retro.benchmarks.task_scorer.ghostlab_cli import GhostlabCli, sha256_file, sha256_json, write_json
from retro.benchmarks.task_scorer.run import report_taskset, run_taskset
from retro.benchmarks.task_scorer.schema import ProjectEnvironment, SchemaError, compute_task_id
from retro.benchmarks.task_scorer.selection import select_taskset
from retro.benchmarks.task_scorer.suite import (
    CURATION_POLICY_SCHEMA,
    CurationPolicy,
    SuiteError,
    TaskOverride,
    assign_splits,
    bind_evaluation_cohort,
    curate_taskset,
    group_tasks,
    load_suite,
)
from retro.storage import Layout
from tests.task_scorer_harness import (
    TASK_PROMPT,
    make_agent_config,
    make_audit_outputs,
    make_builder_outputs,
    make_candidate_overlay,
    make_definer_outputs,
    write_fake_ghostlab,
    write_plan,
)
from tests.task_scorer_helpers import project_environment
from tests.test_task_scorer_e2e import SOURCE_ID, _git_backed_rollout


@dataclass
class PublishedFixture:
    layout: Layout
    paths: TasksetPaths
    task_id: str
    shas: dict[str, str]
    binary: Path
    plan_path: Path
    good_agent: Path
    bad_agent: Path

    def client(self) -> GhostlabCli:
        return GhostlabCli(self.binary, env={"FAKE_GHOSTLAB_PLAN": str(self.plan_path)})


def make_published_fixture(tmp_path: Path) -> PublishedFixture:
    layout, _repo, shas = _git_backed_rollout(tmp_path)
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))
    select_taskset(
        layout=layout, name="pilot", sessions=[("codex", "019abc")], branch="main",
        environment_resolver=lambda _candidate: environment,
    )
    bundle_taskset(layout=layout, name="pilot")
    task_id = compute_task_id(SOURCE_ID, shas["base_tree"], "replay", TASK_PROMPT)
    outputs = tmp_path / "outputs"
    definer = make_definer_outputs(outputs / "definer", SOURCE_ID)
    builder = make_builder_outputs(outputs / "builder", task_id)
    auditor = make_audit_outputs(outputs / "auditor")
    solved = make_candidate_overlay(outputs / "solved", solved=True)
    unsolved = make_candidate_overlay(outputs / "unsolved", solved=False)
    binary = write_fake_ghostlab(tmp_path / "bin")
    plan_path = tmp_path / "plan.json"
    write_plan(
        plan_path,
        {
            "artifact_runs": {
                "retro-task-definer-v1": {"outputs": str(definer)},
                "retro-scorer-builder-v1": {"outputs": str(builder)},
                "retro-scorer-auditor-v1": {"outputs": str(auditor)},
                "candidate-good": {"workspace_overlay": str(solved)},
                "candidate-bad": {"workspace_overlay": str(unsolved)},
            }
        },
    )
    agents = tmp_path / "agents"
    definer_agent = make_agent_config(agents / "definer.json", "retro-task-definer-v1", "m-definer")
    builder_agent = make_agent_config(agents / "builder.json", "retro-scorer-builder-v1", "m-builder")
    auditor_agent = make_agent_config(agents / "auditor.json", "retro-scorer-auditor-v1", "m-auditor")
    good = make_agent_config(agents / "good.json", "candidate-good", "m-candidate")
    bad = make_agent_config(agents / "bad.json", "candidate-bad", "m-candidate")
    built = build_taskset(
        layout, "pilot", binary, definer_agent, builder_agent, auditor_agent,
        repeatability_runs=2, ghostlab_env={"FAKE_GHOSTLAB_PLAN": str(plan_path)},
    )
    assert built.published_task_ids == (task_id,)
    return PublishedFixture(
        layout, TasksetPaths.from_layout(layout, "pilot"), task_id, shas, binary, plan_path, good, bad
    )


@pytest.fixture
def published(tmp_path: Path) -> PublishedFixture:
    return make_published_fixture(tmp_path)


def _task(index: int, *, source: str, repo: str = "repo", prompt: str = "", family: str = ""):
    return {
        "task_id": f"{index:020x}", "source_id": source, "repo_id": repo,
        "prompt": prompt or f"Implement distinct operation {index} for input {index}.",
        "family": family or source,
    }


def test_default_curation_is_test_only_and_policy_is_strict():
    policy = CurationPolicy()
    assert policy.split_unit == "repository"
    assert policy.split_ratios == {"train": 0, "validation": 0, "test": 1}
    assert CurationPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(SuiteError, match="sum"):
        CurationPolicy(split_ratios={"train": 0.8, "validation": 0.1, "test": 0.2})
    with pytest.raises(SuiteError, match="evaluation-only"):
        CurationPolicy(include_judged=True, split_ratios={"train": 1, "validation": 0, "test": 0})
    with pytest.raises(SchemaError, match="unknown"):
        CurationPolicy.from_dict({"schema_version": CURATION_POLICY_SCHEMA, "typo": True})
    with pytest.raises(SuiteError, match="finite"):
        TaskOverride(priority=float("nan"), reason="user preference")
    with pytest.raises(SuiteError, match="explain"):
        CurationPolicy(overrides={"0" * 20: TaskOverride()})


def test_source_family_and_repository_groups_are_transitive():
    tasks = [
        _task(1, source="a", repo="one"),
        _task(2, source="a", repo="two", family="boundary"),
        _task(3, source="b", repo="two", family="boundary"),
        _task(4, source="c", repo="three"),
    ]
    source = group_tasks(tasks, CurationPolicy(split_unit="source"))
    assert source[tasks[0]["task_id"]] == source[tasks[2]["task_id"]]
    assert source[tasks[3]["task_id"]] != source[tasks[0]["task_id"]]
    repository = group_tasks(tasks, CurationPolicy())
    assert repository[tasks[0]["task_id"]] == repository[tasks[2]["task_id"]]


def test_near_duplicate_prompts_stay_together_even_at_different_snapshots():
    prompt = " ".join(f"requirement{index}" for index in range(35))
    tasks = [
        _task(1, source="a", prompt=prompt),
        _task(2, source="b", prompt=prompt.replace("requirement34", "boundary34")),
        _task(3, source="c", repo="unrelated", prompt=prompt),
    ]
    groups = group_tasks(tasks, CurationPolicy(split_unit="source"))
    assert groups[tasks[0]["task_id"]] == groups[tasks[1]["task_id"]]
    assert groups[tasks[0]["task_id"]] != groups[tasks[2]["task_id"]]


def test_split_allocation_is_deterministic_group_disjoint_and_nonempty():
    groups = [f"group-{index}" for index in range(40)]
    ratios = {"train": 0.8, "validation": 0.1, "test": 0.1}
    result = assign_splits(groups, ratios, 42)
    assert Counter(result.values()) == {"train": 32, "validation": 4, "test": 4}
    assert assign_splits(list(reversed(groups)), ratios, 42) == result
    assert assign_splits(groups, ratios, 43) != result
    assert Counter(assign_splits(groups[:3], ratios, 42).values()) == {
        "train": 1, "validation": 1, "test": 1,
    }
    with pytest.raises(SuiteError, match="independent groups"):
        assign_splits(groups[:2], ratios, 42)


def test_suite_freezes_verified_inputs_and_is_resumable(published: PublishedFixture):
    suite = curate_taskset(published.layout, "pilot", suite_id="personal-v1")
    assert suite.manifest["summary"]["tasks"] == 1
    assert suite.manifest["summary"]["splits"] == {"train": 0, "validation": 0, "test": 1}
    assert suite.manifest["tasks"][0]["provenance_grade"] == "exact_replay"
    assert suite.task_ids("test") == [published.task_id]
    assert curate_taskset(published.layout, "pilot", suite_id="personal-v1").content_sha256 == (
        suite.content_sha256
    )
    original = published.paths.task_dir(published.task_id) / "public" / "prompt.txt"
    original.write_text("changed active task\n")
    assert load_suite(published.paths, "personal-v1").content_sha256 == suite.content_sha256


def test_release_id_cannot_be_reused_for_changed_policy(published: PublishedFixture):
    curate_taskset(published.layout, "pilot", suite_id="personal-v1")
    policy = CurationPolicy(
        overrides={published.task_id: TaskOverride(priority=2, tags=("important",), reason="core user task")}
    )
    with pytest.raises(SuiteError, match="immutable"):
        curate_taskset(published.layout, "pilot", suite_id="personal-v1", policy=policy)


def test_tampered_snapshot_is_rejected(published: PublishedFixture):
    suite = curate_taskset(published.layout, "pilot", suite_id="personal-v1")
    (suite.paths.task_dir(published.task_id) / "public" / "prompt.txt").write_text("leaked solution")
    with pytest.raises(SuiteError, match="changed after publication"):
        load_suite(published.paths, suite.suite_id)


def test_legacy_scorer_hash_must_be_canonical_before_freezing_a_release(published: PublishedFixture):
    private = published.paths.task_dir(published.task_id) / "private"
    scorer = json.loads((private / "scorer" / "scorer.json").read_text())
    legacy_hash = sha256_json(
        {
            "scorer_json": {key: value for key, value in scorer.items() if key != "package_sha256"},
            "files": [
                (path.relative_to(private / "scorer").as_posix(), sha256_file(path))
                for path in sorted((private / "scorer").rglob("*"))
                if path.is_file() and path.name != "scorer.json"
            ],
        }
    )
    provenance = json.loads((private / "provenance.json").read_text())
    provenance["scorer_package_sha256"] = legacy_hash
    write_json(private / "provenance.json", provenance)
    with pytest.raises(SuiteError, match="LEGACY_SCORER_PROVENANCE"):
        curate_taskset(published.layout, "pilot")


def _rewrite_validation(published: PublishedFixture, mutate):
    private = published.paths.task_dir(published.task_id) / "private"
    validation = json.loads((private / "scorer-validation.json").read_text())
    mutate(validation)
    write_json(private / "scorer-validation.json", validation)
    provenance = json.loads((private / "provenance.json").read_text())
    provenance["validation"] = validation
    write_json(private / "provenance.json", provenance)


@pytest.mark.parametrize(
    "damage", ["single-run", "base-passes", "preserving-overfit", "wrong-target", "no-isolation"]
)
def test_curation_recomputes_numeric_gates_and_records_rejections(
    published: PublishedFixture, damage: str
):
    def mutate(validation: dict[str, Any]):
        cases = {case["kind"]: case for case in validation["cases"]}
        if damage == "single-run":
            cases["oracle"]["runs"] = 1
        elif damage == "base-passes":
            cases["base"]["score_total"] = 0.3
        elif damage == "preserving-overfit":
            cases["construct_preserving"]["score_total"] = 0.8
        elif damage == "no-isolation":
            validation["isolation_attestation"] = {}
        else:
            cases["construct_changing"]["targeted_component"] = "regression_suite"

    _rewrite_validation(published, mutate)
    with pytest.raises(SuiteError, match="no tasks passed"):
        curate_taskset(published.layout, "pilot")
    report = json.loads((published.paths.root / "curation.json").read_text())
    assert report["status"] == "rejected"
    assert report["accepted_task_ids"] == []
    assert len(report["rejections"]) == 1
    assert not (published.paths.root / "suites").exists()


def test_reconstruction_requires_opt_in_and_is_never_labelled_exact(published: PublishedFixture):
    path = published.paths.task_dir(published.task_id) / "private" / "source-link.json"
    source = json.loads(path.read_text())
    source["repo"]["state_confidence"] = "approximate"
    source["repo"]["base_resolution"] = "anchored_reconstruction"
    write_json(path, source)
    with pytest.raises(SuiteError, match="APPROXIMATE_START_STATE"):
        curate_taskset(published.layout, "pilot")
    suite = curate_taskset(published.layout, "pilot", policy=CurationPolicy(allow_reconstructed=True))
    assert suite.manifest["tasks"][0]["provenance_grade"] == "anchored_reconstruction"


def test_requested_training_splits_require_enough_independent_groups(published: PublishedFixture):
    policy = CurationPolicy(split_ratios={"train": 0.8, "validation": 0.1, "test": 0.1})
    with pytest.raises(SuiteError, match="independent groups"):
        curate_taskset(published.layout, "pilot", policy=policy)


def test_two_agents_use_the_same_frozen_cohort_and_not_current_tasks(published: PublishedFixture):
    suite = curate_taskset(published.layout, "pilot", suite_id="personal-v1")
    active = published.paths.task_dir(published.task_id) / "public" / "prompt.txt"
    active.write_text("this active task is no longer the released task")
    results = [
        run_taskset(
            published.layout, "pilot", agent, "0,1", published.binary,
            suite_id=suite.suite_id, ghostlab_env={"FAKE_GHOSTLAB_PLAN": str(published.plan_path)},
        )
        for agent in (published.good_agent, published.bad_agent)
    ]
    assert results[0].eval_id == results[1].eval_id
    assert results[0].benchmark_score == pytest.approx(1)
    assert results[1].benchmark_score == pytest.approx(0)
    assert results[1].aggregate.cohort is not None
    assert results[1].aggregate.cohort["suite_content_sha256"] == suite.content_sha256
    assert all(agent.coverage == 1 for agent in results[1].aggregate.agents)
    with pytest.raises(SuiteError, match="cohort changed"):
        bind_evaluation_cohort(
            published.paths, results[0].eval_id, suite, "test", [published.task_id], (0,)
        )


def test_missing_attempts_reduce_frozen_coverage_instead_of_disappearing(published: PublishedFixture):
    suite = curate_taskset(published.layout, "pilot")
    result = run_taskset(
        published.layout, "pilot", published.good_agent, "0,1", published.binary,
        suite_id=suite.suite_id, ghostlab_env={"FAKE_GHOSTLAB_PLAN": str(published.plan_path)},
    )
    (result.attempts[1].attempt_dir / "attempt.json").unlink()
    report = report_taskset(published.layout, "pilot", result.eval_id)
    agent = report.aggregate.agents[0]
    assert agent.requested_attempts == 2
    assert agent.scored_attempts == 1
    assert agent.coverage == pytest.approx(0.5)
    assert agent.benchmark_score == pytest.approx(1)
