"""Tests for Claude and Codex importers."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from retro.importers.claude import ClaudeImporter
from retro.importers.codex import CodexImporter
from retro.schema import read_events
from retro.storage import Layout

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
