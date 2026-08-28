"""Composite importer for all locally persisted VS Code Copilot sessions."""
from __future__ import annotations

from pathlib import Path
from typing import Union

from ..schema import Host
from ..storage import Layout
from .base import ImportResult
from .copilot_cli import CopilotCliImporter, CopilotCliSession
from .vscode_copilot import CopilotSession as VscodeCopilotSession
from .vscode_copilot import VscodeCopilotImporter

CopilotSession = Union[VscodeCopilotSession, CopilotCliSession]


class CopilotImporter:
    host: Host = "vscode-copilot"

    def __init__(
        self,
        layout: Layout,
        *,
        user_data_dir: Path | None = None,
        session_state_dir: Path | None = None,
        session_store_db: Path | None = None,
    ):
        self.vscode = VscodeCopilotImporter(
            layout,
            user_data_dir=user_data_dir,
        )
        self.agent_host = CopilotCliImporter(
            layout,
            session_state_dir=session_state_dir,
            session_store_db=session_store_db,
        )
        self._discover_cache: list[CopilotSession] | None = None

    def discover(self) -> list[CopilotSession]:
        if self._discover_cache is not None:
            return list(self._discover_cache)
        sessions: dict[str, CopilotSession] = {}
        for vscode_session in self.vscode.discover():
            sessions[vscode_session.session_id] = vscode_session
        for agent_host_session in self.agent_host.discover():
            previous = sessions.get(agent_host_session.session_id)
            if previous is None or agent_host_session.mtime >= previous.mtime:
                sessions[agent_host_session.session_id] = agent_host_session
        self._discover_cache = sorted(
            sessions.values(),
            key=lambda item: item.mtime,
            reverse=True,
        )
        return list(self._discover_cache)

    def find_session(self, session_id: str) -> CopilotSession | None:
        return next(
            (session for session in self.discover() if session.session_id == session_id),
            None,
        )

    def latest(self) -> CopilotSession | None:
        sessions = self.discover()
        return sessions[0] if sessions else None

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        session = self.find_session(identifier)
        if isinstance(session, CopilotCliSession):
            return self.agent_host.import_session(identifier=identifier, force=force)
        if isinstance(session, VscodeCopilotSession):
            return self.vscode.import_session(identifier=identifier, force=force)
        raise FileNotFoundError(
            f"No local VS Code Copilot or Copilot Agent Host session found with id {identifier!r}"
        )
