"""Shared fixtures for the retro test suite."""
from __future__ import annotations

import shutil
import sqlite3
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
def vscode_copilot_session() -> Path:
    return FIXTURES / "vscode_copilot_session.jsonl"


@pytest.fixture
def vscode_copilot_transcript() -> Path:
    return FIXTURES / "vscode_copilot_transcript.jsonl"


@pytest.fixture
def vscode_copilot_user_data(
    tmp_path: Path,
    vscode_copilot_session: Path,
    vscode_copilot_transcript: Path,
) -> Path:
    user_dir = tmp_path / "vscode-user"
    workspace = user_dir / "workspaceStorage" / "workspace-001"
    chat_sessions = workspace / "chatSessions"
    chat_sessions.mkdir(parents=True)
    shutil.copy2(
        vscode_copilot_session,
        chat_sessions / "copilot-session-001.jsonl",
    )
    (workspace / "workspace.json").write_text(
        '{"folder":"file:///workspace/demo"}',
        encoding="utf-8",
    )

    transcripts = workspace / "GitHub.copilot-chat" / "transcripts"
    transcripts.mkdir(parents=True)
    shutil.copy2(
        vscode_copilot_transcript,
        transcripts / "copilot-session-001.jsonl",
    )

    editing = workspace / "chatEditingSessions" / "copilot-session-001"
    editing.mkdir(parents=True)
    (editing / "state.json").write_text(
        '{"version":1,"initialFileContents":[],"timeline":[],"recentSnapshot":null}',
        encoding="utf-8",
    )

    resources = (
        workspace
        / "GitHub.copilot-chat"
        / "chat-session-resources"
        / "copilot-session-001"
        / "call-read"
    )
    resources.mkdir(parents=True)
    (resources / "content.txt").write_text("print('before')\n", encoding="utf-8")

    extension_storage = user_dir / "globalStorage" / "github.copilot-chat"
    extension_storage.mkdir(parents=True)
    con = sqlite3.connect(extension_storage / "session-store.db")
    try:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                host_type TEXT,
                summary TEXT
            );
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                turn_index INTEGER,
                user_message TEXT,
                assistant_response TEXT
            );
            INSERT INTO sessions VALUES (
                'copilot-session-001',
                '/workspace/demo',
                'demo',
                'vscode',
                'Update app and test'
            );
            INSERT INTO turns VALUES (
                1,
                'copilot-session-001',
                0,
                'Update app.py and run its tests',
                'The test command failed and needs attention.'
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return user_dir


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


@pytest.fixture
def copilot_imported(
    tmp_layout: Layout,
    vscode_copilot_user_data: Path,
) -> tuple[Layout, str]:
    """Import a VS Code Copilot session into a temp layout."""
    from retro.importers.vscode_copilot import VscodeCopilotImporter

    imp = VscodeCopilotImporter(tmp_layout, user_data_dir=vscode_copilot_user_data)
    result = imp.import_session(identifier="copilot-session-001")
    return tmp_layout, result.session_id
