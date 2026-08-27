"""Shared fixtures for the retro test suite."""
from __future__ import annotations

import json
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
def copilot_cli_events() -> Path:
    return FIXTURES / "copilot_cli_events.jsonl"


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
def copilot_cli_state(
    tmp_path: Path,
    copilot_cli_events: Path,
) -> tuple[Path, Path]:
    copilot_home = tmp_path / "copilot-home"
    state_root = copilot_home / "session-state"
    active = state_root / "cli-session-active"
    active.mkdir(parents=True)
    shutil.copy2(copilot_cli_events, active / "events.jsonl")
    (active / "workspace.yaml").write_text(
        "\n".join(
            [
                'id: "cli-session-active"',
                'cwd: "/workspace/demo"',
                'git_root: "/workspace/demo"',
                'repository: "example/demo"',
                'host_type: "github"',
                'branch: "main"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (active / "inuse.123.lock").write_text("", encoding="utf-8")

    complete = state_root / "cli-session-complete"
    complete.mkdir(parents=True)
    completed_events = [
        {
            "type": "session.start",
            "id": "complete-001",
            "parentId": None,
            "timestamp": "2026-08-25T10:00:00.000Z",
            "data": {
                "sessionId": "cli-session-complete",
                "selectedModel": "grok-4.6",
            },
        },
        {
            "type": "user.message",
            "id": "complete-002",
            "parentId": "complete-001",
            "timestamp": "2026-08-25T10:00:01.000Z",
            "data": {"content": "Completed session"},
        },
        {
            "type": "session.shutdown",
            "id": "complete-003",
            "parentId": "complete-002",
            "timestamp": "2026-08-25T10:00:02.000Z",
            "data": {
                "shutdownType": "normal",
                "modelMetrics": [],
                "totalPremiumRequests": 0,
            },
        },
    ]
    (complete / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in completed_events),
        encoding="utf-8",
    )

    empty = state_root / "cli-session-empty"
    empty.mkdir(parents=True)
    (empty / "workspace.yaml").write_text(
        'id: "cli-session-empty"\n',
        encoding="utf-8",
    )

    db_path = copilot_home / "session-store.db"
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                host_type TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                turn_index INTEGER,
                user_message TEXT,
                assistant_response TEXT,
                timestamp TEXT
            );
            CREATE TABLE assistant_usage_events (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                turn_index INTEGER,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                total_nano_aiu INTEGER,
                request_multiplier REAL
            );
            INSERT INTO sessions VALUES (
                'cli-session-active',
                '/workspace/demo',
                'example/demo',
                'github',
                'main',
                'Inspect app and verify',
                '2026-08-26T20:00:00.000Z',
                '2026-08-26T20:00:04.800Z'
            );
            INSERT INTO sessions VALUES (
                'cli-session-complete',
                '/workspace/complete',
                'example/complete',
                'github',
                'main',
                'Completed session',
                '2026-08-25T10:00:00.000Z',
                '2026-08-25T10:00:02.000Z'
            );
            INSERT INTO sessions VALUES (
                'cli-session-empty',
                '/workspace/empty',
                'example/empty',
                'github',
                'main',
                NULL,
                '2026-08-24T10:00:00.000Z',
                '2026-08-24T10:00:00.000Z'
            );
            INSERT INTO turns VALUES (
                1,
                'cli-session-active',
                0,
                'Inspect app.py and verify it',
                'I will inspect it.',
                '2026-08-26T20:00:01.000Z'
            );
            INSERT INTO assistant_usage_events VALUES (
                1,
                'cli-session-active',
                0,
                'gpt-5.6-sol',
                1000,
                100,
                700,
                50,
                25,
                1200,
                1.0
            );
            INSERT INTO assistant_usage_events VALUES (
                2,
                'cli-session-active',
                0,
                'claude-sonnet-5',
                500,
                50,
                300,
                20,
                10,
                600,
                0.5
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return state_root, db_path


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


@pytest.fixture
def copilot_cli_imported(
    tmp_layout: Layout,
    copilot_cli_state: tuple[Path, Path],
) -> tuple[Layout, str]:
    """Import a Copilot Agent Host session into a temp layout."""
    from retro.importers.copilot_cli import CopilotCliImporter

    state_root, db_path = copilot_cli_state
    imp = CopilotCliImporter(
        tmp_layout,
        session_state_dir=state_root,
        session_store_db=db_path,
    )
    result = imp.import_session(identifier="cli-session-active")
    return tmp_layout, result.session_id
