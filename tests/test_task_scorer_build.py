"""Tests for the resumable task/scorer build state machine."""
from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer.build import (
    ACTIVE_TASKS_SCHEMA,
    BASE_MAX_TOTAL,
    ORACLE_MIN_TOTAL,
    BuildConfig,
    BuildConfigurationError,
    LintFinding,
    LintOutcome,
    RepeatabilityResult,
    SourceStageState,
    StageFailure,
    StageStore,
    TasksetPaths,
    _assert_public_clean,
    build_source,
    build_sources,
    coerce_lint_outcome,
    compute_task_id,
    evaluate_repeatability,
    evaluate_validation_case,
    list_published_tasks,
    normalize_prompt,
    pack_directory,
    resolve_lint_fn,
    unpack_bundle,
    validate_scorer_package,
)
from retro.benchmarks.task_scorer.bundle import compute_content_hash
from retro.benchmarks.task_scorer.ghostlab_cli import GhostlabCli
from tests.task_scorer_harness import (
    BASE_TREE,
    CHANGING_FEATURE,
    OUTCOME_SHA,
    TASK_PROMPT,
    accept_all_lint,
    make_agent_config,
    make_audit_outputs,
    make_builder_outputs,
    make_definer_outputs,
    make_scorer_package,
    make_source_bundle,
    make_task_definitions,
    reject_all_lint,
    write_fake_ghostlab,
    write_json,
    write_plan,
    write_text,
)

SOURCE_ID = "codex__019abc"


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.paths = TasksetPaths(root=tmp_path / "bench" / "pilot" / "task-scorer", name="pilot")
        self.source_root = make_source_bundle(self.paths.sources_dir(), SOURCE_ID)
        self.task_id = compute_task_id(SOURCE_ID, BASE_TREE, "replay", TASK_PROMPT)
        self.definer_out = make_definer_outputs(tmp_path / "out" / "definer", SOURCE_ID)
        self.builder_out = make_builder_outputs(tmp_path / "out" / "builder", self.task_id)
        self.audit_out = make_audit_outputs(tmp_path / "out" / "auditor")
        self.binary = write_fake_ghostlab(tmp_path / "bin")
        self.plan_path = tmp_path / "plan.json"
        self.plan: dict[str, Any] = {
            "artifact_runs": {
                "retro-task-definer-v1": {"outputs": str(self.definer_out)},
                "retro-scorer-builder-v1": {"outputs": str(self.builder_out)},
                "retro-scorer-auditor-v1": {"outputs": str(self.audit_out)},
            }
        }
        self.write_plan()
        self.definer_agent = make_agent_config(
            tmp_path / "agents" / "definer.json", "retro-task-definer-v1", "definer-model"
        )
        self.builder_agent = make_agent_config(
            tmp_path / "agents" / "builder.json", "retro-scorer-builder-v1", "builder-model"
        )
        self.auditor_agent = make_agent_config(
            tmp_path / "agents" / "auditor.json", "retro-scorer-auditor-v1", "auditor-model"
        )

    def write_plan(self) -> None:
        write_plan(self.plan_path, self.plan)

    def client(self) -> GhostlabCli:
        return GhostlabCli(self.binary, env={"FAKE_GHOSTLAB_PLAN": str(self.plan_path)})

    def config(self, **overrides: Any) -> BuildConfig:
        defaults: dict[str, Any] = {
            "name": "pilot",
            "ghostlab": self.client(),
            "task_definer_agent": self.definer_agent,
            "scorer_builder_agent": self.builder_agent,
            "scorer_auditor_agent": self.auditor_agent,
            "lint": accept_all_lint,
            "repeatability_runs": 2,
        }
        defaults.update(overrides)
        return BuildConfig(**defaults)


@pytest.fixture()
def fixture(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


def test_task_id_is_content_addressed_and_prompt_normalized() -> None:
    assert normalize_prompt("  Add   a\n greet\thelper  ") == "Add a greet helper"
    first = compute_task_id("src-1", "tree-1", "replay", " Add a  helper ")
    second = compute_task_id("src-1", "tree-1", "replay", "Add a helper")
    assert first == second and len(first) == 20
    assert compute_task_id("src-2", "tree-1", "replay", "Add a helper") != first
    assert compute_task_id("src-1", "tree-1", "adjacent", "Add a helper") != first


def test_unpack_bundle_rejects_escaping_archive_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "malicious.tar"
    with tarfile.open(archive_path, "w") as archive:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        archive.addfile(link)
        data = b"secret"
        nested = tarfile.TarInfo("link/file")
        nested.size = len(data)
        archive.addfile(nested, io.BytesIO(data))

    with pytest.raises(StageFailure, match="escaping link"):
        unpack_bundle(archive_path, tmp_path / "destination")


def test_full_build_publishes_one_validated_task(fixture: _Fixture) -> None:
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == (fixture.task_id,)
    source = result.sources[0]
    assert source.state.stage == "published"
    assert source.state.status == "ok"
    assert set(source.state.completed) >= {"selected", "bundled", "task_generated", "task_linted"}
    # The TaskDefiner's own goal rejection is preserved as a coded record.
    assert result.rejection_counts() == {"NO_OBSERVABLE_OUTCOME": 1}

    task_dir = fixture.paths.task_dir(fixture.task_id)
    assert sorted(p.name for p in (task_dir / "public").iterdir()) == [
        "base.bundle",
        "environment.json",
        "prompt.txt",
        "task.json",
    ]
    assert sorted(p.name for p in (task_dir / "private").iterdir()) == [
        "oracle.bundle",
        "provenance.json",
        "scorer",
        "scorer-validation.json",
        "source-link.json",
    ]

    public_task = json.loads((task_dir / "public" / "task.json").read_text())
    assert public_task["schema_version"] == "retro-benchmark-task-v1"
    assert public_task["task_id"] == fixture.task_id
    assert public_task["scoring"]["pass_threshold"] == 0.8
    assert (task_dir / "public" / "prompt.txt").read_text() == TASK_PROMPT

    validation = json.loads((task_dir / "private" / "scorer-validation.json").read_text())
    assert validation["passed"] is True
    by_kind = {case["kind"]: case for case in validation["cases"]}
    assert set(by_kind) == {
        "base",
        "oracle",
        "no_op",
        "construct_changing",
        "construct_preserving",
        "regression",
    }
    assert by_kind["base"]["score_total"] <= BASE_MAX_TOTAL
    assert by_kind["base"]["hard_gate_failures"]
    assert by_kind["oracle"]["score_total"] >= ORACLE_MIN_TOTAL
    assert by_kind["no_op"]["passed"] is False
    drop = (
        by_kind["oracle"]["component_values"]["requested_behavior"]
        - by_kind["construct_changing"]["component_values"]["requested_behavior"]
    )
    assert drop >= 0.5
    assert abs(by_kind["construct_preserving"]["score_total"] - by_kind["oracle"]["score_total"]) <= 0.05
    assert by_kind["regression"]["score_total"] == 0.0
    assert "regression_suite" in by_kind["regression"]["hard_gate_failures"]
    assert all(case["runs"] == 2 for case in validation["cases"])

    provenance = json.loads((task_dir / "private" / "provenance.json").read_text())
    assert provenance["source_id"] == SOURCE_ID
    assert provenance["audit"]["decision"] == "accept"
    assert provenance["ghostlab"]["version"] == "9.9.9-fake"


def test_build_rejects_source_without_validated_environment(fixture: _Fixture) -> None:
    (fixture.source_root / "context" / "environment.json").unlink()
    manifest_path = fixture.source_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["content_sha256"] = compute_content_hash(fixture.source_root)
    write_json(manifest_path, manifest)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    assert result.sources[0].state.error["code"] == "ENVIRONMENT_UNAVAILABLE"


def test_build_rejects_tampered_source_bundle(fixture: _Fixture) -> None:
    write_text(fixture.source_root / "repo" / "base" / "README.md", "tampered\n")

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    assert result.sources[0].state.error["code"] == "HARNESS_ERROR"
    assert "content hash" in result.sources[0].state.error["detail"]


def test_public_material_never_leaks_oracle_state(fixture: _Fixture) -> None:
    build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    public = fixture.paths.task_dir(fixture.task_id) / "public"
    blob = b"".join(path.read_bytes() for path in public.rglob("*") if path.is_file())
    assert OUTCOME_SHA.encode() not in blob
    assert str(fixture.source_root).encode() not in blob

    with tarfile.open(public / "base.bundle") as archive:
        names = sorted(archive.getnames())
    assert "src/legacy.py" in names
    assert "src/feature.py" not in names

    with tarfile.open(
        fixture.paths.task_dir(fixture.task_id) / "private" / "oracle.bundle"
    ) as archive:
        assert "src/feature.py" in archive.getnames()


def test_public_leak_scan_does_not_reject_literal_text_inside_base_bundle(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public"
    public.mkdir()
    write_json(public / "task.json", {"prompt": "safe"})
    write_text(public / "prompt.txt", "safe")
    write_json(public / "environment.json", {"image": "safe"})
    (public / "base.bundle").write_bytes(b"public base contains /captured/repo")

    _assert_public_clean(public, ["/captured/repo"])

    write_text(public / "prompt.txt", "leaked /captured/repo")
    with pytest.raises(StageFailure, match="leaks private oracle material"):
        _assert_public_clean(public, ["/captured/repo"])


def test_rebuild_with_unchanged_inputs_reuses_stages(fixture: _Fixture) -> None:
    first = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    provenance_path = (
        fixture.paths.task_dir(fixture.task_id) / "private" / "provenance.json"
    )
    published_before = json.loads(provenance_path.read_text())["publication_sha256"]

    second = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert second.build_id == first.build_id
    reused = second.sources[0].reused_stages
    assert "selected" in reused and "bundled" in reused and "task_generated" in reused
    assert f"{fixture.task_id}:scorer_built" in reused
    assert second.published_task_ids == (fixture.task_id,)

    published_after = json.loads(provenance_path.read_text())["publication_sha256"]
    assert published_after == published_before


def test_validation_cache_hashes_referenced_candidate_directories(fixture: _Fixture) -> None:
    first = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    candidate = (
        fixture.paths.build_task_dir(first.build_id, fixture.task_id)
        / "scorer-built"
        / "cases"
        / "construct-preserving"
        / "src"
        / "feature.py"
    )
    write_text(candidate, CHANGING_FEATURE)

    second = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert second.published_task_ids == ()
    assert "SCORER_OVERFIT" in second.rejection_counts()


def test_validation_cache_hashes_reference_files(fixture: _Fixture) -> None:
    first = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    stage_path = fixture.paths.stage_path(first.build_id, SOURCE_ID)
    before = json.loads(stage_path.read_text())["tasks"][fixture.task_id]["fingerprints"][
        "scorer_validated"
    ]
    reference = (
        fixture.paths.build_task_dir(first.build_id, fixture.task_id)
        / "scorer-built"
        / "reference"
        / "reference.patch"
    )
    write_text(reference, "# changed reference\n")

    second = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    after = json.loads(stage_path.read_text())["tasks"][fixture.task_id]["fingerprints"][
        "scorer_validated"
    ]
    assert second.published_task_ids == (fixture.task_id,)
    assert after != before


def test_validation_cache_hashes_source_outcome_tree(fixture: _Fixture) -> None:
    build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    write_text(fixture.source_root / "repo" / "outcome" / "src" / "feature.py", CHANGING_FEATURE)
    manifest_path = fixture.source_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["content_sha256"] = compute_content_hash(fixture.source_root)
    write_json(manifest_path, manifest)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    assert "ORACLE_DOES_NOT_PASS" in result.rejection_counts()


def test_changed_agent_config_invalidates_the_generation_stage(fixture: _Fixture) -> None:
    build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    make_agent_config(fixture.definer_agent, "retro-task-definer-v1", "definer-model-v2")
    second = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert "task_generated" not in second.sources[0].reused_stages


def test_source_mutation_is_a_builder_contract_error(fixture: _Fixture) -> None:
    fixture.plan["artifact_runs"]["retro-task-definer-v1"]["mutate_source"] = True
    fixture.write_plan()
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    assert result.rejection_counts() == {"BUILDER_CONTRACT_ERROR": 1}
    rejection = result.rejections[0]
    assert rejection.stage == "task_generated"
    assert "mutated its input workspace" in rejection.detail
    state = json.loads(fixture.paths.stage_path(result.build_id, SOURCE_ID).read_text())
    assert state["status"] == "error"
    assert state["error"]["stage"] == "task_generated"
    assert "bundled" in state["completed"]


def test_agent_failure_status_maps_to_harness_error(fixture: _Fixture) -> None:
    fixture.plan["artifact_runs"]["retro-task-definer-v1"]["status"] = "model_unavailable"
    fixture.write_plan()
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.rejection_counts() == {"HARNESS_ERROR": 1}


def test_scorer_rejection_is_recorded_not_fabricated(fixture: _Fixture) -> None:
    outputs = fixture.tmp_path / "out" / "builder-rejects"
    write_json(
        outputs / "scorer-rejection.json",
        {"code": "NO_OBSERVABLE_OUTCOME", "detail": "no executable separation exists"},
    )
    fixture.plan["artifact_runs"]["retro-scorer-builder-v1"]["outputs"] = str(outputs)
    fixture.write_plan()

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    codes = result.rejection_counts()
    assert codes["NO_OBSERVABLE_OUTCOME"] == 2  # definer goal rejection + scorer rejection
    assert not fixture.paths.task_dir(fixture.task_id).exists()


def test_missing_mandatory_validation_case_is_a_contract_error(fixture: _Fixture) -> None:
    cases_path = fixture.builder_out / "validation-cases.json"
    payload = json.loads(cases_path.read_text())
    payload["cases"] = [case for case in payload["cases"] if case["kind"] != "regression"]
    cases_path.write_text(json.dumps(payload))

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    detail = [item for item in result.rejections if item.code == "BUILDER_CONTRACT_ERROR"][0].detail
    assert "missing mandatory cases: regression" in detail


def test_validation_case_id_cannot_escape_or_delete_scratch(fixture: _Fixture) -> None:
    cases_path = fixture.builder_out / "validation-cases.json"
    payload = json.loads(cases_path.read_text())
    payload["cases"][0]["id"] = "../../sentinel"
    cases_path.write_text(json.dumps(payload))
    build_id = default_build_id_for(fixture)
    sentinel = fixture.paths.build_task_dir(build_id, fixture.task_id) / "sentinel"
    write_text(sentinel / "keep.txt", "keep\n")

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    assert (sentinel / "keep.txt").read_text() == "keep\n"
    rejection = next(item for item in result.rejections if item.task_id == fixture.task_id)
    assert rejection.code == "BUILDER_CONTRACT_ERROR"
    assert "safe basename identifier" in rejection.detail


def test_weak_oracle_case_blocks_publication(fixture: _Fixture) -> None:
    # Point the oracle case at the unchanged base tree: the scorer now scores it 0.
    cases_path = fixture.builder_out / "validation-cases.json"
    payload = json.loads(cases_path.read_text())
    for case in payload["cases"]:
        if case["kind"] == "oracle":
            case["base_state"] = "base"
    cases_path.write_text(json.dumps(payload))

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert "ORACLE_DOES_NOT_PASS" in result.rejection_counts()
    assert not fixture.paths.task_dir(fixture.task_id).exists()


def test_audit_rejection_blocks_publication(fixture: _Fixture) -> None:
    make_audit_outputs(fixture.audit_out, decision="reject")
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    rejection = [item for item in result.rejections if item.stage == "audited"][0]
    assert rejection.code == "SCORER_OVERFIT"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("leakage", ["prompt reveals the reference patch"], "SCORER_UNSAFE"),
        ("missing_observables", ["requested behavior"], "NO_OBSERVABLE_OUTCOME"),
    ],
)
def test_audit_accept_cannot_override_findings(
    fixture: _Fixture, field: str, value: list[str], expected_code: str
) -> None:
    audit_path = fixture.audit_out / "audit.json"
    audit = json.loads(audit_path.read_text())
    audit[field] = value
    write_json(audit_path, audit)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert result.published_task_ids == ()
    rejection = next(item for item in result.rejections if item.stage == "audited")
    assert rejection.code == expected_code
    assert field in rejection.detail


def test_shared_builder_and_auditor_model_is_warned(fixture: _Fixture) -> None:
    make_agent_config(fixture.auditor_agent, "retro-scorer-auditor-v1", "builder-model")
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == (fixture.task_id,)
    assert any("disjoint model families" in warning for warning in result.sources[0].state.warnings)


def test_missing_auditor_requires_explicit_opt_out(fixture: _Fixture) -> None:
    with pytest.raises(BuildConfigurationError):
        build_sources(
            fixture.paths, fixture.config(scorer_auditor_agent=None), [SOURCE_ID]
        )
    result = build_sources(
        fixture.paths,
        fixture.config(scorer_auditor_agent=None, require_audit=False),
        [SOURCE_ID],
    )
    assert result.published_task_ids == (fixture.task_id,)
    provenance = json.loads(
        (fixture.paths.task_dir(fixture.task_id) / "private" / "provenance.json").read_text()
    )
    assert provenance["audit"]["skipped"] is True


def test_lint_rejections_stop_the_source_without_failing_it(fixture: _Fixture) -> None:
    result = build_sources(fixture.paths, fixture.config(lint=reject_all_lint), [SOURCE_ID])
    assert result.published_task_ids == ()
    source = result.sources[0]
    assert source.state.status == "ok"
    assert source.state.stage == "task_linted"
    assert "PROMPT_ORACLE_LEAKAGE" in result.rejection_counts()


def test_zero_tasks_is_a_successful_build(fixture: _Fixture) -> None:
    write_json(
        fixture.definer_out / "task-definitions.json",
        {
            "schema_version": "retro-task-definitions-v1",
            "source_id": SOURCE_ID,
            "tasks": [],
            "rejections": [
                {
                    "goal_event_ids": [f"{SOURCE_ID}:1"],
                    "code": "NO_STABLE_GOAL",
                    "detail": "conversation only",
                }
            ],
        },
    )
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert result.sources[0].state.status == "ok"
    assert result.rejection_counts() == {"NO_STABLE_GOAL": 1}


def test_too_many_replay_tasks_is_rejected(fixture: _Fixture) -> None:
    definitions = make_task_definitions(SOURCE_ID)
    template = definitions["tasks"][0]
    definitions["tasks"] = []
    for index in range(4):
        task = dict(template)
        task["candidate_id"] = f"goal-{index}"
        task["prompt"] = f"{TASK_PROMPT} variant {index}"
        definitions["tasks"].append(task)
    write_json(fixture.definer_out / "task-definitions.json", definitions)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert "MULTI_GOAL_NOT_SEPARABLE" in result.rejection_counts()


def test_wrong_task_definitions_schema_is_rejected(fixture: _Fixture) -> None:
    payload = make_task_definitions(SOURCE_ID)
    payload["schema_version"] = "retro-task-definitions-v0"
    write_json(fixture.definer_out / "task-definitions.json", payload)
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.rejection_counts() == {"BUILDER_CONTRACT_ERROR": 1}


def test_identical_base_and_outcome_trees_are_rejected(fixture: _Fixture) -> None:
    manifest_path = fixture.source_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo"]["outcome_tree"] = manifest["repo"]["base_tree"]
    write_json(manifest_path, manifest)
    manifest["content_sha256"] = compute_content_hash(fixture.source_root)
    write_json(manifest_path, manifest)
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.rejection_counts() == {"OUTCOME_NOT_DURABLE": 1}


def test_selection_rejection_short_circuits_the_build(fixture: _Fixture) -> None:
    write_json(
        fixture.source_root / "selection.json",
        {"status": "rejected", "code": "DIRTY_START_STATE", "detail": "worktree was dirty"},
    )
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.rejection_counts() == {"DIRTY_START_STATE": 1}
    assert result.sources[0].state.status == "error"


def test_scorer_package_validation_enforces_the_manifest(tmp_path: Path) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123")
    manifest, package_sha256, warnings = validate_scorer_package(package, "task123")
    assert manifest["mode"] == "deterministic"
    assert len(package_sha256) == 64
    assert warnings == []

    payload = json.loads((package / "scorer.json").read_text())
    payload["components"][0]["weight"] = 0.5
    (package / "scorer.json").write_text(json.dumps(payload))
    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(package, "task123")
    assert excinfo.value.code == "BUILDER_CONTRACT_ERROR"
    assert "weights sum to" in excinfo.value.detail


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("image", "python:latest", "pinned by a sha256 digest"),
        ("network", "enabled", "network"),
        ("candidate_mount", "read_write", "candidate_mount"),
    ],
)
def test_scorer_package_rejects_unsafe_runtime(
    tmp_path: Path, field: str, value: str, detail: str
) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123")
    payload = json.loads((package / "scorer.json").read_text())
    payload["runtime"][field] = value
    write_json(package / "scorer.json", payload)

    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(package, "task123")

    assert excinfo.value.code == "SCORER_UNSAFE"
    assert detail in excinfo.value.detail


def test_scorer_package_rejects_hybrid_without_pinned_judge(tmp_path: Path) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123", mode="hybrid")
    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(package, "task123")
    assert "pinned judge.agent_config" in excinfo.value.detail


def test_scorer_package_safety_scan_rejects_oracle_references(tmp_path: Path) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123")
    write_text(package / "helper.py", "ORACLE = 'repo/outcome/src/feature.py'\n")
    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(
            package, "task123", forbidden_substrings=("repo/outcome",)
        )
    assert excinfo.value.code == "SCORER_UNSAFE"


def test_scorer_package_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123")
    payload = json.loads((package / "scorer.json").read_text())
    payload["package_sha256"] = "0" * 64
    (package / "scorer.json").write_text(json.dumps(payload))
    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(package, "task123")
    assert excinfo.value.code == "BUILDER_CONTRACT_ERROR"
    assert "differs from Retro's computed" in excinfo.value.detail


@pytest.mark.parametrize(
    ("kind", "report", "expected_code"),
    [
        (
            "base",
            {"status": "scored", "score_total": 0.5, "components": [], "hard_gate_failures": ["x"]},
            "BASE_ALREADY_PASSES",
        ),
        (
            "oracle",
            {"status": "scored", "score_total": 0.5, "components": [], "hard_gate_failures": []},
            "ORACLE_DOES_NOT_PASS",
        ),
        (
            "no_op",
            {
                "status": "scored",
                "score_total": 1.0,
                "passed": True,
                "components": [],
                "hard_gate_failures": [],
            },
            "BASE_ALREADY_PASSES",
        ),
        (
            "regression",
            {"status": "scored", "score_total": 0.9, "components": [], "hard_gate_failures": []},
            "NO_OBSERVABLE_OUTCOME",
        ),
        (
            "base",
            {"status": "scorer_error", "components": []},
            "BUILDER_CONTRACT_ERROR",
        ),
    ],
)
def test_validation_requirement_table(kind: str, report: dict, expected_code: str) -> None:
    result = evaluate_validation_case(
        kind,
        f"case-{kind}",
        [report],
        oracle_total=1.0,
        targeted_component="requested_behavior",
        oracle_components={"requested_behavior": 1.0},
    )
    assert result.ok is False
    assert result.code == expected_code


def test_construct_mutant_requirements() -> None:
    changing = evaluate_validation_case(
        "construct_changing",
        "mutant",
        [
            {
                "status": "scored",
                "score_total": 0.9,
                "components": [{"id": "requested_behavior", "value": 0.8, "weight": 1.0}],
                "hard_gate_failures": [],
            }
        ],
        oracle_total=1.0,
        targeted_component="requested_behavior",
        oracle_components={"requested_behavior": 1.0},
    )
    assert changing.ok is False and changing.code == "NO_OBSERVABLE_OUTCOME"

    preserving = evaluate_validation_case(
        "construct_preserving",
        "mutant",
        [{"status": "scored", "score_total": 0.8, "components": [], "hard_gate_failures": []}],
        oracle_total=1.0,
        targeted_component=None,
        oracle_components={},
    )
    assert preserving.ok is False and preserving.code == "SCORER_OVERFIT"


def test_repeatability_detects_nondeterministic_components() -> None:
    reports = [
        {
            "status": "scored",
            "score_total": 1.0,
            "components": [{"id": "requested_behavior", "value": 1.0}],
        },
        {
            "status": "scored",
            "score_total": 1.0,
            "components": [{"id": "requested_behavior", "value": 0.5}],
        },
    ]
    result = evaluate_repeatability(
        {"oracle": reports}, {"requested_behavior": "deterministic"}, ["requested_behavior"]
    )
    assert result.ok is False
    assert result.deterministic_mismatches == ("oracle:requested_behavior",)

    stable = evaluate_repeatability(
        {"oracle": [reports[0], reports[0]]},
        {"requested_behavior": "deterministic"},
        ["requested_behavior"],
    )
    assert stable.ok is True
    assert stable.performance_spread == 0.0


def test_repeatability_flags_unstable_judge_hard_gate() -> None:
    reports = [
        {
            "status": "scored",
            "score_total": 0.9,
            "components": [{"id": "project_fit", "value": 1.0}],
        },
        {
            "status": "scored",
            "score_total": 0.5,
            "components": [{"id": "project_fit", "value": 0.0}],
        },
    ]
    result = evaluate_repeatability({"oracle": reports}, {"project_fit": "judge"}, ["project_fit"])
    assert result.ok is False
    assert "project_fit" in result.detail
    assert isinstance(result, RepeatabilityResult)


@pytest.mark.parametrize(
    "bad_report",
    [
        {
            "status": "judge_unavailable",
            "components": [{"id": "requested_behavior", "value": 1.0}],
        },
        {"status": "scored", "score_total": 1.0, "components": []},
    ],
)
def test_repeatability_rejects_failed_or_incomplete_repeats(
    bad_report: dict[str, Any],
) -> None:
    good_report = {
        "status": "scored",
        "score_total": 1.0,
        "components": [{"id": "requested_behavior", "value": 1.0}],
    }
    result = evaluate_repeatability(
        {"oracle": [good_report, bad_report]},
        {"requested_behavior": "deterministic"},
        ["requested_behavior"],
        expected_repeats=2,
    )

    assert result.ok is False
    assert "repeat 1" in result.detail


def test_stage_state_is_atomic_and_invalidates_downstream(tmp_path: Path) -> None:
    store = StageStore(tmp_path / "stage.json")
    state = store.load("src-1")
    assert isinstance(state, SourceStageState)
    state = store.save(state.advance("selected", "fp-a"))
    state = store.save(state.advance("bundled", "fp-b"))
    assert state.reached("selected", "fp-a")
    assert not state.reached("selected", "changed")

    reloaded = StageStore(tmp_path / "stage.json").load("src-1")
    assert reloaded.completed == ("selected", "bundled")
    assert reloaded.fingerprints == {"selected": "fp-a", "bundled": "fp-b"}

    rewound = reloaded.advance("selected", "fp-a2")
    assert rewound.completed == ("selected",)
    assert "bundled" not in rewound.fingerprints

    failed = rewound.fail("bundled", "HARNESS_ERROR", "boom")
    assert failed.status == "error"
    assert failed.completed == ("selected",)


def test_stage_store_rejects_a_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "stage.json"
    path.write_text(json.dumps({"schema_version": "other", "source_id": "src-1"}))
    with pytest.raises(BuildConfigurationError):
        StageStore(path).load("src-1")


def test_lint_outcome_coercion_accepts_several_shapes() -> None:
    mapping = coerce_lint_outcome({"tasks": [{"task_id": "a"}], "rejections": [{"code": "X"}]})
    assert mapping.accepted[0]["task_id"] == "a"
    assert mapping.findings[0].code == "X"

    tupled = coerce_lint_outcome(([{"task_id": "b"}], [LintFinding("Y", "detail")]))
    assert tupled.accepted[0]["task_id"] == "b"
    assert tupled.findings[0].detail == "detail"

    assert coerce_lint_outcome(LintOutcome()) == LintOutcome()
    with pytest.raises(BuildConfigurationError):
        coerce_lint_outcome(42)


def test_resolve_lint_fn_explains_a_missing_foundation_module() -> None:
    with pytest.raises(BuildConfigurationError) as excinfo:
        resolve_lint_fn("retro.benchmarks.task_scorer.__definitely_missing__")
    assert "BuildConfig(lint=...)" in str(excinfo.value)


def test_pack_directory_is_byte_identical_for_identical_trees(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        write_text(root / "src" / "a.py", "print('a')\n")
        write_text(root / "README.md", "# demo\n")
    write_text(left / ".git" / "HEAD", "ref: refs/heads/main\n")

    first = pack_directory(left, tmp_path / "left.tar")
    second = pack_directory(right, tmp_path / "right.tar")
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        assert all(not name.startswith(".git") for name in archive.getnames())


def test_build_taskset_discovers_sources_and_writes_a_report(fixture: _Fixture) -> None:
    result = build_sources(fixture.paths, fixture.config())
    report_path = fixture.paths.build_run_dir(result.build_id) / "build.json"
    payload = json.loads(report_path.read_text())
    assert payload["schema_version"] == "retro-taskset-build-v1"
    assert payload["published_task_ids"] == [fixture.task_id]
    assert payload["sources"][0]["source_id"] == SOURCE_ID


def test_new_build_atomically_replaces_the_active_task_set(fixture: _Fixture) -> None:
    first = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    active_path = fixture.paths.active_tasks_path()
    active = json.loads(active_path.read_text())
    assert active == {
        "schema_version": ACTIVE_TASKS_SCHEMA,
        "name": "pilot",
        "build_id": first.build_id,
        "task_ids": [fixture.task_id],
    }

    definitions = make_task_definitions(SOURCE_ID)
    definitions["tasks"] = []
    write_json(fixture.definer_out / "task-definitions.json", definitions)
    make_agent_config(fixture.definer_agent, "retro-task-definer-v1", "definer-model-v2")
    second = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])

    assert second.published_task_ids == ()
    assert fixture.paths.task_dir(fixture.task_id).is_dir()
    assert list_published_tasks(fixture.paths) == []
    assert json.loads(active_path.read_text())["build_id"] == second.build_id

    active_path.unlink()
    assert list_published_tasks(fixture.paths) == [fixture.task_id]


def test_build_source_accepts_an_explicit_bundle_location(fixture: _Fixture) -> None:
    relocated = fixture.tmp_path / "elsewhere" / SOURCE_ID
    relocated.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture.source_root, relocated)
    shutil.rmtree(fixture.source_root)

    result = build_source(
        fixture.paths,
        fixture.config(),
        SOURCE_ID,
        build_id="build-explicit",
        source_dir=relocated,
    )
    assert result.published_task_ids == (fixture.task_id,)


def test_layout_helpers_are_used_when_available(tmp_path: Path) -> None:
    class FakeLayout:
        def benchmark_taskset_dir(self, name: str) -> Path:
            return tmp_path / "custom" / name

        def benchmark_taskset_tasks_dir(self, name: str) -> Path:
            return tmp_path / "custom" / name / "published"

        def benchmark_taskset_attempt_dir(
            self, name: str, eval_id: str, task_id: str, agent_id: str, seed: int
        ) -> Path:
            return tmp_path / "custom" / name / eval_id / task_id / agent_id / str(seed)

    paths = TasksetPaths.from_layout(FakeLayout(), "pilot")
    assert paths.root == tmp_path / "custom" / "pilot"
    assert paths.tasks_dir() == tmp_path / "custom" / "pilot" / "published"
    assert paths.sources_dir() == tmp_path / "custom" / "pilot" / "sources"
    assert paths.attempt_dir("e1", "t1", "a1", 3) == (
        tmp_path / "custom" / "pilot" / "e1" / "t1" / "a1" / "3"
    )

    class LegacyLayout:
        def benchmark_dir(self, benchmark_id: str) -> Path:
            return tmp_path / "benchmarks" / benchmark_id

    legacy = TasksetPaths.from_layout(LegacyLayout(), "pilot")
    assert legacy.root == tmp_path / "benchmarks" / "pilot" / "task-scorer"
    assert legacy.results_path("e1") == legacy.root / "evals" / "e1" / "results.json"

    with pytest.raises(BuildConfigurationError):
        TasksetPaths.from_layout(object(), "pilot")


def test_validation_cases_must_name_the_task(fixture: _Fixture) -> None:
    cases_path = fixture.builder_out / "validation-cases.json"
    payload = json.loads(cases_path.read_text())
    payload["task_id"] = "some-other-task"
    cases_path.write_text(json.dumps(payload))

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    detail = [item for item in result.rejections if item.code == "BUILDER_CONTRACT_ERROR"][0].detail
    assert "does not match" in detail


def test_scorer_whose_total_contradicts_its_components_is_rejected(fixture: _Fixture) -> None:
    score_py = fixture.builder_out / "scorer" / "score.py"
    score_py.write_text(
        score_py.read_text().replace('"score_total": round(total, 6),', '"score_total": 0.5,')
    )
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    detail = [item for item in result.rejections if item.code == "BUILDER_CONTRACT_ERROR"][0].detail
    assert "invalid score report" in detail


def test_base_must_fail_the_declared_requested_behavior_gate(fixture: _Fixture) -> None:
    cases_path = fixture.builder_out / "validation-cases.json"
    payload = json.loads(cases_path.read_text())
    for case in payload["cases"]:
        if case["kind"] == "base":
            case["component"] = "regression_suite"
    cases_path.write_text(json.dumps(payload))

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert "BASE_ALREADY_PASSES" in result.rejection_counts()


def test_check_builder_contract_allows_a_mutable_candidate_workspace(fixture: _Fixture) -> None:
    from retro.benchmarks.task_scorer.build import check_builder_contract
    from retro.benchmarks.task_scorer.ghostlab_cli import ArtifactRunRequest, ExportSpec

    fixture.plan["artifact_runs"]["retro-task-definer-v1"]["mutate_source"] = True
    fixture.write_plan()
    client = fixture.client()
    prompt = write_text(fixture.tmp_path / "p.md", "go\n")
    result = client.artifact_run(
        ArtifactRunRequest(
            agent_config=fixture.definer_agent,
            workspace=fixture.source_root,
            prompt_file=prompt,
            run_dir=fixture.tmp_path / "raw-run",
            exports=(
                ExportSpec("/sandbox/output/task-definitions.json", "task-definitions.json"),
            ),
        )
    )
    assert result.workspace_mutated
    with pytest.raises(StageFailure):
        check_builder_contract(result, stage="task_generated")
    check_builder_contract(result, stage="candidate", immutable_workspace=False)


def test_build_uses_the_foundation_task_lint_by_default(fixture: _Fixture) -> None:
    """No injected lint: build must resolve and drive retro ... task_scorer.task_lint."""
    from retro.benchmarks import task_scorer

    schema = Path(task_scorer.__file__).parent / "schemas" / "task-definitions.schema.json"
    result = build_sources(
        fixture.paths,
        fixture.config(lint=None, task_definitions_schema=schema if schema.is_file() else None),
        [SOURCE_ID],
    )
    assert result.published_task_ids == (fixture.task_id,)
    assert "NO_OBSERVABLE_OUTCOME" in result.rejection_counts()


def test_foundation_lint_rejects_an_oracle_leaking_prompt(fixture: _Fixture) -> None:
    definitions = make_task_definitions(SOURCE_ID)
    definitions["tasks"][0]["prompt"] = (
        "Add src/feature.py containing def greet(name): return f\"hello, world from {name}\" "
        "exactly as in the accepted implementation."
    )
    write_json(fixture.definer_out / "task-definitions.json", definitions)

    result = build_sources(fixture.paths, fixture.config(lint=None), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert "PROMPT_ORACLE_LEAKAGE" in result.rejection_counts()


def test_foundation_lint_rejects_unknown_evidence_paths(fixture: _Fixture) -> None:
    definitions = make_task_definitions(SOURCE_ID)
    definitions["tasks"][0]["repo_evidence"] = [
        {"state": "base", "path": "src/does_not_exist.py", "reason": "missing"}
    ]
    write_json(fixture.definer_out / "task-definitions.json", definitions)

    result = build_sources(fixture.paths, fixture.config(lint=None), [SOURCE_ID])
    assert result.published_task_ids == ()
    assert result.rejections


def test_taskset_paths_from_the_real_archive_layout(tmp_path: Path) -> None:
    from retro.storage import Layout

    layout = Layout(tmp_path / "archive")
    paths = TasksetPaths.from_layout(layout, "pilot")
    assert paths.root == layout.benchmark_taskset_dir("pilot")
    assert paths.sources_dir() == layout.benchmark_taskset_sources_dir("pilot")
    assert paths.source_dir(SOURCE_ID) == layout.benchmark_taskset_source_dir("pilot", SOURCE_ID)
    assert paths.tasks_dir() == layout.benchmark_taskset_tasks_dir("pilot")
    assert paths.task_dir("t1") == layout.benchmark_taskset_task_dir("pilot", "t1")
    assert paths.build_run_dir("b1") == layout.benchmark_taskset_build_run_dir("pilot", "b1")
    assert paths.eval_dir("e1") == layout.benchmark_taskset_eval_dir("pilot", "e1")
    assert paths.attempt_dir("e1", "t1", "a1", 2) == layout.benchmark_taskset_attempt_dir(
        "pilot", "e1", "t1", "a1", 2
    )
    assert paths.results_path("e1") == layout.benchmark_taskset_results_path("pilot", "e1")


def test_packaged_instruction_helpers_expose_the_shipped_files() -> None:
    from retro.benchmarks.task_scorer.build import (
        INSTRUCTION_ASSET_DIR,
        PACKAGED_INSTRUCTIONS,
        instruction_path,
        instruction_sha256,
        instruction_text,
        packaged_instruction_index,
    )

    assert set(PACKAGED_INSTRUCTIONS) == {
        "task-definer",
        "scorer-builder",
        "scorer-auditor",
        "residual-judge",
    }
    for name, filename in PACKAGED_INSTRUCTIONS.items():
        path = instruction_path(name)
        assert path == INSTRUCTION_ASSET_DIR / filename
        assert path.is_file()
        assert instruction_text(name) == path.read_text(encoding="utf-8")
        assert len(instruction_sha256(name)) == 64

    # The pipeline reads the shipped text instead of restating the instructions.
    assert "/sandbox/output/task-definitions.json" in instruction_text("task-definer")
    assert "/sandbox/output/audit.json" in instruction_text("scorer-auditor")
    assert "CANNOT_ASSESS" in instruction_text("residual-judge")

    index = packaged_instruction_index()
    assert index["task-definer.md"] == instruction_sha256("task-definer")

    with pytest.raises(BuildConfigurationError):
        instruction_path("nope")


def test_build_config_defaults_to_the_packaged_output_contracts(fixture: _Fixture) -> None:
    config = fixture.config()
    assert config.definitions_contract().name == "task-definitions.schema.json"
    assert config.audit_contract().name == "scorer-audit.schema.json"
    assert config.definitions_contract().is_file()

    override = fixture.tmp_path / "custom.schema.json"
    write_json(override, {"type": "object"})
    assert fixture.config(task_definitions_schema=override).definitions_contract() == override
    assert fixture.config(scorer_audit_schema=override).audit_contract() == override


def test_definer_run_passes_the_packaged_contract_to_ghostlab(fixture: _Fixture) -> None:
    build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    stage = json.loads(
        fixture.paths.stage_path(
            default_build_id_for(fixture), SOURCE_ID
        ).read_text()
    )
    assert stage["status"] == "ok"

    provenance = json.loads(
        (fixture.paths.task_dir(fixture.task_id) / "private" / "provenance.json").read_text()
    )
    assets = provenance["assets"]
    assert set(assets["contracts"]) == {"task-definitions", "scorer-audit", "score-report"}
    assert all(len(value) == 64 for value in assets["contracts"].values())
    assert assets["packaged_instructions"]["scorer-builder.md"]
    assert assets["agents"]["task_definer"]["config_sha256"]
    assert assets["agents"]["task_definer"]["instructions"] == {
        "instructions/task-definer.md": assets["packaged_instructions"]["task-definer.md"]
    }


def default_build_id_for(fixture: _Fixture) -> str:
    from retro.benchmarks.task_scorer.build import default_build_id

    return default_build_id("pilot", fixture.config(), [SOURCE_ID])


def test_task_definitions_violating_the_packaged_contract_are_rejected(fixture: _Fixture) -> None:
    definitions = make_task_definitions(SOURCE_ID)
    definitions["tasks"][0]["confidence"] = {"goal": 1.4, "state": 1.0, "scorability": 0.9}
    write_json(fixture.definer_out / "task-definitions.json", definitions)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    detail = [item for item in result.rejections if item.code == "BUILDER_CONTRACT_ERROR"][0].detail
    assert "violates" in detail and "task-definitions.schema.json" in detail


def test_audit_violating_the_packaged_contract_is_rejected(fixture: _Fixture) -> None:
    write_json(
        fixture.audit_out / "audit.json",
        {"decision": "accept", "leakage": [], "evidence": ["scorer.json"]},
    )
    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert result.published_task_ids == ()
    detail = [item for item in result.rejections if item.stage == "audited"][0].detail
    assert "audit.json violates" in detail
    assert "scorer-audit.schema.json" in detail


def test_agent_instruction_drift_is_warned_and_changes_the_fingerprint(fixture: _Fixture) -> None:
    from retro.benchmarks.task_scorer.build import agent_instruction_hashes, instruction_sha256

    hashes, warnings = agent_instruction_hashes(fixture.definer_agent)
    assert hashes == {"instructions/task-definer.md": instruction_sha256("task-definer")}
    assert warnings == []

    local = fixture.definer_agent.parent / "instructions" / "task-definer.md"
    write_text(local, "You are a helpful assistant.\n")
    drifted, warnings = agent_instruction_hashes(fixture.definer_agent)
    assert drifted["instructions/task-definer.md"] != hashes["instructions/task-definer.md"]
    assert any("differs from the packaged" in warning for warning in warnings)

    result = build_sources(fixture.paths, fixture.config(), [SOURCE_ID])
    assert any("differs from the packaged" in item for item in result.sources[0].state.warnings)


def test_unresolvable_agent_instruction_is_warned(fixture: _Fixture) -> None:
    from retro.benchmarks.task_scorer.build import agent_instruction_hashes

    write_json(
        fixture.definer_agent,
        {
            "id": "retro-task-definer-v1",
            "runtime": {"model": "definer-model", "instructions": ["instructions/absent.md"]},
        },
    )
    hashes, warnings = agent_instruction_hashes(fixture.definer_agent)
    assert hashes == {"instructions/absent.md": ""}
    assert any("does not resolve to a file" in warning for warning in warnings)


def test_judge_mode_scorer_must_ship_its_declared_judge_files(tmp_path: Path) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123", mode="hybrid")
    payload = json.loads((package / "scorer.json").read_text())
    payload["judge"] = {
        "enabled": True,
        "agent_config": "/scorer/judge-agent.json",
        "prompt": "/scorer/judge.prompt.md",
        "output_schema": "/scorer/judge.schema.json",
        "criteria": ["project_fit"],
    }
    (package / "scorer.json").write_text(json.dumps(payload))

    with pytest.raises(StageFailure) as excinfo:
        validate_scorer_package(package, "task123")
    assert "judge.agent_config" in excinfo.value.detail

    from retro.benchmarks.task_scorer.build import instruction_text

    write_json(
        package / "judge-agent.json",
        {
            "id": "retro-residual-judge-v1",
            "runtime": {
                "model": "judge-model",
                "tools": {"bash": False, "webfetch": False},
                "permission": {
                    "bash": "deny",
                    "edit": "deny",
                    "external_directory": "deny",
                },
            },
        },
    )
    write_text(package / "judge.prompt.md", instruction_text("residual-judge"))
    write_json(package / "judge.schema.json", {"type": "object"})
    manifest, _, _ = validate_scorer_package(package, "task123")
    assert manifest["mode"] == "hybrid"


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        ({"model": ""}, "runtime.model"),
        ({"tools": {"bash": True, "webfetch": False}}, "tools.bash"),
        ({"tools": {"bash": False, "webfetch": True}}, "tools.webfetch"),
        (
            {
                "permission": {
                    "bash": "allow",
                    "edit": "deny",
                    "external_directory": "deny",
                }
            },
            "permission.bash",
        ),
        (
            {
                "permission": {
                    "bash": "deny",
                    "edit": "allow",
                    "external_directory": "deny",
                }
            },
            "permission.edit",
        ),
        (
            {
                "permission": {
                    "bash": "deny",
                    "edit": "deny",
                    "external_directory": "allow",
                }
            },
            "permission.external_directory",
        ),
    ],
)
def test_judge_agent_must_be_pinned_and_read_only(
    tmp_path: Path, mutation: dict[str, Any], detail: str
) -> None:
    package = make_scorer_package(tmp_path / "scorer", "task123", mode="hybrid")
    scorer = json.loads((package / "scorer.json").read_text())
    scorer["judge"] = {
        "enabled": True,
        "agent_config": "/scorer/judge-agent.json",
        "prompt": "/scorer/judge.prompt.md",
        "output_schema": "/scorer/judge.schema.json",
        "criteria": ["regression_suite"],
    }
    write_json(package / "scorer.json", scorer)
    runtime: dict[str, Any] = {
        "model": "judge-model",
        "tools": {"bash": False, "webfetch": False},
        "permission": {
            "bash": "deny",
            "edit": "deny",
            "external_directory": "deny",
        },
    }
    runtime.update(mutation)
    write_json(package / "judge-agent.json", {"runtime": runtime})
    write_text(package / "judge.prompt.md", "judge\n")
    write_json(package / "judge.schema.json", {"type": "object"})

    with pytest.raises(StageFailure, match=detail):
        validate_scorer_package(package, "task123")
