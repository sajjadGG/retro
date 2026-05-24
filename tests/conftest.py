"""Shared fixtures for the retro test suite."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from retro.storage import Layout

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def claude_transcript() -> Path:
    return FIXTURES / "claude_transcript.jsonl"


@pytest.fixture
def codex_rollout() -> Path:
    return FIXTURES / "codex_rollout.jsonl"


@pytest.fixture
def tmp_layout(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "rollout-memory")
    layout.ensure()
    return layout


@pytest.fixture
def claude_imported(tmp_layout: Layout, claude_transcript: Path) -> tuple[Layout, str]:
    """Import a Claude session into a temp layout and return (layout, session_id)."""
    from retro.importers.claude import ClaudeImporter

    claude_home = tmp_layout.root.parent / "fake-claude-home"
    projects = claude_home / "projects" / "test-project"
    projects.mkdir(parents=True)
    dest = projects / "test-session-001.jsonl"
    shutil.copy2(claude_transcript, dest)

    imp = ClaudeImporter(tmp_layout, claude_home=claude_home)
    result = imp.import_session(identifier="test-session-001")
    return tmp_layout, result.session_id


@pytest.fixture
def codex_imported(tmp_layout: Layout, codex_rollout: Path) -> tuple[Layout, str]:
    """Import a Codex session into a temp layout and return (layout, session_id)."""
    from retro.importers.codex import CodexImporter

    codex_home = tmp_layout.root.parent / "fake-codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    dest = sessions / "thread-001.jsonl"
    shutil.copy2(codex_rollout, dest)

    imp = CodexImporter(tmp_layout, codex_home=codex_home)
    result = imp.import_session(identifier="thread-001")
    return tmp_layout, result.session_id
