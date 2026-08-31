"""Git provenance tests: cwd resolution, clean-state proof, base/outcome anchoring."""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import git_state
from retro.storage import Layout
from tests.task_scorer_helpers import (
    event,
    git,
    install_session,
    make_repo,
    repo_state_json,
    rollout_events,
)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    return root, make_repo(root)


def test_resolves_codex_cwd_from_thread_json(tmp_path: Path, repo):
    root, _ = repo
    layout = Layout(tmp_path / "archive")
    raw_dir = install_session(layout, events=[], cwd=str(root))
    resolution = git_state.resolve_session_cwd(raw_dir=raw_dir)
    assert resolution.source == "thread.json"
    assert resolution.cwd == str(root)
    assert resolution.exists
    assert resolution.root is not None
    assert resolution.root.resolve() == root.resolve()


def test_resolves_claude_cwd_from_raw_events(tmp_path: Path, repo):
    root, _ = repo
    layout = Layout(tmp_path / "archive")
    transcript = "\n".join(
        [
            json.dumps({"type": "summary", "summary": "no cwd here"}),
            json.dumps({"type": "user", "cwd": str(root), "message": {"content": "hi"}}),
        ]
    )
    raw_dir = install_session(
        layout,
        host="claude-code",
        session_id="claude-1",
        events=[],
        raw_files={"transcript.jsonl": transcript},
    )
    resolution = git_state.resolve_session_cwd(raw_dir=raw_dir)
    assert resolution.source == "raw:transcript.jsonl"
    assert resolution.cwd == str(root)
    assert resolution.root is not None


def test_cwd_falls_back_to_normalized_events_and_reports_missing_path(tmp_path: Path):
    events = [event(1, actor="system", event_type="session_start", payload={"cwd": "/nope/gone"})]
    resolution = git_state.resolve_session_cwd(raw_dir=tmp_path / "absent", events=events)
    assert resolution.source == "normalized_event"
    assert resolution.exists is False
    assert resolution.root is None


def test_capture_repo_state_records_clean_and_dirty(repo):
    root, shas = repo
    clean = git_state.capture_repo_state(root, captured_at="2026-08-01T18:00:00Z")
    assert clean.clean is True
    assert clean.head_sha == shas["outcome_sha"]
    assert clean.head_tree == shas["outcome_tree"]

    (root / "src" / "calc.py").write_text("dirty\n", encoding="utf-8")
    dirty = git_state.capture_repo_state(root)
    assert dirty.clean is False
    assert dirty.dirty_entries


@pytest.mark.parametrize(
    "porcelain",
    [
        " M src/calc.py\n",
        "?? untracked.txt\0",
        "1 .M N... 100644 100644 100644 " + "a" * 40 + " " + "b" * 40 + " src/calc.py\0",
        "2 R. N... 100644 100644 100644 "
        + "a" * 40
        + " "
        + "b" * 40
        + " R100 renamed.py\0old.py\0",
        "u UU N... 100644 100644 100644 100644 "
        + "a" * 40
        + " "
        + "b" * 40
        + " "
        + "c" * 40
        + " conflict.py\0",
        "? untracked.txt\0",
        "! ignored.txt\0",
    ],
)
def test_porcelain_v1_and_v2_dirty_records_are_never_clean(porcelain):
    state = git_state.RepoStateCapture(
        root="/repo",
        head_sha="a" * 40,
        head_tree="b" * 40,
        porcelain=porcelain,
        submodules="",
    )

    assert state.clean is False
    assert state.dirty_entries
    assert git_state._short_status_is_clean(porcelain) is False


def test_porcelain_branch_headers_remain_valid_clean_evidence():
    for porcelain in (
        "## main...origin/main\n",
        "# branch.oid " + "a" * 40 + "\0# branch.head main\0# branch.ab +0 -0\0",
    ):
        state = git_state.RepoStateCapture(
            root="/repo",
            head_sha="a" * 40,
            head_tree="b" * 40,
            porcelain=porcelain,
            submodules="",
        )
        assert state.clean is True
        assert git_state._short_status_is_clean(porcelain) is True


def test_unknown_status_output_cannot_prove_a_clean_worktree():
    assert git_state._short_status_is_clean("command completed successfully") is False


def test_repo_state_capture_round_trip(tmp_path: Path, repo):
    root, _ = repo
    state = git_state.capture_repo_state(root)
    path = tmp_path / "repo_start.json"
    git_state.write_repo_state(path, state)
    restored = git_state.load_repo_state(path)
    assert restored == state


def test_base_from_captured_clean_start(tmp_path: Path, repo):
    root, shas = repo
    layout = Layout(tmp_path / "archive")
    raw_dir = install_session(
        layout,
        events=[],
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])
        },
    )
    resolution = git_state.resolve_base(
        root=root, events=[], captured_start=git_state.load_captured_start(raw_dir)
    )
    assert resolution.resolved
    assert resolution.base_sha == shas["base_sha"]
    assert resolution.base_tree == shas["base_tree"]
    assert resolution.resolution == "captured_start"
    assert resolution.state_confidence == "exact_clean_commit"


def test_captured_start_must_belong_to_resolved_repository(repo, tmp_path: Path):
    root, _shas = repo
    capture = replace(git_state.capture_repo_state(root), root=str(tmp_path / "other"))

    resolution = git_state.resolve_base(root=root, events=[], captured_start=capture)

    assert resolution.rejection_code == "NO_EXACT_BASE_SHA"
    assert "different repository" in resolution.detail


def test_base_rejects_dirty_start(tmp_path: Path, repo):
    root, shas = repo
    layout = Layout(tmp_path / "archive")
    raw_dir = install_session(
        layout,
        events=[],
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(
                root,
                shas["base_sha"],
                shas["base_tree"],
                porcelain="1 .M N... 100644 100644 100644 aaa bbb src/calc.py\x00",
            )
        },
    )
    resolution = git_state.resolve_base(
        root=root, events=[], captured_start=git_state.load_captured_start(raw_dir)
    )
    assert not resolution.resolved
    assert resolution.rejection_code == "DIRTY_START_STATE"


def test_base_rejects_tree_mismatch(tmp_path: Path, repo):
    root, shas = repo
    layout = Layout(tmp_path / "archive")
    raw_dir = install_session(
        layout,
        events=[],
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["outcome_tree"])
        },
    )
    resolution = git_state.resolve_base(
        root=root, events=[], captured_start=git_state.load_captured_start(raw_dir)
    )
    assert resolution.rejection_code == "DIRTY_START_STATE"
    assert "tree" in resolution.detail


def test_base_from_rollout_head_command(repo):
    root, shas = repo
    events = rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7])
    resolution = git_state.resolve_base(root=root, events=events)
    assert resolution.resolution == "rollout_command"
    assert resolution.base_sha == shas["base_sha"]
    assert resolution.state_confidence == "exact_clean_commit"
    assert resolution.evidence["event_id"].endswith(":3")


def test_base_from_separate_tool_result_event(repo):
    root, shas = repo
    events = [
        event(1, actor="user", payload={"text": "please implement"}),
        event(
            2,
            actor="assistant",
            event_type="tool_call",
            payload={"command": "git rev-parse HEAD", "call_id": "call-1"},
        ),
        event(
            3,
            actor="tool",
            event_type="tool_result",
            payload={"call_id": "call-1", "output": shas["base_sha"] + "\n"},
        ),
        event(
            4,
            actor="assistant",
            event_type="tool_call",
            payload={
                "command": "git status --porcelain=v2 --untracked-files=all",
                "call_id": "call-2",
            },
        ),
        event(
            5,
            actor="tool",
            event_type="tool_result",
            payload={"call_id": "call-2", "output": "", "exit_code": 0},
        ),
    ]
    resolution = git_state.resolve_base(root=root, events=events)
    assert resolution.base_sha == shas["base_sha"]
    assert resolution.evidence["event_id"] == "session-1:2"


def test_rollout_head_without_clean_start_proof_is_rejected(repo):
    root, shas = repo
    events = rollout_events(
        base_sha=shas["base_sha"],
        commit_short=shas["outcome_sha"][:7],
        include_clean_status=False,
    )

    resolution = git_state.resolve_base(root=root, events=events)

    assert resolution.rejection_code == "DIRTY_START_STATE"
    assert "did not prove" in resolution.detail


def test_historical_short_status_branch_header_proves_clean_start(repo):
    root, shas = repo
    events = [
        event(
            1,
            actor="assistant",
            event_type="command",
            payload={"command": "git status --short --branch", "call_id": "status"},
        ),
        event(
            2,
            actor="tool",
            event_type="tool_result",
            payload={
                "call_id": "status",
                "output": (
                    "Process exited with code 0\nOriginal token count: 5\n"
                    "Output:\n## HEAD (no branch)\n"
                ),
            },
        ),
        event(
            3,
            actor="assistant",
            event_type="command",
            payload={"command": "git rev-parse HEAD", "call_id": "head"},
        ),
        event(
            4,
            actor="tool",
            event_type="tool_result",
            payload={
                "call_id": "head",
                "output": f"Process exited with code 0\nOutput:\n{shas['base_sha']}\n",
            },
        ),
        event(5, actor="assistant", event_type="file_edit", payload={"file_path": "src/calc.py"}),
    ]

    resolution = git_state.resolve_base(root=root, events=events)

    assert resolution.resolution == "rollout_command"
    assert resolution.base_sha == shas["base_sha"]
    assert resolution.evidence["clean_status_event_id"] == "session-1:1"


def test_historical_short_status_rejects_dirty_or_failed_output(repo):
    root, shas = repo
    for output in (
        "Process exited with code 0\nOutput:\n## main\n M src/calc.py\n",
        "Process exited with code 128\nOutput:\nfatal: clean filter failed\n",
    ):
        events = [
            event(
                1,
                actor="assistant",
                event_type="command",
                payload={"command": "git status --short --branch", "call_id": "status"},
            ),
            event(
                2,
                actor="tool",
                event_type="tool_result",
                payload={"call_id": "status", "output": output},
            ),
            event(
                3,
                actor="tool",
                event_type="command",
                payload={
                    "command": "git rev-parse HEAD",
                    "output": shas["base_sha"],
                    "exit_code": 0,
                },
            ),
            event(4, actor="assistant", event_type="file_edit", payload={"file_path": "src/calc.py"}),
        ]

        resolution = git_state.resolve_base(root=root, events=events)

        assert resolution.rejection_code == "DIRTY_START_STATE"


def test_base_ignores_head_probe_after_first_mutation(repo):
    root, shas = repo
    events = [
        event(1, actor="assistant", event_type="file_edit", payload={"file_path": "src/calc.py"}),
        event(
            2,
            actor="tool",
            event_type="command",
            payload={"command": "git rev-parse HEAD", "output": shas["outcome_sha"]},
        ),
    ]
    resolution = git_state.resolve_base(root=root, events=events)
    assert not resolution.resolved
    assert resolution.rejection_code == "NO_EXACT_BASE_SHA"


def test_base_from_first_commit_parent_is_approximate(repo):
    root, shas = repo
    events = [
        event(1, actor="user", payload={"text": "guard divide"}),
        event(
            2,
            actor="tool",
            event_type="command",
            payload={
                "command": "git status --porcelain=v2 --untracked-files=all",
                "output": "",
                "exit_code": 0,
            },
        ),
        event(3, actor="assistant", event_type="file_edit", payload={"file_path": "src/calc.py"}),
        event(
            4,
            actor="tool",
            event_type="command",
            payload={
                "command": "git commit -am 'add divide'",
                "output": f"[main {shas['outcome_sha'][:7]}] add divide",
            },
        ),
    ]
    resolution = git_state.resolve_base(root=root, events=events)
    assert resolution.resolution == "first_commit_parent"
    assert resolution.base_sha == shas["base_sha"]
    assert resolution.state_confidence == "approximate"


def test_first_commit_parent_rejected_without_clean_start_proof(repo):
    root, shas = repo
    events = [
        event(1, actor="assistant", event_type="file_edit", payload={"file_path": "src/calc.py"}),
        event(
            2,
            actor="tool",
            event_type="command",
            payload={
                "command": "git commit -am 'add divide'",
                "output": f"[main {shas['outcome_sha'][:7]}] add divide",
            },
        ),
    ]
    resolution = git_state.resolve_base(root=root, events=events)
    assert resolution.rejection_code == "DIRTY_START_STATE"


def test_base_is_never_inferred_from_timestamps(repo):
    root, _ = repo
    events = [
        event(1, actor="user", payload={"text": "do the work"}, timestamp="2026-08-01T18:00:00Z"),
        event(2, actor="assistant", payload={"text": "done"}, timestamp="2026-08-01T18:30:00Z"),
    ]
    resolution = git_state.resolve_base(root=root, events=events)
    assert not resolution.resolved
    assert resolution.rejection_code == "NO_EXACT_BASE_SHA"
    assert resolution.base_sha is None


def test_outcome_from_durable_rollout_commit(repo):
    root, shas = repo
    events = rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7])
    resolution = git_state.resolve_outcome(
        root=root,
        events=events,
        base_sha=shas["base_sha"],
        branch="main",
        user_accepted=True,
    )
    assert resolution.resolution == "rollout_commit"
    assert resolution.outcome_sha == shas["outcome_sha"]
    assert resolution.outcome_tree == shas["outcome_tree"]


def test_rollout_commit_requires_completion_or_acceptance(repo):
    root, shas = repo
    events = rollout_events(
        base_sha=shas["base_sha"],
        commit_short=shas["outcome_sha"][:7],
    )[:-1]

    resolution = git_state.resolve_outcome(
        root=root,
        events=events,
        base_sha=shas["base_sha"],
        branch="main",
    )

    assert resolution.rejection_code == "NO_OUTCOME_SHA"


def test_outcome_from_linked_pull_request_merge(tmp_path: Path):
    root = tmp_path / "pr-project"
    shas = make_repo(root)
    git(root, "remote", "add", "origin", "https://github.com/demo/demo.git")
    git(root, "checkout", "-b", "feature")
    (root / "src" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "add extra value")
    feature_sha = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "feature", "-m", "Merge pull request #7 from demo/feature")
    merge_sha = git(root, "rev-parse", "HEAD")

    events = rollout_events(
        base_sha=shas["outcome_sha"],
        commit_short=feature_sha[:7],
        pull_request=7,
    )
    resolution = git_state.resolve_outcome(
        root=root, events=events, base_sha=shas["outcome_sha"], branch="main"
    )
    assert resolution.resolution == "linked_pr_merge"
    assert resolution.outcome_sha == merge_sha
    assert resolution.evidence["pull_request"] == 7


def test_external_pull_request_url_cannot_select_same_number_from_local_repo(tmp_path: Path):
    root = tmp_path / "pr-project"
    shas = make_repo(root)
    git(root, "remote", "add", "origin", "git@github.com:demo/demo.git")
    git(root, "checkout", "-b", "feature")
    (root / "src" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "add extra value")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "feature", "-m", "Merge pull request #7 from demo/feature")
    events = [
        event(
            1,
            actor="assistant",
            payload={"text": "Opened https://github.com/other/project/pull/7 for review."},
        )
    ]

    resolution = git_state.resolve_outcome(
        root=root,
        events=events,
        base_sha=shas["outcome_sha"],
        branch="main",
    )

    assert resolution.rejection_code == "NO_OUTCOME_SHA"


def test_capture_timestamps_must_bracket_rollout_events(repo):
    root, shas = repo
    events = [
        event(1, actor="user", timestamp="2026-08-01T18:01:00Z"),
        event(2, actor="user", timestamp="2026-08-01T18:02:00Z"),
    ]
    late_start = git_state.RepoStateCapture(
        root=str(root),
        head_sha=shas["base_sha"],
        head_tree=shas["base_tree"],
        porcelain="",
        submodules="",
        captured_at="2026-08-01T18:01:01Z",
    )
    early_end = git_state.RepoStateCapture(
        root=str(root),
        head_sha=shas["outcome_sha"],
        head_tree=shas["outcome_tree"],
        porcelain="",
        submodules="",
        captured_at="2026-08-01T18:01:59Z",
    )

    base = git_state.resolve_base(
        root=root,
        events=events,
        captured_start=late_start,
    )
    outcome = git_state.resolve_outcome(
        root=root,
        events=events,
        base_sha=shas["base_sha"],
        captured_end=early_end,
        user_accepted=True,
    )

    assert base.rejection_code == "NO_EXACT_BASE_SHA"
    assert "after the rollout started" in base.detail
    assert outcome.rejection_code == "NO_OUTCOME_SHA"
    assert "before the rollout ended" in outcome.detail


def test_outcome_from_accepted_clean_end_capture(repo):
    root, shas = repo
    events = [event(1, actor="user", payload={"text": "looks good, thanks"})]
    end_state = git_state.capture_repo_state(root, captured_at="2026-08-01T18:02:00Z")
    resolution = git_state.resolve_outcome(
        root=root,
        events=events,
        base_sha=shas["base_sha"],
        branch="main",
        captured_end=end_state,
        user_accepted=True,
    )
    assert resolution.resolution == "captured_end"
    assert resolution.outcome_sha == shas["outcome_sha"]


def test_outcome_rejects_dirty_end_state(repo):
    root, shas = repo
    (root / "src" / "calc.py").write_text("uncommitted work\n", encoding="utf-8")
    end_state = git_state.capture_repo_state(root)
    resolution = git_state.resolve_outcome(
        root=root,
        events=[],
        base_sha=shas["base_sha"],
        branch="main",
        captured_end=end_state,
        user_accepted=True,
    )
    assert resolution.rejection_code == "OUTCOME_NOT_DURABLE"


def test_outcome_rejects_captured_tree_mismatch(repo):
    root, shas = repo
    end_state = replace(git_state.capture_repo_state(root), head_tree=shas["base_tree"])

    resolution = git_state.resolve_outcome(
        root=root,
        events=[],
        base_sha=shas["base_sha"],
        branch="main",
        captured_end=end_state,
        user_accepted=True,
    )

    assert resolution.rejection_code == "OUTCOME_NOT_DURABLE"
    assert "captured end tree" in resolution.detail


def test_outcome_unresolved_without_evidence(repo):
    root, shas = repo
    resolution = git_state.resolve_outcome(
        root=root, events=[], base_sha=shas["base_sha"], branch="main"
    )
    assert resolution.rejection_code == "NO_OUTCOME_SHA"


def test_find_revert_detects_reverting_commit(repo):
    root, shas = repo
    assert git_state.find_revert(root, shas["outcome_sha"], branch="main") is None
    git(root, "revert", "--no-edit", shas["outcome_sha"])
    revert = git_state.find_revert(root, shas["outcome_sha"], branch="main")
    assert revert is not None
    assert revert["sha"] == git(root, "rev-parse", "HEAD")


def test_materialize_tree_has_no_git_directory(tmp_path: Path, repo):
    root, shas = repo
    dest = tmp_path / "checkout"
    git_state.materialize_tree(root, shas["base_sha"], dest)
    assert (dest / "src" / "calc.py").read_text(encoding="utf-8").startswith("def add")
    assert not (dest / ".git").exists()
    assert not (dest / "tests" / "test_divide.py").exists()


def test_materialize_tree_rejects_symlink_outside_destination(tmp_path: Path):
    root = tmp_path / "symlink-project"
    make_repo(root)
    (root / "leak").symlink_to("../outside-secret")
    git(root, "add", "leak")
    git(root, "commit", "-m", "add unsafe symlink")
    sha = git(root, "rev-parse", "HEAD")

    with pytest.raises(git_state.GitError, match="link outside"):
        git_state.materialize_tree(root, sha, tmp_path / "checkout")


def test_change_patch_and_commit_range(repo):
    root, shas = repo
    patch = git_state.change_patch(root, shas["base_sha"], shas["outcome_sha"])
    assert "def divide" in patch
    assert "src/calc.py" in patch
    added = git_state.added_lines(patch)
    assert any("division by zero" in line for line in added)

    entries = git_state.commit_range(root, shas["base_sha"], shas["outcome_sha"])
    assert [entry["sha"] for entry in entries] == [shas["outcome_sha"]]
    assert entries[0]["subject"] == "add divide with zero guard"
    assert "src/calc.py" in entries[0]["files"]


def test_repo_identity_strips_credentials(repo):
    root, _ = repo
    without_remote = git_state.repo_identity(root)
    git(root, "remote", "add", "origin", "https://user:token@github.com/demo/demo.git")
    assert git_state.canonical_remote(root) == "https://github.com/demo/demo"
    assert git_state.repo_identity(root) != without_remote
    assert git_state.repo_identity(root).startswith("sha256:")


def test_repo_root_returns_none_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_state.repo_root(plain) is None


def test_write_session_capture_runs_exactly_the_specified_command_set(tmp_path, monkeypatch):
    root = tmp_path / "project"
    make_repo(root)
    raw_dir = tmp_path / "raw"
    calls: list[list[str]] = []

    real_run = subprocess.run

    def record(args, *rest, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            calls.append([str(item) for item in args])
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(git_state.subprocess, "run", record)
    record_out = git_state.write_session_capture(raw_dir, "start", cwd=root)

    git_args = [call[3:] for call in calls if call[1] == "-C"]
    assert ["rev-parse", "--show-toplevel"] in git_args
    assert ["rev-parse", "HEAD"] in git_args
    assert ["rev-parse", "HEAD^{tree}"] in git_args
    assert ["status", "--porcelain=v2", "-z", "--untracked-files=all"] in git_args
    assert ["submodule", "status", "--recursive"] in git_args
    assert not any("log" in args or "reflog" in args for args in git_args)
    assert record_out.state.clean is True


def test_write_session_capture_publishes_both_phases_immutably(tmp_path):
    root = tmp_path / "project"
    shas = make_repo(root)
    raw_dir = tmp_path / "raw"

    start = git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert start.path == raw_dir / "repo_start.json"
    assert start.state.head_sha == shas["outcome_sha"]
    payload = json.loads(start.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == git_state.CAPTURE_SCHEMA
    assert payload["captured_at"].endswith("Z")
    assert start.to_dict()["clean"] is True

    end = git_state.write_session_capture(raw_dir, "end", cwd=root / "src")
    assert end.path == raw_dir / "repo_end.json"
    assert end.state.root == start.state.root

    before = start.path.read_bytes()
    with pytest.raises(git_state.CaptureExistsError) as error:
        git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert error.value.path == start.path
    assert error.value.state == start.state
    assert start.path.read_bytes() == before
    assert not [item for item in raw_dir.iterdir() if item.name.startswith(".")]


def test_write_session_capture_records_dirty_state_without_judging_it(tmp_path):
    root = tmp_path / "project"
    make_repo(root)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    (root / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    raw_dir = tmp_path / "raw"

    record = git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert record.state.clean is False
    assert any("calc.py" in entry for entry in record.state.dirty_entries)
    assert any("untracked.txt" in entry for entry in record.state.dirty_entries)
    assert git_state.read_capture(raw_dir, "start") == record.state


def test_capture_phase_and_location_are_validated(tmp_path):
    root = tmp_path / "project"
    make_repo(root)
    with pytest.raises(git_state.GitError, match="capture phase"):
        git_state.write_session_capture(tmp_path / "raw", "middle", cwd=root)
    with pytest.raises(git_state.GitError, match="capture phase"):
        git_state.capture_path(tmp_path / "raw", "middle")
    with pytest.raises(git_state.GitError, match="does not exist"):
        git_state.write_session_capture(tmp_path / "raw", "start", cwd=tmp_path / "missing")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(git_state.GitError, match="not inside a Git repository"):
        git_state.write_session_capture(tmp_path / "raw", "start", cwd=outside)
    assert git_state.read_capture(tmp_path / "raw", "start") is None


def test_capture_is_never_partially_visible(tmp_path, monkeypatch):
    root = tmp_path / "project"
    make_repo(root)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    def explode(source, destination):
        raise RuntimeError("link failed after staging")

    monkeypatch.setattr(git_state.os, "link", explode)
    with pytest.raises(RuntimeError):
        git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert list(raw_dir.iterdir()) == []


def test_capture_loses_a_concurrent_race_instead_of_overwriting(tmp_path, monkeypatch):
    root = tmp_path / "project"
    make_repo(root)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    real_capture = git_state.capture_repo_state

    def capture_then_race(*args, **kwargs):
        state = real_capture(*args, **kwargs)
        (raw_dir / "repo_start.json").write_text('{"winner": true}', encoding="utf-8")
        return state

    monkeypatch.setattr(git_state, "capture_repo_state", capture_then_race)
    with pytest.raises(git_state.CaptureExistsError):
        git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert (raw_dir / "repo_start.json").read_text(encoding="utf-8") == '{"winner": true}'
    assert [item.name for item in raw_dir.iterdir()] == ["repo_start.json"]


def test_capture_falls_back_to_o_excl_when_links_are_unsupported(tmp_path, monkeypatch):
    root = tmp_path / "project"
    make_repo(root)
    raw_dir = tmp_path / "raw"

    def unsupported(source, destination):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(git_state.os, "link", unsupported)
    record = git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert git_state.read_capture(raw_dir, "start") == record.state

    with pytest.raises(git_state.CaptureExistsError):
        git_state.write_session_capture(raw_dir, "start", cwd=root)
    assert [item.name for item in raw_dir.iterdir()] == ["repo_start.json"]


def test_o_excl_fallback_also_loses_a_concurrent_race(tmp_path, monkeypatch):
    root = tmp_path / "project"
    make_repo(root)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    real_capture = git_state.capture_repo_state

    def capture_then_race(*args, **kwargs):
        state = real_capture(*args, **kwargs)
        (raw_dir / "repo_end.json").write_text('{"winner": true}', encoding="utf-8")
        return state

    def unsupported(source, destination):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(git_state, "capture_repo_state", capture_then_race)
    monkeypatch.setattr(git_state.os, "link", unsupported)
    with pytest.raises(git_state.CaptureExistsError):
        git_state.write_session_capture(raw_dir, "end", cwd=root)
    assert (raw_dir / "repo_end.json").read_text(encoding="utf-8") == '{"winner": true}'
    assert [item.name for item in raw_dir.iterdir()] == ["repo_end.json"]


def test_capture_filenames_match_importer_sidecar_contract():
    from retro.importers import base as importer_base

    assert set(git_state.CAPTURE_FILENAMES.values()) == set(importer_base._REPO_STATE_FILES)
    assert tuple(git_state.CAPTURE_FILENAMES) == git_state.CAPTURE_PHASES
