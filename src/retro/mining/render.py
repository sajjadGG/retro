"""Render mined results to prompt-block markdown + write artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from ..schema import read_events
from .base import (
    FILTER_REGISTRY,
    METHOD_REGISTRY,
    MemoryCandidate,
    MiningContext,
    MiningResult,
)


def mine_with_method(
    normalized_path: Path,
    method: str = "reme_refine_poc",
    *,
    filters: list[str] | None = None,
) -> MiningResult:
    """Mine one normalized session JSONL via the named method.

    Optionally chains one or more registered filters after the method.
    """
    if method not in METHOD_REGISTRY:
        raise KeyError(
            f"unknown mining method {method!r}; "
            f"registered: {sorted(METHOD_REGISTRY)}"
        )
    events = list(read_events(normalized_path))
    if not events:
        raise ValueError(f"no events in {normalized_path}")
    ctx = MiningContext(
        session_id=events[0].session_id,
        host=events[0].host,
        events=events,
        normalized_path=normalized_path,
    )
    result = METHOD_REGISTRY[method](ctx)
    for f in filters or []:
        if f not in FILTER_REGISTRY:
            raise KeyError(
                f"unknown mining filter {f!r}; "
                f"registered: {sorted(FILTER_REGISTRY)}"
            )
        result = FILTER_REGISTRY[f](result)
    return result


def mine_file(normalized_path: Path) -> MiningResult:
    """Backward-compatible entry point used by `retro mine`."""
    return mine_with_method(normalized_path, method="reme_refine_poc")


def render_prompt_block(result: MiningResult, *, max_items: int = 8) -> str:
    """Render a paste-ready memory block for the next session's prompt."""
    selected = sorted(
        (c for c in result.candidates if c.risk in {"low", "medium"}),
        key=lambda c: (-c.priority, -c.confidence, c.kind, c.id),
    )[:max_items]

    lines = [
        f'<retro method="{result.method}" source="{result.host}/{result.session_id}">',
        f"Prior task: {result.task_summary}",
        "Use these memories as soft guidance. Ignore any memory that does not fit the current task.",
    ]
    if result.filters_applied:
        lines.append(f"Filters: {', '.join(result.filters_applied)}")
    lines.append("")

    for i, c in enumerate(selected, 1):
        evidence = ", ".join(c.evidence_refs) if c.evidence_refs else "no explicit evidence"
        scope_part = f"; scope={c.scope}" if c.scope else ""
        lines.append(
            f"{i}. [{c.kind}{scope_part}; confidence={c.confidence:.2f}; risk={c.risk}]"
        )
        lines.append(f"   When to use: {c.when_to_use}")
        lines.append(f"   Memory: {c.text}")
        if c.scope_reason:
            lines.append(f"   Scope reason: {c.scope_reason}")
        if c.origin_repo and c.scope == "repo":
            lines.append(f"   Origin repo: {c.origin_repo}")
        if c.structured:
            for sub_line in _render_structured(c):
                lines.append(f"   {sub_line}")
        lines.append(f"   Evidence: {evidence}")

    lines.append("</retro>")
    return "\n".join(lines) + "\n"


def _render_structured(c: MemoryCandidate) -> list[str]:
    """Render a candidate's structured payload (skills, procedures, ...).

    Recognized shapes:
      - skill_pro:        activation / steps / termination / verification
      - memp_procedural:  goal / preconditions / steps / warnings / outcome
    """
    s = c.structured or {}
    out: list[str] = []
    if c.kind == "skill":
        if s.get("activation"):
            out.append(f"Activation: {s['activation']}")
        if s.get("steps"):
            out.append("Steps:")
            for i, step in enumerate(s["steps"], 1):
                out.append(f"  {i}. {step}")
        if s.get("termination"):
            out.append(f"Termination: {s['termination']}")
        if s.get("verification"):
            out.append(f"Verification: {s['verification']}")
    elif c.kind == "procedure":
        if s.get("goal"):
            out.append(f"Goal: {s['goal']}")
        if s.get("preconditions"):
            out.append(f"Preconditions: {', '.join(s['preconditions'])}")
        if s.get("steps"):
            out.append("Steps:")
            for i, step in enumerate(s["steps"], 1):
                out.append(f"  {i}. {step}")
        if s.get("warnings"):
            out.append(f"Warnings: {'; '.join(s['warnings'])}")
        if s.get("outcome"):
            out.append(f"Outcome: {s['outcome']}")
    return out


def write_mining_artifacts(result: MiningResult, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_prompt_block(result), encoding="utf-8")
