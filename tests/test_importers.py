"""Tests for Claude, Codex, and VS Code Copilot importers."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from retro.importers.base import (
    GHOSTLAB_COPILOT_SESSION_ID_PREFIX,
    GHOSTLAB_ORIGINATOR,
)
from retro.importers.claude import ClaudeImporter
from retro.importers.codex import CodexImporter
from retro.importers.copilot import CopilotImporter
from retro.importers.copilot_cli import CopilotCliImporter
from retro.importers.vscode_copilot import (
    VscodeCopilotImporter,
    _load_core_session,
)
from retro.schema import read_events
from retro.storage import Layout


def _write_tagged_codex_rollout(
    source: Path,
    destination: Path,
    thread_id: str = "thread-ghostlab",
) -> None:
    records = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["payload"].update(
        {"id": thread_id, "originator": GHOSTLAB_ORIGINATOR}
    )
    destination.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


# ---- Claude importer -------------------------------------------------------


class TestClaudeImporter:
    def _make_importer(self, tmp_path: Path, transcript: Path) -> tuple[ClaudeImporter, Layout]:
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        claude_home = tmp_path / "claude-home"
        projects = claude_home / "projects" / "my-project"
        projects.mkdir(parents=True)
        shutil.copy2(transcript, projects / "sess-100.jsonl")
        return ClaudeImporter(layout, claude_home=claude_home), layout

    def test_discover(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        sessions = imp.discover()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-100"
        assert sessions[0].project_slug == "my-project"

    def test_import_creates_artifacts(self, tmp_path: Path, claude_transcript: Path):
        imp, layout = self._make_importer(tmp_path, claude_transcript)
        result = imp.import_session(identifier="sess-100")

        assert result.host == "claude-code"
        assert result.session_id == "sess-100"
        assert result.event_count > 0
        assert result.raw_dir.exists()
        assert result.normalized_path.exists()
        assert (result.raw_dir / "transcript.jsonl").exists()
        assert (result.raw_dir / "import_meta.json").exists()

    def test_normalized_events_have_correct_types(self, tmp_path: Path, claude_transcript: Path):
        imp, layout = self._make_importer(tmp_path, claude_transcript)
        result = imp.import_session(identifier="sess-100")
        events = list(read_events(result.normalized_path))

        types = [e.event_type for e in events]
        assert "message" in types
        assert "file_read" in types
        assert "file_edit" in types
        assert "command" in types

        actors = {e.actor for e in events}
        assert "user" in actors
        assert "assistant" in actors
        assert "tool" in actors

    def test_reimport_blocked_without_force(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        imp.import_session(identifier="sess-100")

        with pytest.raises(FileExistsError):
            imp.import_session(identifier="sess-100")

    def test_reimport_allowed_with_force(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        imp.import_session(identifier="sess-100")
        result = imp.import_session(identifier="sess-100", force=True)
        assert result.event_count > 0

    def test_latest(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        latest = imp.latest()
        assert latest is not None
        assert latest.session_id == "sess-100"

    def test_find_missing_session(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        with pytest.raises(FileNotFoundError):
            imp.import_session(identifier="nonexistent")

    def test_reimport_allowed_without_force_if_newer(self, tmp_path: Path, claude_transcript: Path):
        imp, _ = self._make_importer(tmp_path, claude_transcript)
        imp.import_session(identifier="sess-100")

        # Modifying the source file (appending and shifting mtime)
        session = imp.find_session("sess-100")
        import os
        import time
        with session.transcript_path.open("a", encoding="utf-8") as f:
            f.write("\n")
        new_mtime = time.time() + 10
        os.utime(session.transcript_path, (new_mtime, new_mtime))

        # Should be allowed to re-import without force
        result = imp.import_session(identifier="sess-100")
        assert result.event_count > 0


# ---- Codex importer --------------------------------------------------------


class TestCodexImporter:
    def _make_importer(self, tmp_path: Path, rollout: Path) -> tuple[CodexImporter, Layout]:
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        codex_home = tmp_path / "codex-home"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        shutil.copy2(rollout, sessions / "thread-100.jsonl")
        return CodexImporter(layout, codex_home=codex_home), layout

    def test_discover(self, tmp_path: Path, codex_rollout: Path):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        threads = imp.discover()
        assert len(threads) == 1
        assert threads[0].thread_id == "thread-001"

    def test_discover_ignores_ghostlab_rollout(
        self,
        tmp_path: Path,
        codex_rollout: Path,
    ):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        tagged = imp.codex_home / "sessions" / "ghostlab.jsonl"
        _write_tagged_codex_rollout(codex_rollout, tagged)

        assert [thread.thread_id for thread in imp.discover()] == ["thread-001"]
        with pytest.raises(FileNotFoundError):
            imp.import_session(identifier="thread-ghostlab")

    def test_sqlite_discovery_ignores_ghostlab_rollout(
        self,
        tmp_path: Path,
        codex_rollout: Path,
    ):
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        codex_home = tmp_path / "codex-home"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        normal = sessions / "normal.jsonl"
        tagged = sessions / "ghostlab.jsonl"
        shutil.copy2(codex_rollout, normal)
        _write_tagged_codex_rollout(codex_rollout, tagged)
        con = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            con.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, cwd TEXT, "
                "created_at INTEGER, updated_at INTEGER, model_provider TEXT, "
                "git_branch TEXT, archived INTEGER)"
            )
            con.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "thread-001",
                        str(normal),
                        "normal",
                        "/workspace",
                        1,
                        2,
                        "openai",
                        "main",
                        0,
                    ),
                    (
                        "thread-ghostlab",
                        str(tagged),
                        "synthetic",
                        "/workspace",
                        1,
                        3,
                        "openai",
                        "main",
                        0,
                    ),
                ],
            )
            con.commit()
        finally:
            con.close()

        discovered = CodexImporter(layout, codex_home=codex_home).discover()

        assert [thread.thread_id for thread in discovered] == ["thread-001"]

    def test_import_creates_artifacts(self, tmp_path: Path, codex_rollout: Path):
        imp, layout = self._make_importer(tmp_path, codex_rollout)
        result = imp.import_session(identifier="thread-001")

        assert result.host == "codex"
        assert result.session_id == "thread-001"
        assert result.event_count > 0
        assert result.raw_dir.exists()
        assert result.normalized_path.exists()
        assert (result.raw_dir / "rollout.jsonl").exists()
        assert (result.raw_dir / "thread.json").exists()

    def test_normalized_events_have_correct_types(self, tmp_path: Path, codex_rollout: Path):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        result = imp.import_session(identifier="thread-001")
        events = list(read_events(result.normalized_path))

        types = [e.event_type for e in events]
        assert "session_start" in types
        assert "message" in types
        assert "file_read" in types
        assert "file_edit" in types
        assert "command" in types

    def test_tool_call_type_overrides(self, tmp_path: Path, codex_rollout: Path):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        result = imp.import_session(identifier="thread-001")
        events = list(read_events(result.normalized_path))

        read_events_list = [e for e in events if e.event_type == "file_read"]
        assert len(read_events_list) >= 1

        edit_events = [e for e in events if e.event_type == "file_edit"]
        assert len(edit_events) >= 1

        command_events = [e for e in events if e.event_type == "command"]
        assert len(command_events) >= 1

    def test_reimport_blocked_without_force(self, tmp_path: Path, codex_rollout: Path):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        imp.import_session(identifier="thread-001")

        with pytest.raises(FileExistsError):
            imp.import_session(identifier="thread-001")

    def test_reimport_allowed_without_force_if_newer(self, tmp_path: Path, codex_rollout: Path):
        imp, _ = self._make_importer(tmp_path, codex_rollout)
        imp.import_session(identifier="thread-001")

        # Modifying the source rollout file (appending and shifting mtime)
        thread = imp.find_thread("thread-001")
        import os
        import time
        with thread.rollout_path.open("a", encoding="utf-8") as f:
            f.write("\n")
        new_mtime = time.time() + 10
        os.utime(thread.rollout_path, (new_mtime, new_mtime))

        # Should be allowed to re-import without force
        result = imp.import_session(identifier="thread-001")
        assert result.event_count > 0


# ---- VS Code Copilot importer -----------------------------------------------


class TestVscodeCopilotImporter:
    def _make_importer(
        self,
        tmp_path: Path,
        user_data: Path,
    ) -> tuple[VscodeCopilotImporter, Layout]:
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        return VscodeCopilotImporter(layout, user_data_dir=user_data), layout

    def test_discover(self, tmp_path: Path, vscode_copilot_user_data: Path):
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)

        sessions = imp.discover()

        assert len(sessions) == 1
        session = sessions[0]
        assert session.session_id == "copilot-session-001"
        assert session.workspace_name == "demo"
        assert session.cwd == "/workspace/demo"
        assert session.title == "Update app and test"
        assert session.models == ("copilot/gpt-5.4",)
        assert session.request_count == 1
        assert session.transcript_path is not None

    def test_import_captures_all_available_artifacts(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
    ):
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)

        result = imp.import_session(identifier="copilot-session-001")

        assert result.host == "vscode-copilot"
        assert result.event_count > 0
        assert (result.raw_dir / "session.jsonl").exists()
        assert (result.raw_dir / "session.snapshot.json").exists()
        assert (result.raw_dir / "transcript.jsonl").exists()
        assert (result.raw_dir / "import_meta.json").exists()
        assert (result.raw_dir / "sidecars" / "workspace.json").exists()
        assert (result.raw_dir / "sidecars" / "chatEditingSession" / "state.json").exists()
        assert (
            result.raw_dir
            / "sidecars"
            / "chat-session-resources"
            / "call-read"
            / "content.txt"
        ).exists()
        store = json.loads(
            (result.raw_dir / "sidecars" / "session-store.json").read_text(encoding="utf-8")
        )
        assert store["sessions"][0]["id"] == "copilot-session-001"
        assert store["turns"][0]["turn_index"] == 0

    def test_transcript_normalization_preserves_tools_reasoning_and_parents(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
    ):
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)

        result = imp.import_session(identifier="copilot-session-001")
        events = list(read_events(result.normalized_path))

        assert result.unknown_event_count == 0
        assert {event.event_type for event in events} >= {
            "session_start",
            "message",
            "reasoning",
            "file_read",
            "command",
            "attachment",
        }
        read_call = next(
            event
            for event in events
            if event.actor == "assistant"
            and event.event_type == "file_read"
            and event.payload.get("call_id") == "call-read"
        )
        assert read_call.payload["input"]["filePath"] == "/workspace/demo/app.py"
        assert read_call.payload["vscode_chat_raw_ref"]["line"] == 3
        assert read_call.raw_ref.line == 5
        assistant_message = next(
            event
            for event in events
            if event.event_id == "copilot-session-001:transcript:event-004"
        )
        assert assistant_message.event_type == "message"
        assert read_call.parent_event_id == assistant_message.event_id

        command_start = next(
            event
            for event in events
            if event.actor == "assistant"
            and event.event_type == "command"
            and event.payload.get("call_id") == "call-command"
        )
        command_result = next(
            event
            for event in events
            if event.actor == "tool"
            and event.event_type == "command"
            and event.payload.get("call_id") == "call-command"
        )
        assert command_result.payload["success"] is False
        assert command_result.parent_event_id == command_start.event_id
        assert command_result.payload["result"]["exitCode"] == 1

    def test_core_snapshot_fallback_normalizes_edits_and_permissions(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
    ):
        transcript = (
            vscode_copilot_user_data
            / "workspaceStorage"
            / "workspace-001"
            / "GitHub.copilot-chat"
            / "transcripts"
            / "copilot-session-001.jsonl"
        )
        transcript.unlink()
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)

        result = imp.import_session(identifier="copilot-session-001")
        events = list(read_events(result.normalized_path))

        assert result.unknown_event_count == 0
        assert any(event.event_type == "file_edit" for event in events)
        assert any(event.event_type == "permission" for event in events)
        assert any(
            event.event_type == "command"
            and event.actor == "tool"
            and event.payload["is_error"] is True
            for event in events
        )

    def test_jsonl_mutation_log_reconstruction(
        self,
        vscode_copilot_session: Path,
    ):
        core = _load_core_session(vscode_copilot_session)

        assert core.data["customTitle"] == "Update app and test"
        assert core.data["requests"][0]["promptTokens"] == 120
        assert "inputText" not in core.data["inputState"]
        assert core.line_for(("requests", 0, "response", 0)) == 3
        assert core.line_for(("requests", 0, "promptTokens")) == 4

    def test_reimport_blocked_without_force(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
    ):
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)
        imp.import_session(identifier="copilot-session-001")

        with pytest.raises(FileExistsError):
            imp.import_session(identifier="copilot-session-001")

    def test_reimport_allowed_when_source_is_newer(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
    ):
        imp, _ = self._make_importer(tmp_path, vscode_copilot_user_data)
        imp.import_session(identifier="copilot-session-001")
        source = (
            vscode_copilot_user_data
            / "workspaceStorage"
            / "workspace-001"
            / "chatSessions"
            / "copilot-session-001.jsonl"
        )
        with source.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        changed = time.time() + 10
        os.utime(source, (changed, changed))

        result = imp.import_session(identifier="copilot-session-001")

        assert result.event_count > 0


# ---- Copilot CLI / Agent Host importer --------------------------------------


class TestCopilotCliImporter:
    def _make_importer(
        self,
        tmp_path: Path,
        state: tuple[Path, Path],
    ) -> tuple[CopilotCliImporter, Layout]:
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        state_root, db_path = state
        return (
            CopilotCliImporter(
                layout,
                session_state_dir=state_root,
                session_store_db=db_path,
            ),
            layout,
        )

    def test_discover_includes_active_and_completed_sessions(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)

        sessions = imp.discover()

        assert {session.session_id for session in sessions} == {
            "cli-session-active",
            "cli-session-complete",
        }
        active = next(
            session for session in sessions if session.session_id == "cli-session-active"
        )
        assert active.active is True
        assert active.source_kind == "copilot-cli"
        assert active.title == "Inspect app and verify"
        assert active.cwd == "/workspace/demo"
        assert active.workspace_name == "demo"
        assert set(active.models) == {"gpt-5.6-sol", "claude-sonnet-5"}
        assert active.request_count == 1

    def test_discover_ignores_ghostlab_session_ids(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        state_root, _ = copilot_cli_state
        session_id = (
            f"{GHOSTLAB_COPILOT_SESSION_ID_PREFIX}"
            "4abc-8abc-0123456789ab"
        )
        tagged = state_root / session_id
        tagged.mkdir()
        (tagged / "events.jsonl").write_text("not json\n", encoding="utf-8")
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)

        assert {session.session_id for session in imp.discover()} == {
            "cli-session-active",
            "cli-session-complete",
        }
        with pytest.raises(FileNotFoundError):
            imp.import_session(identifier=session_id)

    def test_import_captures_event_log_metadata_and_usage(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)

        result = imp.import_session(identifier="cli-session-active")

        assert result.host == "vscode-copilot"
        assert (result.raw_dir / "events.jsonl").exists()
        assert (result.raw_dir / "import_meta.json").exists()
        assert (result.raw_dir / "sidecars" / "workspace.yaml").exists()
        store = json.loads(
            (result.raw_dir / "sidecars" / "session-store.json").read_text(
                encoding="utf-8"
            )
        )
        assert store["sessions"][0]["id"] == "cli-session-active"
        assert len(store["assistant_usage_events"]) == 2
        meta = json.loads(
            (result.raw_dir / "import_meta.json").read_text(encoding="utf-8")
        )
        assert meta["source_kind"] == "copilot-cli"
        assert meta["active"] is True

    def test_normalization_reconstructs_chunks_and_agent_lifecycle(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)

        result = imp.import_session(identifier="cli-session-active")
        events = list(read_events(result.normalized_path))

        assert result.unknown_event_count == 1
        assert result.gaps == ["future.event"]
        assistant_messages = [
            event
            for event in events
            if event.actor == "assistant" and event.event_type == "message"
        ]
        assert [event.payload["text"] for event in assistant_messages] == [
            "I will inspect it."
        ]
        reasoning = next(event for event in events if event.event_type == "reasoning")
        assert reasoning.payload["thinking"] == "Read before editing. Then run tests."
        assert reasoning.payload["raw_lines"] == [4, 5]

        read_call = next(
            event
            for event in events
            if event.actor == "assistant"
            and event.event_type == "file_read"
            and event.payload.get("call_id") == "call-read"
        )
        read_result = next(
            event
            for event in events
            if event.actor == "tool"
            and event.event_type == "file_read"
            and event.payload.get("call_id") == "call-read"
        )
        assert read_result.parent_event_id == read_call.event_id
        assert read_result.payload["output"]["content"] == "print('hello')"
        assert sum(event.event_type == "permission" for event in events) == 2
        assert any(event.event_type == "file_edit" for event in events)
        assert any(event.event_type == "subagent_start" for event in events)
        assert any(event.event_type == "subagent_end" for event in events)
        assert any(event.actor == "hook" for event in events)
        unknown = next(event for event in events if event.event_type == "unknown")
        assert unknown.payload["data"]["futureField"] == "preserve me"

    def test_completed_session_emits_session_end(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)

        result = imp.import_session(identifier="cli-session-complete")
        events = list(read_events(result.normalized_path))

        assert [event.event_type for event in events] == [
            "session_start",
            "message",
            "session_end",
        ]

    def test_reimport_allowed_when_active_log_grows(
        self,
        tmp_path: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        imp, _ = self._make_importer(tmp_path, copilot_cli_state)
        imp.import_session(identifier="cli-session-active")
        state_root, _ = copilot_cli_state
        source = state_root / "cli-session-active" / "events.jsonl"
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "system.notification",
                        "id": "cli-event-025",
                        "parentId": "cli-event-024",
                        "timestamp": "2026-08-26T20:00:05.000Z",
                        "data": {"content": "Session updated", "kind": "info"},
                    }
                )
                + "\n"
            )
        changed = time.time() + 10
        os.utime(source, (changed, changed))

        result = imp.import_session(identifier="cli-session-active")

        assert result.event_count > 0

    def test_composite_importer_combines_both_local_sources(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        state_root, db_path = copilot_cli_state
        imp = CopilotImporter(
            layout,
            user_data_dir=vscode_copilot_user_data,
            session_state_dir=state_root,
            session_store_db=db_path,
        )

        sessions = imp.discover()

        assert {session.session_id for session in sessions} == {
            "copilot-session-001",
            "cli-session-active",
            "cli-session-complete",
        }
        assert {session.source_kind for session in sessions} == {
            "vscode-chat",
            "copilot-cli",
        }

    def test_interleaved_chunk_streams_are_reconstructed_independently(
        self,
        tmp_path: Path,
    ):
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        events_path = tmp_path / "interleaved.jsonl"
        records = [
            {
                "type": "assistant.message",
                "id": "a-0",
                "timestamp": "2026-08-26T20:00:00Z",
                "data": {
                    "turnId": "turn-a",
                    "interactionId": "interaction",
                    "model": "gpt-5.6-sol",
                    "chunkIndex": 0,
                    "chunkCount": 2,
                    "content": "Hello ",
                },
            },
            {
                "type": "assistant.message",
                "id": "b-0",
                "agentId": "subagent",
                "timestamp": "2026-08-26T20:00:00Z",
                "data": {
                    "turnId": "turn-b",
                    "parentToolCallId": "subagent-call",
                    "interactionId": "interaction",
                    "model": "claude-sonnet-5",
                    "chunkIndex": 0,
                    "chunkCount": 2,
                    "content": "Sub ",
                },
            },
            {
                "type": "assistant.message",
                "id": "a-1",
                "timestamp": "2026-08-26T20:00:01Z",
                "data": {
                    "turnId": "turn-a",
                    "interactionId": "interaction",
                    "model": "gpt-5.6-sol",
                    "chunkIndex": 1,
                    "chunkCount": 2,
                    "content": "world",
                },
            },
            {
                "type": "assistant.message",
                "id": "b-1",
                "agentId": "subagent",
                "timestamp": "2026-08-26T20:00:01Z",
                "data": {
                    "turnId": "turn-b",
                    "parentToolCallId": "subagent-call",
                    "interactionId": "interaction",
                    "model": "claude-sonnet-5",
                    "chunkIndex": 1,
                    "chunkCount": 2,
                    "content": "agent done",
                },
            },
        ]
        events_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        imp = CopilotCliImporter(layout, session_state_dir=tmp_path / "state")

        events, unknown, gaps = imp._normalize(events_path, "interleaved")

        assert unknown == 0
        assert gaps == []
        assert [event.payload["text"] for event in events] == [
            "Hello world",
            "Sub agent done",
        ]

    def test_composite_duplicate_selection_matches_discovery(
        self,
        tmp_path: Path,
        vscode_copilot_user_data: Path,
        copilot_cli_state: tuple[Path, Path],
    ):
        state_root, db_path = copilot_cli_state
        duplicate = state_root / "copilot-session-001"
        duplicate.mkdir()
        source = state_root / "cli-session-complete" / "events.jsonl"
        shutil.copy2(source, duplicate / "events.jsonl")
        old_time = time.time() - 100
        os.utime(duplicate / "events.jsonl", (old_time, old_time))
        vscode_session = (
            vscode_copilot_user_data
            / "workspaceStorage"
            / "workspace-001"
            / "chatSessions"
            / "copilot-session-001.jsonl"
        )
        current_time = time.time()
        os.utime(vscode_session, (current_time, current_time))
        layout = Layout(tmp_path / "rollout-memory")
        layout.ensure()
        imp = CopilotImporter(
            layout,
            user_data_dir=vscode_copilot_user_data,
            session_state_dir=state_root,
            session_store_db=db_path,
        )

        discovered = next(
            session
            for session in imp.discover()
            if session.session_id == "copilot-session-001"
        )
        found = imp.find_session("copilot-session-001")

        assert discovered.source_kind == "vscode-chat"
        assert found is discovered
        result = imp.import_session(identifier="copilot-session-001")
        assert (result.raw_dir / "session.snapshot.json").exists()
        assert not (result.raw_dir / "events.jsonl").exists()
