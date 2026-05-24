"""Render normalized events to a human-readable markdown transcript.

This is a *view*. The source of truth is `raw/` + `normalized/*.events.jsonl`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .schema import NormalizedEvent, read_events

_COMMAND_OUT_LIMIT = 4000  # per-event content truncation in rendered md
_TEXT_LIMIT = 8000

_ACTOR_HEADINGS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "system": "System",
    "subagent": "Sub-agent",
    "hook": "Hook",
}


def render_markdown(events: Iterable[NormalizedEvent]) -> str:
    events = list(events)
    if not events:
        return "# (empty rollout)\n"

    first = events[0]
    lines: list[str] = []
    lines.append(f"# Rollout: {first.session_id}")
    lines.append("")
    lines.append(f"- **Host:** `{first.host}`")
    lines.append(f"- **Events:** {len(events)}")
    if first.timestamp:
        lines.append(f"- **First event:** {first.timestamp}")
    last_ts = next((e.timestamp for e in reversed(events) if e.timestamp), None)
    if last_ts:
        lines.append(f"- **Last event:** {last_ts}")
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type] = counts.get(e.event_type, 0) + 1
    lines.append("- **Event type counts:** " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append("")
    lines.append("---")
    lines.append("")

    for ev in events:
        lines.extend(_render_event(ev))

    return "\n".join(lines) + "\n"


def render_file(normalized_path: Path, dest: Path) -> int:
    md = render_markdown(read_events(normalized_path))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    return len(md)


def _render_event(ev: NormalizedEvent) -> list[str]:
    heading_actor = _ACTOR_HEADINGS.get(ev.actor, ev.actor)
    ts = f"  _{ev.timestamp}_" if ev.timestamp else ""
    out: list[str] = []
    et = ev.event_type

    if et == "session_start":
        out.append(f"## · session_start · {heading_actor}{ts}")
        out.append("```json")
        out.append(_short_json(ev.payload))
        out.append("```")
    elif et == "session_end":
        out.append(f"## · session_end · {heading_actor}{ts}")
        out.append("```json")
        out.append(_short_json(ev.payload))
        out.append("```")
    elif et == "message":
        text = _extract_text(ev)
        out.append(f"## {heading_actor}{ts}")
        out.append(_block_quote(_truncate_keep_lines(text, _TEXT_LIMIT)))
    elif et == "reasoning":
        text = _extract_text(ev) or "(reasoning, encrypted or empty)"
        out.append(f"## {heading_actor} (reasoning){ts}")
        out.append("<details><summary>show</summary>")
        out.append("")
        out.append(_block_quote(_truncate_keep_lines(text, _TEXT_LIMIT)))
        out.append("")
        out.append("</details>")
    elif et in {"tool_call", "command", "file_edit", "file_read"}:
        out.append(f"## · {et} · {heading_actor}{ts}")
        out.append(f"**{ev.summary}**")
        body = _tool_call_body(ev)
        if body:
            out.append("")
            out.append(body)
    elif et == "tool_result":
        out.append(f"## · tool_result · {heading_actor}{ts}")
        out.append(f"**{ev.summary}**")
        text = _extract_tool_result_text(ev)
        if text:
            out.append("")
            out.append("```")
            out.append(_truncate_keep_lines(text, _COMMAND_OUT_LIMIT))
            out.append("```")
    elif et == "attachment":
        out.append(f"## · attachment · {ev.summary}{ts}")
        out.append("<details><summary>payload</summary>")
        out.append("")
        out.append("```json")
        out.append(_short_json(ev.payload, indent=2, limit=2000))
        out.append("```")
        out.append("")
        out.append("</details>")
    elif et == "error":
        out.append(f"## · ERROR ·{ts}")
        out.append("```")
        out.append(_short_json(ev.payload, limit=2000))
        out.append("```")
    elif et == "unknown":
        out.append(f"## · unknown · {ev.summary}{ts}")
        out.append("<details><summary>payload</summary>")
        out.append("")
        out.append("```json")
        out.append(_short_json(ev.payload, indent=2, limit=2000))
        out.append("```")
        out.append("")
        out.append("</details>")
    else:
        out.append(f"## · {et} · {heading_actor}{ts}")
        out.append(f"_{ev.summary}_")

    out.append("")
    return out


def _extract_text(ev: NormalizedEvent) -> str:
    p = ev.payload or {}
    for k in ("text", "thinking", "message"):
        v = p.get(k)
        if isinstance(v, str):
            return v
    raw = p.get("raw_content")
    if raw is not None:
        return json.dumps(raw, ensure_ascii=False)
    return ev.summary


def _extract_tool_result_text(ev: NormalizedEvent) -> str:
    p = ev.payload or {}
    out = p.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, (dict, list)):
        return json.dumps(out, ensure_ascii=False, indent=2)
    content = p.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic-style tool_result content blocks: [{type: "text", text: ...}, ...]
        chunks: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    chunks.append(t)
                else:
                    chunks.append(json.dumps(c, ensure_ascii=False))
            else:
                chunks.append(str(c))
        return "\n".join(chunks)
    return ""


def _tool_call_body(ev: NormalizedEvent) -> str:
    p = ev.payload or {}
    parts: list[str] = []
    name = p.get("name") or p.get("tool_name")
    if name:
        parts.append(f"- **name:** `{name}`")
    inp = p.get("input")
    if inp is None:
        inp = p.get("arguments")
    if inp is not None:
        parts.append("- **input:**")
        parts.append("```json")
        parts.append(_short_json(inp, indent=2, limit=2000))
        parts.append("```")
    return "\n".join(parts)


def _block_quote(text: str) -> str:
    if not text:
        return "> "
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _short_json(value, indent: int | None = None, limit: int = 600) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError):
        s = repr(value)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _truncate_keep_lines(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\n… [truncated]"
