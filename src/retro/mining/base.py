"""Core mining abstractions: MemoryCandidate, MiningResult, registries.

The data shape mirrors the JSON contract in `specs/rollout_mining_methods.md`
(Shared Output section). Methods extend `MemoryCandidate.structured` with
their own typed fields when they need richer state (skills carry
activation/execution/termination blocks; procedures carry ordered steps).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Sequence

from ..schema import Host, NormalizedEvent

MemoryKind = Literal[
    "procedure",
    "skill",
    "failure_trigger",
    "user_preference",
    "repo_convention",
    "tool_lesson",
    "risk_rule",
    "case",
]
MemoryRisk = Literal["low", "medium", "high"]
# Scope answers: who or what does this memory apply to?
#   - user:   broad working-style or assistant-behavior pattern that crosses
#             projects. The user wants this in every future session.
#   - repo:   specific to the repository / codebase the rollout ran in. The
#             memory references local files, conventions, or workflows.
#   - task:   tied to a particular kind of task (e.g. "drafting specs"),
#             repo-agnostic.
#   - global: foundational rule about evaluating systems, security, or
#             tooling that applies everywhere.
MemoryScope = Literal["user", "repo", "task", "global"]


@dataclass
class MemoryCandidate:
    id: str
    method: str
    kind: MemoryKind
    text: str
    when_to_use: str
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.5
    priority: int = 3
    scope: MemoryScope = "repo"
    risk: MemoryRisk = "medium"
    structured: dict[str, Any] | None = None  # method-specific payload
    # Free-form trace for *why* `scope` was assigned. Surfaced in the dashboard
    # tooltip on the scope pill so users can question / override the heuristic.
    scope_reason: str = ""
    # Origin info: the repo/cwd this memory was mined from, when known. Helps
    # filter repo-scoped memories per-repo and shows the source on cards.
    origin_repo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.structured is None:
            d.pop("structured", None)
        return d


@dataclass
class MiningResult:
    session_id: str
    host: Host
    method: str
    task_summary: str
    candidates: list[MemoryCandidate]
    notes: list[str] = field(default_factory=list)
    filters_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "host": self.host,
            "method": self.method,
            "task_summary": self.task_summary,
            "candidates": [c.to_dict() for c in self.candidates],
            "notes": list(self.notes),
            "filters_applied": list(self.filters_applied),
        }


@dataclass
class MiningContext:
    """All inputs a mining method or filter gets to work with."""

    session_id: str
    host: Host
    events: Sequence[NormalizedEvent]
    normalized_path: Path | None = None

    def task_summary(self) -> str:
        for ev in self.events:
            if ev.actor == "user" and ev.event_type == "message":
                text = event_text(ev).strip().replace("\n", " ")
                return truncate(text, 220)
        return self.events[0].summary if self.events else ""

    def origin_repo(self) -> str | None:
        """Best-effort: which cwd / repo did this rollout run in?"""
        for ev in self.events:
            payload = ev.payload or {}
            for key in ("cwd", "current_working_directory"):
                v = payload.get(key)
                if isinstance(v, str) and v:
                    return v
        return None

    def artifact_root(self) -> Path | None:
        """Best-effort rollout-memory root from normalized/<host>/<id>.events.jsonl."""
        if self.normalized_path is None:
            return None
        try:
            return self.normalized_path.resolve().parents[2]
        except IndexError:
            return None


# ---- registries -------------------------------------------------------------

MethodFn = Callable[[MiningContext], MiningResult]
FilterFn = Callable[[MiningResult], MiningResult]


@dataclass
class MiningMethod:
    name: str
    description: str
    fn: MethodFn

    def __call__(self, ctx: MiningContext) -> MiningResult:
        return self.fn(ctx)


@dataclass
class MiningFilter:
    name: str
    description: str
    fn: FilterFn

    def __call__(self, result: MiningResult) -> MiningResult:
        return self.fn(result)


METHOD_REGISTRY: dict[str, MiningMethod] = {}
FILTER_REGISTRY: dict[str, MiningFilter] = {}


def register_method(name: str, *, description: str = "") -> Callable[[MethodFn], MiningMethod]:
    def wrap(fn: MethodFn) -> MiningMethod:
        if name in METHOD_REGISTRY:
            raise ValueError(f"mining method {name!r} already registered")
        m = MiningMethod(name=name, description=description.strip(), fn=fn)
        METHOD_REGISTRY[name] = m
        return m

    return wrap


def register_filter(name: str, *, description: str = "") -> Callable[[FilterFn], MiningFilter]:
    def wrap(fn: FilterFn) -> MiningFilter:
        if name in FILTER_REGISTRY:
            raise ValueError(f"mining filter {name!r} already registered")
        f = MiningFilter(name=name, description=description.strip(), fn=fn)
        FILTER_REGISTRY[name] = f
        return f

    return wrap


# ---- helpers methods reuse --------------------------------------------------


def event_text(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    for key in ("text", "message", "thinking"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raw = payload.get("raw_content")
    if raw is not None:
        return json.dumps(raw, ensure_ascii=False)
    return ev.summary or ""


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def refs(events: Iterable[NormalizedEvent], *, limit: int = 5) -> list[str]:
    return [ev.event_id for ev in list(events)[:limit]]


def memory_id(session_id: str, method: str, index: int) -> str:
    return f"{session_id}:{method}:{index}"


def iter_messages(events: Sequence[NormalizedEvent], actor: str | None = None):
    for ev in events:
        if ev.event_type != "message":
            continue
        if actor is None or ev.actor == actor:
            yield ev


def file_edit_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    return [ev for ev in events if ev.event_type == "file_edit"]


def tool_call_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    return [
        ev
        for ev in events
        if ev.event_type in {"tool_call", "command", "file_edit", "file_read"}
        and ev.actor == "assistant"
    ]


def tool_result_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    return [ev for ev in events if ev.actor == "tool"]
