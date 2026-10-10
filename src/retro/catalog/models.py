"""Public catalog records and update results."""
from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class SessionRecord:
    schema: int
    key: str
    host: str
    session_id: str
    agent: str
    client: str
    interactive: bool | None
    is_subagent: bool
    parent_key: str | None
    title: str | None
    first_prompt: str | None
    started_at: str | None
    ended_at: str | None
    duration_s: float | None
    active_s: float | None
    cwd: str | None
    cwd_resolved: str | None
    git: dict[str, Any]
    project: dict[str, Any]
    models: list[str]
    counts: dict[str, int]
    markers: list[dict[str, Any]]
    tags: list[str]
    tag_sources: dict[str, list[str]]
    excluded: bool
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "key": self.key,
            "host": self.host,
            "session_id": self.session_id,
            "agent": self.agent,
            "client": self.client,
            "interactive": self.interactive,
            "is_subagent": self.is_subagent,
            "parent_key": self.parent_key,
            "title": self.title,
            "first_prompt": self.first_prompt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "active_s": self.active_s,
            "cwd": self.cwd,
            "cwd_resolved": self.cwd_resolved,
            "git": copy.deepcopy(self.git),
            "project": copy.deepcopy(self.project),
            "models": list(self.models),
            "counts": dict(self.counts),
            "markers": copy.deepcopy(self.markers),
            "tags": list(self.tags),
            "tag_sources": copy.deepcopy(self.tag_sources),
            "excluded": self.excluded,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SessionRecord:
        raw_git = dict(value.get("git") or {})
        git = {
            "remote": raw_git.get("remote"),
            "remote_url": raw_git.get("remote_url"),
            "branch": raw_git.get("branch"),
            "commit": raw_git.get("commit"),
            "root": raw_git.get("root"),
            "worktree": raw_git.get("worktree"),
        }
        raw_project = dict(value.get("project") or {})
        project = {
            "id": raw_project.get("id") or "unknown",
            "name": raw_project.get("name") or "Unknown",
            "source": raw_project.get("source") or "unknown",
            "evidence": raw_project.get("evidence"),
        }
        raw_counts = dict(value.get("counts") or {})
        counts = {
            name: int(raw_counts.get(name, 0))
            for name in (
                "events",
                "user_messages",
                "assistant_messages",
                "tool_calls",
                "unknown_events",
            )
        }
        return cls(
            schema=int(value.get("schema", 1)),
            key=str(value["key"]),
            host=str(value["host"]),
            session_id=str(value["session_id"]),
            agent=str(value.get("agent") or value["host"]),
            client=str(value.get("client") or "unknown"),
            interactive=(
                value.get("interactive")
                if isinstance(value.get("interactive"), bool)
                else None
            ),
            is_subagent=bool(value.get("is_subagent", False)),
            parent_key=(
                str(value["parent_key"]) if value.get("parent_key") is not None else None
            ),
            title=str(value["title"]) if value.get("title") is not None else None,
            first_prompt=(
                str(value["first_prompt"])
                if value.get("first_prompt") is not None
                else None
            ),
            started_at=(
                str(value["started_at"]) if value.get("started_at") is not None else None
            ),
            ended_at=str(value["ended_at"]) if value.get("ended_at") is not None else None,
            duration_s=(
                float(value["duration_s"])
                if value.get("duration_s") is not None
                else None
            ),
            active_s=(
                float(value["active_s"]) if value.get("active_s") is not None else None
            ),
            cwd=str(value["cwd"]) if value.get("cwd") is not None else None,
            cwd_resolved=(
                str(value["cwd_resolved"])
                if value.get("cwd_resolved") is not None
                else None
            ),
            git=git,
            project=project,
            models=[str(item) for item in value.get("models") or []],
            counts=counts,
            markers=[dict(marker) for marker in value.get("markers") or []],
            tags=[str(tag) for tag in value.get("tags") or []],
            tag_sources={
                str(tag): [str(source) for source in sources]
                for tag, sources in dict(value.get("tag_sources") or {}).items()
            },
            excluded=bool(value.get("excluded", False)),
            fingerprint=str(value.get("fingerprint") or ""),
        )


class Catalog(Mapping[str, SessionRecord]):
    def __init__(self, records: Mapping[str, SessionRecord] | None = None):
        self._records = dict(records or {})

    def __getitem__(self, key: str) -> SessionRecord:
        return self._records[key]

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._records))

    def __len__(self) -> int:
        return len(self._records)

    def included(self) -> Iterator[SessionRecord]:
        return (self._records[key] for key in sorted(self._records) if not self._records[key].excluded)

    def excluded(self) -> Iterator[SessionRecord]:
        return (self._records[key] for key in sorted(self._records) if self._records[key].excluded)


@dataclass(frozen=True)
class CatalogUpdate:
    changed_keys: list[str]
    added_keys: list[str]
    removed_keys: list[str]
    total: int
    excluded_total: int
