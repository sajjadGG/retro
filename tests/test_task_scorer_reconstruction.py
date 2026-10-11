"""Anchored reconstruction selection and replay verification."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle, reconstruction, selection
from retro.storage import Layout
from tests.task_scorer_helpers import event, git, install_session, make_repo, repo_state_json


@pytest.fixture
def archive(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    return layout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    return root, make_repo(root)


def _contents(root: Path, shas: dict[str, str]) -> tuple[str, str, str]:
    base_calc = git(root, "show", f"{shas['base_sha']}:src/calc.py") + "\n"
    outcome_calc = git(root, "show", f"{shas['outcome_sha']}:src/calc.py") + "\n"
    outcome_test = git(root, "show", f"{shas['outcome_sha']}:tests/test_divide.py") + "\n"
    return base_calc, outcome_calc, outcome_test


def _events(
    root: Path,
    shas: dict[str, str],
    *,
    old_calc: str | None = None,
    new_calc: str | None = None,
    shell_only: bool = False,
) -> list:
    base_calc, outcome_calc, outcome_test = _contents(root, shas)
    events = [
        event(
            1,
            actor="system",
            event_type="session_start",
            payload={"cwd": str(root)},
        ),
        event(2, actor="user", payload={"text": "Add guarded division and its regression test."}),
    ]
    if shell_only:
        events.append(
            event(
                3,
                actor="assistant",
                event_type="command",
                payload={
                    "command": "cat > src/calc.py <<'EOF'\nchanged by shell\nEOF",
                    "exit_code": 0,
                },
            )
        )
    else:
        events.extend(
            [
                event(
                    3,
                    actor="assistant",
                    event_type="file_edit",
                    payload={
                        "name": "Edit",
                        "part": {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": str(root / "src" / "calc.py"),
                                "old_string": old_calc if old_calc is not None else base_calc,
                                "new_string": new_calc if new_calc is not None else outcome_calc,
                            },
                        },
                    },
                ),
                event(
                    4,
                    actor="assistant",
                    event_type="file_edit",
                    payload={
                        "name": "Write",
                        "part": {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": str(root / "tests" / "test_divide.py"),
                                "content": outcome_test,
                            },
                        },
                    },
                ),
            ]
        )
    sequence = len(events) + 1
    events.extend(
        [
            event(
                sequence,
                actor="tool",
                event_type="command",
                payload={
                    "command": "git rev-parse HEAD",
                    "output": shas["base_sha"],
                    "exit_code": 0,
                },
            ),
            event(sequence + 1, actor="user", payload={"text": "Looks good, thanks."}),
        ]
    )
    return events


def _install(
    archive: Layout,
    root: Path,
    shas: dict[str, str],
    events: list,
) -> None:
    install_session(
        archive,
        events=events,
        cwd=str(root),
        raw_files={
            "repo_end.json": repo_state_json(
                root,
                shas["outcome_sha"],
                shas["outcome_tree"],
                captured_at="2026-08-01T19:00:00Z",
            )
        },
    )


def _select(
    archive: Layout,
    root: Path,
    *,
    allow_reconstruction: bool,
) -> tuple[selection.SourceCandidate | None, selection.SourceRejection | None]:
    return selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
        allow_reconstruction=allow_reconstruction,
        repo_root=root,
    )


def test_selects_fully_verified_anchored_reconstruction(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas, _events(root, shas))

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert rejection is None
    assert candidate is not None
    assert candidate.base.resolution == "anchored_reconstruction"
    assert candidate.base.state_confidence == "approximate"
    report = candidate.base.evidence["reconstruction"]
    assert report["anchor_method"] == "rollout_head"
    assert report["confidence"] == 1.0
    assert report["files_touched"] == ["src/calc.py", "tests/test_divide.py"]
    assert report["files_verified_pre"] == report["files_touched"]
    assert report["files_verified_post"] == report["files_touched"]
    assert report["unverifiable_edits"] == []

    selection.select_taskset(
        layout=archive,
        name="reconstructed",
        sessions=[("codex", "session-1")],
        branch="main",
        require_environment=False,
        repo_root=root,
        allow_reconstruction=True,
    )
    bundled = bundle.bundle_taskset(layout=archive, name="reconstructed")
    assert len(bundled.bundled) == 1
    source_path = bundled.bundled[0].path
    assert source_path is not None
    assert bundle.verify_bundle(source_path)
    manifest = json.loads((source_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["base_resolution"] == "anchored_reconstruction"
    assert manifest["repo"]["reconstruction"] == report


def test_reconstruction_rejects_pre_edit_mismatch(archive: Layout, repo):
    root, shas = repo
    _install(
        archive,
        root,
        shas,
        _events(root, shas, old_calc="content that was never in the anchor\n"),
    )

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "RECONSTRUCTION_PRE_MISMATCH"
    assert rejection.evidence["reconstruction"]["pre_mismatches"]
    assert rejection.evidence["reconstruction"]["confidence"] < 0.8


def test_reconstruction_rejects_post_edit_mismatch(archive: Layout, repo):
    root, shas = repo
    base_calc, _outcome_calc, _outcome_test = _contents(root, shas)
    _install(
        archive,
        root,
        shas,
        _events(root, shas, old_calc=base_calc, new_calc=base_calc + "\n# wrong outcome\n"),
    )

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "RECONSTRUCTION_POST_MISMATCH"
    assert rejection.evidence["reconstruction"]["post_mismatches"] == ["src/calc.py"]
    assert rejection.evidence["reconstruction"]["confidence"] < 0.8


def test_reconstruction_rejects_shell_only_edit(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas, _events(root, shas, shell_only=True))

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "RECONSTRUCTION_UNVERIFIABLE_EDIT"
    assert rejection.evidence["reconstruction"]["unverifiable_edits"]


def test_reconstruction_flag_off_keeps_exact_proof_gate(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas, _events(root, shas))

    candidate, rejection = _select(archive, root, allow_reconstruction=False)

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "NO_EXACT_BASE_SHA"


def test_anchor_prefers_first_rollout_commit_parent(repo):
    root, shas = repo
    events = [
        event(1, actor="user", payload={"text": "implement division"}),
        event(
            2,
            actor="tool",
            event_type="command",
            payload={"command": "git rev-parse HEAD", "output": shas["base_sha"]},
        ),
        event(
            3,
            actor="tool",
            event_type="command",
            payload={
                "command": "git commit -am 'division'",
                "output": f"[main {shas['outcome_sha'][:7]}] division",
            },
        ),
    ]

    anchor = reconstruction.resolve_anchor(
        root=root,
        events=events,
        session_started_at="2026-08-01T18:00:00Z",
        recorded_branch="main",
    )

    assert anchor.base_sha == shas["base_sha"]
    assert anchor.evidence["anchor_method"] == "first_commit_parent"


def test_reconstruction_replays_codex_apply_patch(archive: Layout, repo):
    root, shas = repo
    base_calc, outcome_calc, outcome_test = _contents(root, shas)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/calc.py",
            "@@",
            *(f"-{line}" for line in base_calc.splitlines()),
            *(f"+{line}" for line in outcome_calc.splitlines()),
            "*** Add File: tests/test_divide.py",
            *(f"+{line}" for line in outcome_test.splitlines()),
            "*** End Patch",
        ]
    )
    events = [
        event(
            1,
            actor="system",
            event_type="session_start",
            payload={"cwd": str(root)},
        ),
        event(2, actor="user", payload={"text": "Add guarded division."}),
        event(
            3,
            actor="assistant",
            event_type="file_edit",
            payload={"name": "apply_patch", "input": patch, "call_id": "patch-1"},
        ),
        event(
            4,
            actor="tool",
            event_type="command",
            payload={"command": "git rev-parse HEAD", "output": shas["base_sha"]},
        ),
        event(5, actor="user", payload={"text": "Looks good."}),
    ]
    _install(archive, root, shas, events)

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert rejection is None
    assert candidate is not None
    assert candidate.base.evidence["reconstruction"]["confidence"] == 1.0


def test_anchor_falls_back_to_recorded_branch_before_session(tmp_path: Path):
    root = tmp_path / "dated-project"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    (root / "value.txt").write_text("before\n", encoding="utf-8")
    git(root, "add", "value.txt")
    git(
        root,
        "commit",
        "-m",
        "before session",
        env={
            "GIT_AUTHOR_DATE": "2026-08-01T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-01T10:00:00+00:00",
        },
    )
    before = git(root, "rev-parse", "HEAD")
    (root / "value.txt").write_text("after\n", encoding="utf-8")
    git(root, "add", "value.txt")
    git(
        root,
        "commit",
        "-m",
        "after session",
        env={
            "GIT_AUTHOR_DATE": "2026-08-01T20:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-01T20:00:00+00:00",
        },
    )

    anchor = reconstruction.resolve_anchor(
        root=root,
        events=[],
        session_started_at="2026-08-01T18:00:00Z",
        recorded_branch="main",
    )

    assert anchor.base_sha == before
    assert anchor.evidence["anchor_method"] == "branch_before_session"


def test_exact_start_proof_keeps_priority_when_reconstruction_is_enabled(
    archive: Layout,
    repo,
):
    root, shas = repo
    install_session(
        archive,
        events=_events(root, shas),
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(
                root,
                shas["base_sha"],
                shas["base_tree"],
                captured_at="2026-08-01T18:00:00Z",
            ),
            "repo_end.json": repo_state_json(
                root,
                shas["outcome_sha"],
                shas["outcome_tree"],
                captured_at="2026-08-01T19:00:00Z",
            ),
        },
    )

    candidate, rejection = _select(archive, root, allow_reconstruction=True)

    assert rejection is None
    assert candidate is not None
    assert candidate.base.resolution == "captured_start"
    assert "reconstruction" not in candidate.base.evidence
