"""Shared fixtures for the rollout-to-task-and-scorer pipeline tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Retro Tests",
    "GIT_AUTHOR_EMAIL": "retro@example.invalid",
    "GIT_COMMITTER_NAME": "Retro Tests",
    "GIT_COMMITTER_EMAIL": "retro@example.invalid",
    "GIT_AUTHOR_DATE": "2026-08-01T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-08-01T10:00:00+00:00",
}


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    import os

    merged = dict(os.environ)
    merged.update(GIT_ENV)
    if env:
        merged.update(env)
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
    )
    assert process.returncode == 0, f"git {' '.join(args)}: {process.stderr}"
    return process.stdout.strip()


def make_repo(root: Path) -> dict[str, str]:
    """Base commit with a failing feature case, outcome commit implementing it."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "retro@example.invalid")
    git(root, "config", "user.name", "Retro Tests")
    (root / "README.md").write_text("# demo project\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial project")
    base_sha = git(root, "rev-parse", "HEAD")
    base_tree = git(root, "rev-parse", "HEAD^{tree}")

    (src / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n"
        "    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n",
        encoding="utf-8",
    )
    (tests / "test_divide.py").write_text(
        "import pytest\n\nfrom src.calc import divide\n\n\n"
        "def test_divide_by_zero():\n    with pytest.raises(ValueError):\n        divide(1, 0)\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add divide with zero guard")
    outcome_sha = git(root, "rev-parse", "HEAD")
    outcome_tree = git(root, "rev-parse", "HEAD^{tree}")
    return {
        "base_sha": base_sha,
        "base_tree": base_tree,
        "outcome_sha": outcome_sha,
        "outcome_tree": outcome_tree,
    }


def event(
    sequence: int,
    *,
    actor: str = "user",
    event_type: str = "message",
    payload: dict[str, Any] | None = None,
    summary: str = "",
    session_id: str = "session-1",
    host: str = "codex",
    timestamp: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"{session_id}:{sequence}",
        session_id=session_id,
        host=host,  # type: ignore[arg-type]
        sequence=sequence,
        actor=actor,  # type: ignore[arg-type]
        event_type=event_type,  # type: ignore[arg-type]
        summary=summary,
        raw_ref=RawRef(path=f"raw/{host}/{session_id}/rollout.jsonl", line=sequence),
        timestamp=timestamp or f"2026-08-01T18:{sequence:02d}:00Z",
        payload=payload or {},
    )


def rollout_events(
    *,
    session_id: str = "session-1",
    base_sha: str,
    commit_short: str | None = None,
    include_head_probe: bool = True,
    include_clean_status: bool = True,
    include_commit: bool = True,
    edit_before_commit: bool = True,
    pull_request: int | None = None,
) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = [
        event(
            1,
            actor="system",
            event_type="session_start",
            payload={"cwd": "/does/not/matter"},
            summary="session start",
            session_id=session_id,
        ),
        event(
            2,
            actor="user",
            event_type="message",
            payload={"text": "Guard divide against zero denominators and raise ValueError."},
            session_id=session_id,
        ),
    ]
    sequence = 3
    if include_head_probe:
        events.append(
            event(
                sequence,
                actor="tool",
                event_type="command",
                payload={"command": "git rev-parse HEAD", "output": base_sha, "exit_code": 0},
                session_id=session_id,
            )
        )
        sequence += 1
    if include_clean_status:
        events.append(
            event(
                sequence,
                actor="tool",
                event_type="command",
                payload={
                    "command": "git status --porcelain=v2 --untracked-files=all",
                    "output": "",
                    "exit_code": 0,
                },
                session_id=session_id,
            )
        )
        sequence += 1
    if edit_before_commit:
        events.append(
            event(
                sequence,
                actor="assistant",
                event_type="file_edit",
                payload={"file_path": "src/calc.py", "text": "divide implementation"},
                session_id=session_id,
            )
        )
        sequence += 1
    if include_commit and commit_short:
        events.append(
            event(
                sequence,
                actor="tool",
                event_type="command",
                payload={
                    "command": "git commit -m 'add divide with zero guard'",
                    "output": f"[main {commit_short}] add divide with zero guard\n"
                    " 2 files changed, 9 insertions(+)",
                    "exit_code": 0,
                },
                session_id=session_id,
            )
        )
        sequence += 1
    if pull_request is not None:
        events.append(
            event(
                sequence,
                actor="assistant",
                event_type="message",
                payload={
                    "text": f"Opened https://github.com/demo/demo/pull/{pull_request} for review."
                },
                session_id=session_id,
            )
        )
        sequence += 1
    events.append(
        event(
            sequence,
            actor="user",
            event_type="message",
            payload={"text": "Looks good, thanks."},
            session_id=session_id,
        )
    )
    return events


def install_session(
    layout: Layout,
    *,
    host: str = "codex",
    session_id: str = "session-1",
    events: list[NormalizedEvent] | None = None,
    cwd: str | None = None,
    raw_files: dict[str, str] | None = None,
) -> Path:
    layout.ensure()
    normalized = layout.normalized_path(host, session_id)  # type: ignore[arg-type]
    write_events(normalized, events or [])
    raw_dir = layout.raw_dir(host, session_id)  # type: ignore[arg-type]
    raw_dir.mkdir(parents=True, exist_ok=True)
    if cwd is not None and host == "codex":
        (raw_dir / "thread.json").write_text(
            json.dumps({"thread_id": session_id, "cwd": cwd}), encoding="utf-8"
        )
    for name, content in (raw_files or {}).items():
        target = raw_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return raw_dir


def repo_state_json(
    root: Path,
    head_sha: str,
    head_tree: str,
    *,
    porcelain: str = "",
    submodules: str = "",
    captured_at: str = "2026-08-01T18:00:00Z",
) -> str:
    return json.dumps(
        {
            "schema_version": "retro-repo-state-v1",
            "root": str(root),
            "head_sha": head_sha,
            "head_tree": head_tree,
            "branch": "main",
            "porcelain": porcelain,
            "submodules": submodules,
            "clean": not porcelain and not submodules,
            "captured_at": captured_at,
        }
    )


def project_environment(base_sha: str) -> dict[str, Any]:
    return {
        "schema_version": "retro-project-environment-v1",
        "environment_id": "sha256:" + "a" * 64,
        "source": "explicit",
        "base_sha": base_sha,
        "image": "demo@sha256:" + "b" * 64,
        "workdir": "/workspace/repo",
        "setup": [["python3", "-m", "venv", ".venv"]],
        "smoke": [[".venv/bin/pytest", "--collect-only", "-q"]],
        "test": [[".venv/bin/pytest", "-q"]],
        "env": {},
        "network_during_build": "allowlisted",
        "network_during_run": "disabled",
        "workspace_excludes": [".venv"],
        "validated": {"base": True, "outcome": True, "runs": 2},
    }


TASK_PROMPT = (
    "The divide helper currently blows up with an unclear error when the denominator "
    "is zero. Make it reject that input with a clear exception instead."
)


def task_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "candidate_id": "goal-1-replay",
        "kind": "replay",
        "prompt": TASK_PROMPT,
        "prompt_provenance": {"user_event_ids": ["session-1:2"], "mode": "resolved_user_messages"},
        "goal_segment": {
            "introduced_event_id": "session-1:2",
            "closed_event_id": None,
            "summary": "zero guard",
        },
        "repo_evidence": [{"state": "base", "path": "src/calc.py", "reason": "target module"}],
        "scorer_brief": {
            "observables": [
                {
                    "id": "requested-behavior",
                    "description": "dividing by zero raises instead of returning",
                    "importance": "gate",
                    "evidence": ["session-1:2", "repo/outcome:tests/test_divide.py"],
                }
            ],
            "regressions_to_protect": ["existing test suite"],
            "performance": [],
            "residual_judgment": [],
            "forbidden_shortcuts": ["reference patch equality"],
        },
        "base_failure_claim": "base returns a ZeroDivisionError traceback",
        "outcome_success_claim": "outcome raises a domain error",
        "adjacency": None,
        "confidence": {"goal": 0.95, "state": 1.0, "scorability": 0.9},
    }
    payload.update(overrides)
    return payload


def definitions_payload(
    tasks: list[dict[str, Any]],
    *,
    source_id: str = "codex__session-1",
    rejections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "retro-task-definitions-v1",
        "source_id": source_id,
        "tasks": tasks,
        "rejections": rejections if rejections is not None else [],
    }
