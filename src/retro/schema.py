"""Normalized event schema shared by all host importers.

The wire format intentionally mirrors the JSON shape in
`specs/full_rollout_capture_feature_spec.md` so that downstream tools can
consume it without consulting Python.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Actor = Literal["user", "assistant", "tool", "system", "subagent", "hook"]
EventType = Literal[
    "message",
    "tool_call",
    "tool_result",
    "file_read",
    "file_edit",
    "command",
    "error",
    "session_start",
    "session_end",
    "subagent_start",
    "subagent_end",
    "compaction",
    "permission",
    "attachment",
    "reasoning",
    "unknown",
]
Host = Literal["claude-code", "codex"]


@dataclass
class RawRef:
    path: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line}


@dataclass
class NormalizedEvent:
    event_id: str
    session_id: str
    host: Host
    sequence: int
    actor: Actor
    event_type: EventType
    summary: str
    raw_ref: RawRef
    timestamp: str | None = None
    parent_event_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw_ref"] = self.raw_ref.to_dict()
        return d


def write_events(path: Path, events: Iterable[NormalizedEvent]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev.to_dict(), ensure_ascii=False))
            fh.write("\n")
            count += 1
    return count


def read_events(path: Path) -> Iterator[NormalizedEvent]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            raw_ref = d.pop("raw_ref")
            yield NormalizedEvent(raw_ref=RawRef(**raw_ref), **d)
