"""ReMe-style "Remember Me, Refine Me" POC.

Original implementation lived in mining.py. Behavior preserved verbatim; the
only change is the wrapping into a `@register_method` plugin so it composes
with skill_pro / memp_procedural / risk_aware in the new mining package.

Paper: https://arxiv.org/abs/2512.10696
"""
from __future__ import annotations

from collections.abc import Sequence

from ...schema import NormalizedEvent
from ..base import (
    MemoryCandidate,
    MiningContext,
    MiningResult,
    event_text,
    file_edit_events,
    memory_id,
    refs,
    register_method,
)


@register_method(
    "reme_refine_poc",
    description=(
        "Deterministic ReMe-style distillation: success patterns, failure "
        "triggers, tool lessons, risk rules. No LLM call."
    ),
)
def mine_reme_style(ctx: MiningContext) -> MiningResult:
    events = ctx.events
    method = "reme_refine_poc"
    candidates: list[MemoryCandidate] = []
    task_summary = ctx.task_summary()
    origin = ctx.origin_repo()

    if _correction_events(events):
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, len(candidates) + 1),
                method=method,
                kind="failure_trigger",
                text=(
                    "When the user challenges a research or tool claim, verify the raw "
                    "evidence and revise the artifact rather than defending the earlier summary."
                ),
                when_to_use="Use when a user asks whether a previous conclusion is actually supported.",
                evidence_refs=refs(_correction_events(events)),
                confidence=0.82,
                priority=5,
                risk="low",
                scope="user",
                scope_reason="assistant-behavior pattern; applies across all repos",
                origin_repo=origin,
            )
        )

    if _has_docs_file_added(events):
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, len(candidates) + 1),
                method=method,
                kind="procedure",
                text=(
                    "For project-planning or research requests, create durable docs "
                    "artifacts under docs/ or specs/, then report the exact paths."
                ),
                when_to_use=(
                    "Use when the user asks for a spec, project purpose, "
                    "research note, or landscape scan."
                ),
                evidence_refs=refs(file_edit_events(events)),
                confidence=0.78,
                priority=4,
                risk="medium",
                scope="user",
                scope_reason="working-style preference for durable docs/specs artifacts",
                origin_repo=origin,
            )
        )

    if _uses_web_research(events):
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, len(candidates) + 1),
                method=method,
                kind="tool_lesson",
                text=(
                    "Distinguish current, directly verified sources from broad search "
                    "results. If the topic is fast-moving, prefer recent primary sources "
                    "and label uncertainty."
                ),
                when_to_use="Use when researching recent tools, papers, APIs, or agent-memory claims.",
                evidence_refs=refs(_web_events(events)),
                confidence=0.7,
                priority=4,
                risk="medium",
                scope="user",
                scope_reason="cross-repo tool-use lesson",
                origin_repo=origin,
            )
        )

    if _has_capture_distinction(events):
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, len(candidates) + 1),
                method=method,
                kind="risk_rule",
                text=(
                    "Do not equate persistent memory with full rollout capture. Check "
                    "whether a system stores raw prompts, assistant messages, tool calls, "
                    "tool results, file edits, and replayable event traces."
                ),
                when_to_use="Use when evaluating memory systems for Codex, Claude Code, or coding agents.",
                evidence_refs=refs(_message_events_containing(events, ["full rollout", "raw", "capture"])),
                confidence=0.86,
                priority=5,
                risk="low",
                scope="global",
                scope_reason="foundational rule about evaluating memory/capture systems",
                origin_repo=origin,
            )
        )

    if not candidates:
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, 1),
                method=method,
                kind="procedure",
                text=(
                    "Review the captured rollout before starting a similar task, but do "
                    "not inject specific advice unless there is clear evidence from user "
                    "corrections or successful outcomes."
                ),
                when_to_use="Use when no high-confidence pattern was mined from the rollout.",
                evidence_refs=[events[0].event_id],
                confidence=0.45,
                priority=1,
                risk="medium",
                scope="task",
                scope_reason="fallback when nothing concrete was extracted",
                origin_repo=origin,
            )
        )

    return MiningResult(
        session_id=ctx.session_id,
        host=ctx.host,
        method=method,
        task_summary=task_summary,
        candidates=candidates,
        notes=[
            "POC deterministic miner inspired by ReMe multi-faceted distillation.",
            "No model call was used; candidates are heuristics over normalized events.",
        ],
    )


# ---- detection helpers (kept local) ----------------------------------------


def _correction_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    needles = (
        "you are right",
        "you're right",
        "good catch",
        "fair",
        "overstated",
        "not actually",
        "doesn't",
        "does not",
        "none of these",
        "is this actually",
    )
    return [
        ev
        for ev in events
        if ev.actor == "user" and any(n in event_text(ev).lower() for n in needles)
    ]


def _has_docs_file_added(events: Sequence[NormalizedEvent]) -> bool:
    for ev in file_edit_events(events):
        payload = ev.payload or {}
        changes = payload.get("changes")
        if isinstance(changes, dict):
            for path, change in changes.items():
                if "/docs/" in path or "/specs/" in path:
                    if isinstance(change, dict) and change.get("type") in {"add", "update"}:
                        return True
    return False


def _uses_web_research(events: Sequence[NormalizedEvent]) -> bool:
    return bool(_web_events(events))


def _web_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    return [
        ev
        for ev in events
        if "web_search" in (ev.summary or "") or "search" in (ev.summary or "").lower()
    ]


def _has_capture_distinction(events: Sequence[NormalizedEvent]) -> bool:
    text = "\n".join(event_text(ev).lower() for ev in events if ev.event_type == "message")
    return "full rollout" in text and ("memory" in text or "capture" in text)


def _message_events_containing(
    events: Sequence[NormalizedEvent], needles: Sequence[str]
) -> list[NormalizedEvent]:
    lowered = [n.lower() for n in needles]
    return [
        ev
        for ev in events
        if ev.event_type == "message" and any(n in event_text(ev).lower() for n in lowered)
    ]
