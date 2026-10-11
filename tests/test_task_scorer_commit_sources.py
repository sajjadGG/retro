"""Commit-mined task sources: enumeration filters, session linkage and bundles."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle as bundle_mod
from retro.benchmarks.task_scorer import commit_sources, selection
from retro.benchmarks.task_scorer.schema import ProjectEnvironment, SourceBundleManifest
from retro.storage import Layout
from tests.task_scorer_helpers import (
    event,
    git,
    install_session,
    project_environment,
)

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _commit(root: Path, message: str, *, when: str) -> str:
    git(root, "add", "-A")
    git(
        root,
        "commit",
        "-m",
        message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def archive(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    return layout


@pytest.fixture
def mined(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repository whose history exercises every commit eligibility gate."""
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "retro@example.invalid")
    git(root, "config", "user.name", "Retro Tests")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    source = root / "src"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    shas = {"initial": _commit(root, "initial project", when="2026-08-01T09:00:00+00:00")}

    (source / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n"
        "    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n",
        encoding="utf-8",
    )
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_divide.py").write_text(
        "from src.calc import divide\n\n\ndef test_divide():\n    assert divide(4, 2) == 2\n",
        encoding="utf-8",
    )
    shas["feature"] = _commit(root, "fix: guard divide against zero", when="2026-08-01T12:00:00+00:00")

    (root / "README.md").write_text("# demo project\n\nDocs only.\n", encoding="utf-8")
    shas["docs"] = _commit(root, "docs: expand the readme", when="2026-08-01T13:00:00+00:00")

    (source / "wide.py").write_text(
        "\n".join(f"VALUE_{index} = {index}" for index in range(80)) + "\n", encoding="utf-8"
    )
    shas["large"] = _commit(root, "feat: generate wide constants", when="2026-08-01T14:00:00+00:00")

    (source / "flaky.py").write_text("FLAG = True\n", encoding="utf-8")
    shas["reverted"] = _commit(root, "feat: add a flag", when="2026-08-01T15:00:00+00:00")
    git(
        root,
        "revert",
        "--no-edit",
        shas["reverted"],
        env={
            "GIT_AUTHOR_DATE": "2026-08-01T16:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-01T16:00:00+00:00",
        },
    )
    shas["revert"] = git(root, "rev-parse", "HEAD")

    git(root, "checkout", "-b", "side")
    (source / "side.py").write_text("SIDE = 1\n", encoding="utf-8")
    shas["side"] = _commit(root, "feat: side work", when="2026-08-01T17:00:00+00:00")
    git(root, "checkout", "main")
    git(
        root,
        "merge",
        "--no-ff",
        "-m",
        "Merge branch 'side'",
        "side",
        env={
            "GIT_AUTHOR_DATE": "2026-08-01T18:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-01T18:00:00+00:00",
        },
    )
    shas["merge"] = git(root, "rev-parse", "HEAD")
    return root, shas


def _catalog(layout: Layout, records: list[dict]) -> None:
    path = layout.root / "catalog" / "sessions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _install_linked_session(layout: Layout, root: Path, *, session_id: str = "link-1") -> None:
    install_session(
        layout,
        session_id=session_id,
        events=[
            event(
                1,
                actor="user",
                payload={"text": "Make divide reject a zero denominator."},
                session_id=session_id,
            ),
            event(
                2,
                actor="assistant",
                event_type="file_edit",
                payload={"file_path": str(root / "src" / "calc.py"), "text": "guard"},
                session_id=session_id,
            ),
        ],
        cwd=str(root),
    )
    _catalog(
        layout,
        [
            {
                "schema": 2,
                "key": f"codex/{session_id}",
                "host": "codex",
                "session_id": session_id,
                "started_at": "2026-08-01T11:00:00+00:00",
                "ended_at": "2026-08-01T11:50:00+00:00",
                "cwd": str(root),
                "cwd_resolved": str(root),
                "git": {"root": str(root), "branch": "main"},
                "first_prompt": "Make divide reject a zero denominator.",
                "excluded": False,
            }
        ],
    )


def test_classify_path_separates_tests_docs_locks_and_vendored():
    assert commit_sources.classify_path("src/calc.py") == "source"
    assert commit_sources.classify_path("tests/test_calc.py") == "test"
    assert commit_sources.classify_path("src/calc_test.go") == "test"
    assert commit_sources.classify_path("docs/guide.md") == "docs"
    assert commit_sources.classify_path("README.md") == "docs"
    assert commit_sources.classify_path("poetry.lock") == "lockfile"
    assert commit_sources.classify_path("vendor/pkg/mod.go") == "vendored"
    assert commit_sources.classify_path("node_modules/left-pad/index.js") == "vendored"


def test_enumeration_filters_docs_merges_reverts_and_large_commits(archive: Layout, mined):
    root, shas = mined
    limits = commit_sources.CommitLimits(max_files=10, max_lines=40, link_window_hours=6.0)
    selected, rejections = commit_sources.select_commit_sources(
        layout=archive,
        repos=[root],
        branch="main",
        limits=limits,
        since=SINCE,
    )
    by_code = {rejection.session_id: rejection.code for rejection in rejections}
    assert [candidate.session_id for candidate in selected] == [shas["side"], shas["feature"]]
    assert by_code[shas["docs"]] == "COMMIT_NO_SOURCE_CHANGE"
    assert by_code[shas["large"]] == "COMMIT_TOO_LARGE"
    assert by_code[shas["reverted"]] == "OUTCOME_NOT_DURABLE"
    assert by_code[shas["merge"]] == "COMMIT_MERGE_COMMIT"
    assert by_code[shas["revert"]] == "COMMIT_REVERT_COMMIT"

    candidate = selected[1]
    assert candidate.kind == "commit"
    assert candidate.base_sha == shas["initial"]
    assert candidate.outcome_sha == shas["feature"]
    assert candidate.base.resolution == "commit_parent"
    assert candidate.base.state_confidence == "exact_clean_commit"
    assert candidate.outcome.resolution == "mined_commit"
    assert candidate.source_id.startswith("commit__project__")
    assert candidate.provenance is not None
    assert candidate.provenance["commit"]["touches_tests"] is True
    assert candidate.provenance["commit"]["test_files"] == ["tests/test_divide.py"]
    assert candidate.provenance["work_kind"] == "debug"


def test_merge_commits_are_mined_when_explicitly_enabled(archive: Layout, mined):
    root, shas = mined
    limits = commit_sources.CommitLimits(include_merges=True, max_files=10, max_lines=40)
    candidate, rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["merge"], branch="main", limits=limits
    )
    assert rejection is None and candidate is not None
    assert candidate.base_sha == shas["revert"]
    assert candidate.outcome_sha == shas["merge"]


def test_prompt_provenance_falls_back_to_the_commit_message(archive: Layout, mined):
    root, shas = mined
    candidate, rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["feature"], branch="main"
    )
    assert rejection is None and candidate is not None
    provenance = candidate.provenance or {}
    assert provenance["prompt_provenance"]["mode"] == "commit_message"
    assert "guard divide" in provenance["prompt_provenance"]["prompt"]
    assert provenance["linkage"] is None
    assert candidate.normalized_path is None
    assert candidate.events == ()


def test_overlapping_session_supplies_the_real_first_prompt(archive: Layout, mined):
    root, shas = mined
    _install_linked_session(archive, root)
    candidate, rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["feature"], branch="main"
    )
    assert rejection is None and candidate is not None
    provenance = candidate.provenance or {}
    assert provenance["prompt_provenance"]["mode"] == "linked_session"
    assert provenance["prompt_provenance"]["prompt"] == "Make divide reject a zero denominator."
    assert provenance["prompt_provenance"]["session_key"] == "codex/link-1"
    assert provenance["linkage"]["matched_files"] == ["src/calc.py"]
    assert provenance["linkage"]["gap_minutes"] == 10.0
    assert candidate.normalized_path == archive.normalized_path("codex", "link-1")
    assert candidate.outcome_labels is not None
    assert candidate.outcome_labels["evidence"]["linked_session"] == "codex/link-1"


def test_sessions_outside_the_link_window_are_not_linked(archive: Layout, mined):
    root, shas = mined
    _install_linked_session(archive, root)
    limits = commit_sources.CommitLimits(link_window_hours=0.05)
    candidate, _rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["feature"], branch="main", limits=limits
    )
    assert candidate is not None
    assert (candidate.provenance or {})["prompt_provenance"]["mode"] == "commit_message"


def test_catalog_repositories_are_discovered(archive: Layout, mined):
    root, _shas = mined
    _install_linked_session(archive, root)
    from retro.catalog import load_catalog

    assert commit_sources.discover_repos(load_catalog(archive)) == [root.resolve()]


def test_mining_never_mutates_the_repository(archive: Layout, mined):
    root, _shas = mined
    before = {
        path.relative_to(root).as_posix(): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    head_before = git(root, "rev-parse", "HEAD")
    commit_sources.select_commit_sources(layout=archive, repos=[root], branch="main", since=SINCE)
    after = {
        path.relative_to(root).as_posix(): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert before == after
    assert git(root, "rev-parse", "HEAD") == head_before
    assert git(root, "status", "--porcelain") == ""


def test_commit_source_bundles_match_the_bundle_contract(archive: Layout, mined):
    root, shas = mined
    environment = ProjectEnvironment.from_dict(project_environment(shas["initial"]))
    candidate, rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["feature"], branch="main"
    )
    assert rejection is None and candidate is not None
    import dataclasses

    candidate = dataclasses.replace(candidate, environment=environment)
    built = bundle_mod.build_source_bundle(candidate, layout=archive, name="mined")
    for relative in bundle_mod.BUNDLE_LAYOUT + bundle_mod.COMMIT_BUNDLE_EXTRAS:
        assert (built.path / relative).exists(), relative
    assert bundle_mod.verify_bundle(built.path)

    manifest = SourceBundleManifest.from_dict(
        json.loads((built.path / "manifest.json").read_text(encoding="utf-8"))
    )
    assert manifest.source_kind == "commit"
    assert manifest.host == "git"
    assert manifest.repo.base_resolution == "commit_parent"
    assert manifest.repo.outcome_resolution == "mined_commit"
    assert manifest.repo.state_confidence == "exact_clean_commit"
    assert manifest.provenance is not None
    assert manifest.outcome_labels is not None
    assert (built.path / "rollout" / "events.jsonl").read_text(encoding="utf-8") == ""

    provenance = json.loads(
        (built.path / "context" / "source-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["schema_version"] == commit_sources.COMMIT_PROVENANCE_SCHEMA
    assert provenance["touches_tests"] is True
    patch = (built.path / "repo" / "change.patch").read_text(encoding="utf-8")
    assert "def divide" in patch


def test_linked_commit_bundles_carry_the_session_rollout(archive: Layout, mined):
    root, shas = mined
    _install_linked_session(archive, root)
    candidate, _rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=shas["feature"], branch="main"
    )
    assert candidate is not None
    built = bundle_mod.build_source_bundle(candidate, layout=archive, name="mined")
    events = (built.path / "rollout" / "events.jsonl").read_text(encoding="utf-8")
    assert "Make divide reject a zero denominator." in events
    assert bundle_mod.verify_bundle(built.path)


def test_commit_selection_round_trips_through_select_and_bundle(archive: Layout, mined):
    root, shas = mined
    limits = commit_sources.CommitLimits(max_files=10, max_lines=40)
    result = selection.select_taskset(
        layout=archive,
        name="mined",
        sources="commits",
        require_environment=False,
        branch="main",
        commit_repos=[root],
        commit_limits=limits,
    )
    payload = json.loads((result.path or Path()).read_text(encoding="utf-8"))
    assert payload["counts"]["by_kind"] == {"commit": 2}
    assert payload["sampling"]["sources"] == "commits"
    assert payload["sampling"]["commits"]["limits"]["max_files"] == 10
    entries = {entry["provenance"]["commit"]["sha"]: entry for entry in payload["selected"]}
    entry = entries[shas["feature"]]
    assert entry["source_kind"] == "commit"
    assert entry["normalized_path"] is None
    assert entry["outcome_labels"]["label"] == "successful_surviving"

    report = bundle_mod.bundle_taskset(
        layout=archive, name="mined", selected_only=True, require_environment=False, branch="main"
    )
    assert [outcome.status for outcome in report.outcomes] == ["bundled", "bundled"]
    bundled = bundle_mod.load_bundle(report.outcomes[0].path or Path())
    assert bundled.manifest.source_kind == "commit"
    assert bundle_mod.verify_bundle(bundled.path)


def test_dotfile_edits_link_without_losing_the_leading_dot(archive: Layout, mined):
    root, _shas = mined
    workflow = root / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("name: ci\non: push\n", encoding="utf-8")
    (root / "src" / "ci_helper.py").write_text("STEPS = 1\n", encoding="utf-8")
    sha = _commit(root, "feat: add ci helper", when="2026-08-01T19:00:00+00:00")
    install_session(
        archive,
        session_id="ci-1",
        events=[
            event(1, actor="user", payload={"text": "Add the CI helper."}, session_id="ci-1"),
            event(
                2,
                actor="assistant",
                event_type="file_edit",
                payload={"file_path": ".github/workflows/ci.yml", "text": "ci"},
                session_id="ci-1",
            ),
        ],
        cwd=str(root),
    )
    _catalog(
        archive,
        [
            {
                "schema": 2,
                "key": "codex/ci-1",
                "host": "codex",
                "session_id": "ci-1",
                "started_at": "2026-08-01T18:30:00+00:00",
                "ended_at": "2026-08-01T18:55:00+00:00",
                "cwd": str(root),
                "cwd_resolved": str(root),
                "git": {"root": str(root)},
                "excluded": False,
            }
        ],
    )
    candidate, rejection = commit_sources.select_commit_source(
        layout=archive, root=root, sha=sha, branch="main"
    )
    assert rejection is None and candidate is not None
    linkage = (candidate.provenance or {})["linkage"]
    assert linkage["matched_files"] == [".github/workflows/ci.yml"]


def test_recorded_commit_limits_round_trip_into_bundling(archive: Layout, mined):
    root, shas = mined
    limits = commit_sources.CommitLimits(include_merges=True, max_files=10, max_lines=40)
    selection.select_taskset(
        layout=archive,
        name="merged",
        sources="commits",
        require_environment=False,
        branch="main",
        commit_repos=[root],
        commit_limits=limits,
    )
    report = bundle_mod.bundle_taskset(
        layout=archive, name="merged", selected_only=True, require_environment=False, branch="main"
    )
    statuses = {outcome.session_id: outcome.status for outcome in report.outcomes}
    assert statuses[shas["merge"]] == "bundled"
    assert set(statuses.values()) == {"bundled"}


def test_cli_select_reports_yield_by_kind(archive: Layout, mined):
    from typer.testing import CliRunner

    from retro.cli import app

    root, _shas = mined
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "taskset",
            "select",
            "--name",
            "cli-mined",
            "--sources",
            "commits",
            "--commit-repo",
            str(root),
            "--commit-max-files",
            "10",
            "--commit-max-lines",
            "40",
            "--branch",
            "main",
            "--allow-unvalidated-environment",
            "--root",
            str(archive.root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "by kind: commit=2" in result.output
    assert "COMMIT_TOO_LARGE" in result.output
    payload = json.loads(
        (archive.benchmark_taskset_dir("cli-mined") / "selection.json").read_text(encoding="utf-8")
    )
    assert payload["counts"]["by_kind"] == {"commit": 2}


def test_default_sources_remain_session_only(archive: Layout, mined):
    root, _shas = mined
    _install_linked_session(archive, root)
    result = selection.select_taskset(
        layout=archive,
        name="sessions-only",
        require_environment=False,
        branch="main",
    )
    payload = result.result.to_dict()
    assert payload["counts"]["by_kind"] == {}
    assert payload["sampling"]["sources"] == "sessions"
    assert "commits" not in payload["sampling"]
    assert all(
        rejection["host"] != commit_sources.COMMIT_HOST for rejection in payload["rejections"]
    )
