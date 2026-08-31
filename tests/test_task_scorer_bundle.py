"""Immutable, content-addressed SourceBundle construction."""
from __future__ import annotations

import dataclasses
import json
import stat
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle as bundle_mod
from retro.benchmarks.task_scorer import selection
from retro.benchmarks.task_scorer.schema import ProjectEnvironment, SourceBundleManifest, TaskLimits
from retro.storage import Layout
from tests.task_scorer_helpers import (
    install_session,
    make_repo,
    project_environment,
    repo_state_json,
    rollout_events,
)


@pytest.fixture
def source(tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    install_session(
        layout,
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])
        },
    )
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))
    candidate, rejection = selection.select_source(
        layout=layout,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=lambda _candidate: environment,
    )
    assert rejection is None and candidate is not None
    return layout, candidate, shas


def test_bundle_layout_and_manifest(source):
    layout, candidate, shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    path = built.path
    assert path == layout.benchmark_taskset_source_dir("pilot", "codex__session-1")
    for relative in bundle_mod.BUNDLE_LAYOUT:
        assert (path / relative).exists(), relative

    manifest = SourceBundleManifest.from_dict(
        json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    )
    assert manifest.repo.base_sha == shas["base_sha"]
    assert manifest.repo.outcome_sha == shas["outcome_sha"]
    assert manifest.repo.base_resolution == "captured_start"
    assert manifest.task_limits == TaskLimits(max_replay_tasks=3, adjacent_per_replay=0)
    assert manifest.content_sha256 is not None
    assert bundle_mod.verify_bundle(path)


def test_bundle_contents_are_the_two_repository_states(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    base_calc = (built.path / "repo" / "base" / "src" / "calc.py").read_text(encoding="utf-8")
    outcome_calc = (built.path / "repo" / "outcome" / "src" / "calc.py").read_text(encoding="utf-8")
    assert "divide" not in base_calc
    assert "division by zero" in outcome_calc
    assert not (built.path / "repo" / "base" / ".git").exists()

    patch = (built.path / "repo" / "change.patch").read_text(encoding="utf-8")
    assert "def divide" in patch

    log_lines = [
        json.loads(line)
        for line in (built.path / "repo" / "git-log.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["subject"] for entry in log_lines] == ["add divide with zero guard"]

    transcript = (built.path / "rollout" / "transcript.md").read_text(encoding="utf-8")
    assert "Guard divide against zero" in transcript


def test_identical_inputs_produce_identical_content_hashes(tmp_path: Path, source):
    layout, candidate, _shas = source
    first = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    other_layout = Layout(tmp_path / "archive-2")
    other_layout.ensure()
    second = bundle_mod.build_source_bundle(candidate, layout=other_layout, name="pilot")
    assert first.content_sha256 == second.content_sha256
    assert (
        bundle_mod.compute_content_hash(first.path)
        == bundle_mod.compute_content_hash(second.path)
    )


def test_content_hash_changes_when_a_bundle_file_changes(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    before = bundle_mod.compute_content_hash(built.path)
    (built.path / "repo" / "base" / "README.md").write_text("tampered\n", encoding="utf-8")
    assert bundle_mod.compute_content_hash(built.path) != before
    assert bundle_mod.verify_bundle(built.path) is False


def test_content_hash_changes_when_executable_mode_changes(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    path = built.path / "repo" / "base" / "README.md"
    before = bundle_mod.compute_content_hash(built.path)

    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    assert bundle_mod.compute_content_hash(built.path) != before
    assert bundle_mod.verify_bundle(built.path) is False


def test_content_hash_changes_when_symlink_target_changes(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    path = built.path / "repo" / "base" / "README.md"
    before = bundle_mod.compute_content_hash(built.path)

    path.unlink()
    path.symlink_to("pyproject.toml")
    first_target = bundle_mod.compute_content_hash(built.path)
    path.unlink()
    path.symlink_to("src/calc.py")

    assert first_target != before
    assert bundle_mod.compute_content_hash(built.path) != first_target
    assert bundle_mod.verify_bundle(built.path) is False


def test_rebuild_reuses_verified_bundle_and_rejects_tampering(source):
    layout, candidate, _shas = source
    first = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    reused = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    assert reused.manifest.content_sha256 == first.manifest.content_sha256

    marker = first.path / "repo" / "base" / "MARKER"
    marker.write_text("x", encoding="utf-8")
    with pytest.raises(bundle_mod.BundleError, match="checksum"):
        bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")

    rebuilt = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot", force=True)
    assert not marker.exists()
    assert rebuilt.content_sha256 == first.content_sha256


@pytest.mark.parametrize("changed_input", ["rollout", "environment", "anchors", "task_limits"])
def test_reuse_requires_matching_complete_input_fingerprint(
    source, tmp_path: Path, changed_input: str
):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    changed_candidate = candidate
    limits = TaskLimits()

    if changed_input == "rollout":
        changed_events = tmp_path / "changed-events.jsonl"
        changed_events.write_text(
            candidate.normalized_path.read_text(encoding="utf-8").replace(
                "Guard divide", "Prevent divide", 1
            ),
            encoding="utf-8",
        )
        changed_candidate = dataclasses.replace(candidate, normalized_path=changed_events)
    elif changed_input == "environment":
        assert candidate.environment is not None
        environment = candidate.environment.to_dict()
        environment["test"] = [["python", "-m", "pytest", "-q"]]
        changed_candidate = dataclasses.replace(
            candidate,
            environment=ProjectEnvironment.from_dict(environment),
        )
    elif changed_input == "anchors":
        changed_candidate = dataclasses.replace(
            candidate,
            outcome=dataclasses.replace(
                candidate.outcome,
                resolution="captured_end",
            ),
        )
    else:
        limits = TaskLimits(max_replay_tasks=2)

    with pytest.raises(bundle_mod.BundleError, match="inputs differ"):
        bundle_mod.build_source_bundle(
            changed_candidate,
            layout=layout,
            name="pilot",
            task_limits=limits,
        )

    assert bundle_mod.verify_bundle(built.path)
    assert bundle_mod.load_bundle(built.path).content_sha256 == built.content_sha256


def test_manifest_fields_are_covered_by_content_hash(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    manifest_path = built.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task_limits"]["max_replay_tasks"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert bundle_mod.verify_bundle(built.path) is False


def test_project_files_context_is_deterministic_and_llm_free(source):
    layout, candidate, shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    payload = json.loads(
        (built.path / "context" / "project-files.json").read_text(encoding="utf-8")
    )
    paths = {entry["path"] for entry in payload["files"]}
    assert {"README.md", "pyproject.toml"} <= paths
    assert "src/calc.py" not in paths
    assert payload["languages"]["python"] >= 2
    assert payload["base_sha"] == shas["base_sha"]
    assert "summary" not in payload

    again = bundle_mod.build_project_files(
        candidate.repo_root, shas["base_sha"], built.path / "repo" / "base"
    )
    assert again == payload


def test_test_commands_copy_validated_environment(source):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    payload = json.loads(
        (built.path / "context" / "test-commands.json").read_text(encoding="utf-8")
    )
    assert payload["test"] == [[".venv/bin/pytest", "-q"]]
    assert payload["smoke"] == [[".venv/bin/pytest", "--collect-only", "-q"]]
    assert payload["environment_id"] == "sha256:" + "a" * 64


def test_bundle_publication_is_atomic(source, monkeypatch):
    layout, candidate, _shas = source

    def explode(*args, **kwargs):
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(bundle_mod.git_state, "materialize_tree", explode)
    with pytest.raises(RuntimeError, match="materialization failed"):
        bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    target = layout.benchmark_taskset_source_dir("pilot", "codex__session-1")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_failed_forced_construction_preserves_existing_bundle(source, monkeypatch):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    manifest_before = (built.path / "manifest.json").read_bytes()

    def explode(*args, **kwargs):
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(bundle_mod.git_state, "materialize_tree", explode)
    with pytest.raises(RuntimeError, match="materialization failed"):
        bundle_mod.build_source_bundle(
            candidate,
            layout=layout,
            name="pilot",
            force=True,
        )

    assert (built.path / "manifest.json").read_bytes() == manifest_before
    assert bundle_mod.verify_bundle(built.path)
    assert [path.name for path in built.path.parent.iterdir()] == [built.path.name]


def test_failed_forced_publish_rolls_back_existing_bundle(source, monkeypatch):
    layout, candidate, _shas = source
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    manifest_before = (built.path / "manifest.json").read_bytes()
    real_replace = bundle_mod.os.replace
    failed = False

    def fail_staged_publish(source_path, destination_path):
        nonlocal failed
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            not failed
            and destination_path == built.path
            and source_path != built.path
            and not source_path.name.endswith(".previous")
        ):
            failed = True
            raise OSError("publish failed")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(bundle_mod.os, "replace", fail_staged_publish)
    with pytest.raises(OSError, match="publish failed"):
        bundle_mod.build_source_bundle(
            candidate,
            layout=layout,
            name="pilot",
            force=True,
        )

    assert failed
    assert (built.path / "manifest.json").read_bytes() == manifest_before
    assert bundle_mod.verify_bundle(built.path)
    assert [path.name for path in built.path.parent.iterdir()] == [built.path.name]


def test_bundle_preserves_raw_and_normalized(source):
    layout, candidate, _shas = source
    normalized_before = candidate.normalized_path.read_bytes()
    raw_before = {
        path.name: path.read_bytes()
        for path in candidate.raw_dir.iterdir()
        if path.is_file()
    }
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    assert candidate.normalized_path.read_bytes() == normalized_before
    assert {
        path.name: path.read_bytes() for path in candidate.raw_dir.iterdir() if path.is_file()
    } == raw_before
    assert (built.path / "rollout" / "events.jsonl").read_bytes() == normalized_before


def test_adjacent_generation_requires_environment(source):
    layout, candidate, _shas = source
    stripped = candidate.__class__(
        source_id=candidate.source_id,
        host=candidate.host,
        session_id=candidate.session_id,
        normalized_path=candidate.normalized_path,
        raw_dir=candidate.raw_dir,
        repo_root=candidate.repo_root,
        cwd=candidate.cwd,
        branch=candidate.branch,
        started_at=candidate.started_at,
        ended_at=candidate.ended_at,
        base=candidate.base,
        outcome=candidate.outcome,
        events=candidate.events,
        environment=None,
    )
    with pytest.raises(bundle_mod.BundleError):
        bundle_mod.build_source_bundle(
            stripped,
            layout=layout,
            name="pilot",
            task_limits=TaskLimits(max_replay_tasks=3, adjacent_per_replay=1),
        )
