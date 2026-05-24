"""memp_procedural: distill rollouts into ordered procedures.

Inspired by Memp (https://arxiv.org/abs/2508.06433). A "procedure" is a
higher-level script-like memory: a goal, preconditions, ordered steps,
warnings learned during the session, and an outcome description.

The distinction from `skill_pro`:
  - skill_pro extracts *reusable* sub-workflows (often per turn) with
    explicit activation/termination/verification.
  - memp_procedural rolls the *whole session* into one procedure, focused on
    the user's overall goal and the trajectory that ended it.

Heuristic, no LLM call. Procedure quality depends on session shape.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

from ...schema import NormalizedEvent
from ..base import (
    MemoryCandidate,
    MiningContext,
    MiningResult,
    event_text,
    file_edit_events,
    memory_id,
    register_method,
    tool_call_events,
    truncate,
)

_WARN_PATTERNS = (
    re.compile(r"\b(?:don'?t|do not) (?:do|use|run|add|delete|remove)[^.\n]{0,80}", re.IGNORECASE),
    re.compile(r"\b(?:avoid|never|skip|always) [^.\n]{3,80}", re.IGNORECASE),
    re.compile(r"\bwarn(?:ing)?: [^.\n]{3,80}", re.IGNORECASE),
)
_COMPLETION_HINTS = (
    "done",
    "completed",
    "shipped",
    "wrote",
    "summary",
    "result",
    "task complete",
)


@register_method(
    "memp_procedural",
    description=(
        "Distill the whole rollout into one procedural memory: goal, "
        "preconditions, ordered steps, warnings, and outcome."
    ),
)
def mine_memp_procedural(ctx: MiningContext) -> MiningResult:
    method = "memp_procedural"
    events = ctx.events
    origin = ctx.origin_repo()
    goal = _infer_goal(events)
    preconditions = _infer_preconditions(events)
    steps = _abstract_phases(events)
    warnings = _extract_warnings(events)
    outcome = _infer_outcome(events)

    candidates: list[MemoryCandidate] = []
    if not steps:
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, 1),
                method=method,
                kind="procedure",
                text=(
                    "This rollout did not contain a coherent tool sequence to abstract "
                    "into a procedure. Inspect the raw transcript before reusing it."
                ),
                when_to_use="Skip; insufficient evidence.",
                evidence_refs=[events[0].event_id],
                confidence=0.2,
                priority=1,
                risk="medium",
                scope="task",
                scope_reason="fallback when no procedure was extracted",
                origin_repo=origin,
            )
        )
    else:
        # Confidence rises with evidence richness.
        confidence = min(0.85, 0.45 + 0.05 * len(steps) + 0.05 * len(file_edit_events(events)))
        structured = {
            "goal": goal,
            "preconditions": preconditions,
            "steps": steps,
            "warnings": warnings,
            "outcome": outcome,
        }
        text = f"Procedure for: {goal}" if goal else "Procedure for this rollout's overall task"
        scope, scope_reason = _infer_procedure_scope(events, origin)
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, 1),
                method=method,
                kind="procedure",
                text=text,
                when_to_use=(
                    f"Use when the user's request resembles: {truncate(goal, 160)}"
                    if goal
                    else "Use when starting a task with a similar overall shape."
                ),
                evidence_refs=_procedure_evidence(events),
                confidence=round(confidence, 2),
                priority=4 if len(steps) >= 3 else 2,
                risk="medium",
                scope=scope,
                scope_reason=scope_reason,
                origin_repo=origin,
                structured=structured,
            )
        )

    return MiningResult(
        session_id=ctx.session_id,
        host=ctx.host,
        method=method,
        task_summary=ctx.task_summary(),
        candidates=candidates,
        notes=[
            f"Inferred {len(steps)} phase(s) across {len(tool_call_events(events))} tool call(s).",
            "Whole-session procedural abstraction; heuristic, no LLM call.",
        ],
    )


# ---- goal / preconditions / outcome ----------------------------------------


def _infer_goal(events: Sequence[NormalizedEvent]) -> str:
    """The first user message is typically the goal."""
    for ev in events:
        if ev.actor == "user" and ev.event_type == "message":
            text = event_text(ev).strip().replace("\n", " ")
            if text:
                return truncate(text, 200)
    return ""


def _infer_preconditions(events: Sequence[NormalizedEvent]) -> list[str]:
    """Surface signals about the world state that mattered at the start.

    Today: just record the host, the cwd if known, and whether a git repo was
    in play. Cheap, evidence-anchored.
    """
    out: list[str] = []
    if not events:
        return out
    out.append(f"host={events[0].host}")
    for ev in events:
        payload = ev.payload or {}
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            out.append(f"cwd={cwd}")
            break
    # Was a git branch recorded anywhere?
    for ev in events:
        payload = ev.payload or {}
        for key in ("gitBranch", "git_branch", "branch"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                out.append(f"git_branch={v}")
                return out
    return out


def _abstract_phases(events: Sequence[NormalizedEvent], *, limit: int = 12) -> list[str]:
    """Coalesce consecutive tool calls of the same kind into one phase line.

    A rollout of `Bash, Bash, Bash, Edit, Edit, Read` becomes:
        "ran 3 shell commands", "edited 2 files", "read 1 file"

    This produces a readable script-like procedure that a future agent can
    follow without committing to exact tool invocations.
    """
    out: list[str] = []
    last_kind: str | None = None
    run_count = 0

    def flush() -> None:
        nonlocal run_count, last_kind
        if last_kind and run_count:
            out.append(_phase_phrase(last_kind, run_count))
        last_kind = None
        run_count = 0

    for ev in events:
        kind = _classify_phase(ev)
        if kind is None:
            continue
        if kind == last_kind:
            run_count += 1
        else:
            flush()
            last_kind = kind
            run_count = 1
        if len(out) >= limit:
            break
    flush()
    return out[:limit]


def _classify_phase(ev: NormalizedEvent) -> str | None:
    if ev.actor != "assistant":
        return None
    et = ev.event_type
    payload = ev.payload or {}
    name = (payload.get("name") or payload.get("tool_name") or "").lower()
    if et == "command":
        return "shell"
    if et == "file_edit":
        return "edit"
    if et == "file_read":
        return "read"
    if et == "tool_call":
        if "search" in name or "fetch" in name or "web" in name:
            return "research"
        if "task" in name or "agent" in name:
            return "delegate"
        return "tool"
    return None


def _phase_phrase(kind: str, count: int) -> str:
    plural = "s" if count != 1 else ""
    return {
        "shell": f"ran {count} shell command{plural}",
        "edit": f"edited {count} file{plural}",
        "read": f"read {count} file{plural}",
        "research": f"performed {count} web research call{plural}",
        "delegate": f"delegated {count} sub-task{plural}",
        "tool": f"made {count} tool call{plural}",
    }.get(kind, f"{count} {kind} step{plural}")


def _extract_warnings(events: Sequence[NormalizedEvent]) -> list[str]:
    """Pull "don't / avoid / never" advice from user + assistant messages."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for ev in events:
        if ev.event_type != "message":
            continue
        text = event_text(ev)
        for pat in _WARN_PATTERNS:
            for match in pat.findall(text):
                snippet = match if isinstance(match, str) else " ".join(match)
                snippet = snippet.strip().rstrip(".").strip()
                if 4 < len(snippet) < 140 and snippet.lower() not in seen_lower:
                    seen.append(truncate(snippet, 140))
                    seen_lower.add(snippet.lower())
                    if len(seen) >= 5:
                        return seen
    return seen


def _infer_outcome(events: Sequence[NormalizedEvent]) -> str:
    """Find an outcome-shaped statement near the end of the session.

    Walk from the back through assistant messages and pick the first one that
    looks like a conclusion (mentions "done", "summary", "result", "wrote",
    "completed", or is the very last assistant message).
    """
    last_assistant: NormalizedEvent | None = None
    for ev in reversed(events):
        if ev.actor == "assistant" and ev.event_type == "message":
            last_assistant = last_assistant or ev
            text = event_text(ev).lower()
            if any(h in text for h in _COMPLETION_HINTS):
                return truncate(event_text(ev).strip().replace("\n", " "), 220)
    if last_assistant is not None:
        return truncate(event_text(last_assistant).strip().replace("\n", " "), 220)
    return ""


def _infer_procedure_scope(events: Sequence[NormalizedEvent], origin: str | None) -> tuple[str, str]:
    """Procedures usually reuse repo conventions, so default `repo`.

    Treat as `task` when:
      - the session made no file edits in the cwd (it was exploratory /
        research-shaped),
      - or no origin repo is known.
    """
    if not origin:
        return "task", "no origin repo known; treating as task-shaped procedure"
    edits = file_edit_events(events)
    local = 0
    for ev in edits:
        payload = ev.payload or {}
        inp = payload.get("input") or {}
        if isinstance(inp, dict):
            for key in ("file_path", "path", "file"):
                v = inp.get(key)
                if isinstance(v, str) and origin in v:
                    local += 1
                    break
        changes = payload.get("changes")
        if isinstance(changes, dict):
            if any(isinstance(k, str) and origin in k for k in changes.keys()):
                local += 1
    if local == 0:
        return "task", "session has no file edits inside the origin repo"
    return "repo", f"procedure edits {local} file(s) inside {origin}"


def _procedure_evidence(events: Sequence[NormalizedEvent]) -> list[str]:
    """Pick a small, diverse set of event_ids backing the candidate."""
    out: list[str] = []
    counts: Counter[str] = Counter()
    for ev in events:
        bucket = ev.event_type
        if counts[bucket] >= 2:
            continue
        if ev.event_type in {"message", "command", "file_edit", "tool_call"}:
            out.append(ev.event_id)
            counts[bucket] += 1
        if len(out) >= 6:
            break
    return out
