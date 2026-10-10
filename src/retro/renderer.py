"""Render normalized events to a human-readable markdown transcript.

This is a *view*. The source of truth is `raw/` + `normalized/*.events.jsonl`.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .schema import NormalizedEvent, read_events
from .utils import atomic_write_text, event_text, event_tool_input, event_tool_output

_COMMAND_OUT_LIMIT = 4000  # per-event content truncation in rendered md
_TEXT_LIMIT = 8000
_NOISE_TYPES = {"lifecycle", "metadata", "usage"}
_LEGACY_NOISE_PREFIXES = (
    "assistant.turn_",
    "hook.start",
    "hook.end",
    "session resume",
    "session.mode_changed",
    "session.model_change",
    "session.task_complete",
    "session.usage_checkpoint",
    "subagent.deselected",
    "task_started",
    "task_complete",
    "token_count",
    "turn_context",
)
_CALL_TYPES = {"tool_call", "command", "file_edit", "file_read", "subagent_start"}
_RESULT_TYPES = {"tool_result", "subagent_end"}

_ACTOR_HEADINGS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "system": "System",
    "subagent": "Sub-agent",
    "hook": "Hook",
}


def render_markdown(
    events: Iterable[NormalizedEvent],
    *,
    include_noise: bool = False,
) -> str:
    events = list(events)
    if not events:
        return "# (empty rollout)\n"

    visible = [
        event
        for event in events
        if include_noise or not _is_noise(event)
    ]
    first = events[0]
    lines: list[str] = []
    lines.append(f"# Rollout: {first.session_id}")
    lines.append("")
    lines.append(f"- **Host:** `{first.host}`")
    lines.append(f"- **Events:** {len(events)}")
    if len(visible) != len(events):
        lines.append(f"- **Transcript events:** {len(visible)}")
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

    inline_results = _inline_results(visible)
    consumed_results = {
        result.event_id
        for results in inline_results.values()
        for result in results
    }
    for ev in visible:
        if ev.event_id in consumed_results:
            continue
        lines.extend(_render_event(ev))
        for result in inline_results.get(ev.event_id, []):
            lines.extend(_render_result_inline(result))

    return "\n".join(lines) + "\n"


def render_file(
    normalized_path: Path,
    dest: Path,
    *,
    include_noise: bool = False,
) -> int:
    md = render_markdown(read_events(normalized_path), include_noise=include_noise)
    atomic_write_text(dest, md)
    return len(md)


def _is_noise(ev: NormalizedEvent) -> bool:
    if ev.event_type in _NOISE_TYPES:
        return True
    if ev.event_type != "attachment":
        return False
    summary = ev.summary.casefold()
    return any(summary.startswith(prefix) for prefix in _LEGACY_NOISE_PREFIXES)


def _is_call(ev: NormalizedEvent) -> bool:
    return ev.actor in {"assistant", "subagent"} and ev.event_type in _CALL_TYPES


def _is_result(ev: NormalizedEvent) -> bool:
    return ev.event_type in _RESULT_TYPES or (
        ev.actor == "tool" and ev.event_type in {"command", "file_edit", "file_read"}
    )


def _inline_results(
    events: list[NormalizedEvent],
) -> dict[str, list[NormalizedEvent]]:
    calls = [event for event in events if _is_call(event)]
    call_ids: dict[str, list[NormalizedEvent]] = {}
    calls_by_event_id = {event.event_id: event for event in calls}
    for call_event in calls:
        call_id = call_event.payload.get("call_id")
        if call_id:
            call_ids.setdefault(str(call_id), []).append(call_event)

    paired: dict[str, list[NormalizedEvent]] = {}
    for result in (event for event in events if _is_result(event)):
        call: NormalizedEvent | None = None
        if result.parent_event_id:
            call = calls_by_event_id.get(result.parent_event_id)
        if call is None and result.payload.get("call_id"):
            candidates = call_ids.get(str(result.payload["call_id"]), [])
            if candidates:
                call = candidates[-1]
        if call is not None:
            paired.setdefault(call.event_id, []).append(result)
    return paired


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
    elif et in {"tool_call", "command", "file_edit", "file_read", "subagent_start"}:
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
    elif et in _NOISE_TYPES:
        out.append(f"## · {et} · {ev.summary}{ts}")
        out.append("<details><summary>payload</summary>")
        out.append("")
        out.append("```json")
        out.append(_short_json(ev.payload, indent=2, limit=2000))
        out.append("```")
        out.append("")
        out.append("</details>")
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


def _render_result_inline(ev: NormalizedEvent) -> list[str]:
    status: list[str] = []
    if isinstance(ev.payload.get("success"), bool):
        status.append("success" if ev.payload["success"] else "failed")
    if ev.payload.get("exit_code") is not None:
        status.append(f"exit {ev.payload['exit_code']}")
    label = f" — {', '.join(status)}" if status else ""
    out = [f"### Result{label}"]
    text = _extract_tool_result_text(ev)
    if text:
        out.extend(
            [
                "```",
                _truncate_keep_lines(text, _COMMAND_OUT_LIMIT),
                "```",
            ]
        )
    else:
        out.extend(
            [
                "<details><summary>payload</summary>",
                "",
                "```json",
                _short_json(ev.payload, indent=2, limit=2000),
                "```",
                "",
                "</details>",
            ]
        )
    out.append("")
    return out


def _extract_text(ev: NormalizedEvent) -> str:
    return event_text(ev)


def _extract_tool_result_text(ev: NormalizedEvent) -> str:
    output = event_tool_output(ev)
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False, indent=2)
    if isinstance(output, list):
        # Anthropic-style tool_result content blocks: [{type: "text", text: ...}, ...]
        content_chunks: list[str] = []
        for c in output:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    content_chunks.append(t)
                else:
                    content_chunks.append(json.dumps(c, ensure_ascii=False))
            else:
                content_chunks.append(str(c))
        return "\n".join(content_chunks)
    return ""


def _tool_call_body(ev: NormalizedEvent) -> str:
    p = ev.payload or {}
    parts: list[str] = []
    name = p.get("name") or p.get("tool_name")
    if name:
        parts.append(f"- **name:** `{name}`")
    inp = event_tool_input(ev)
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
        s = json.dumps(
            _bounded_json(value, [max(limit * 2, 1024)], depth=0),
            ensure_ascii=False,
            indent=indent,
        )
    except (TypeError, ValueError):
        s = repr(value)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _bounded_json(value, remaining: list[int], *, depth: int):
    if remaining[0] <= 0:
        return "... [truncated]"
    if depth >= 8:
        return "... [max depth]"
    if isinstance(value, str):
        if len(value) <= remaining[0]:
            remaining[0] -= len(value)
            return value
        keep = max(0, remaining[0])
        remaining[0] = 0
        return value[:keep] + "... [truncated]"
    if isinstance(value, dict):
        dict_out = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 100 or remaining[0] <= 0:
                dict_out["..."] = "[truncated]"
                break
            remaining[0] -= min(len(str(key)), remaining[0])
            dict_out[key] = _bounded_json(child, remaining, depth=depth + 1)
        return dict_out
    if isinstance(value, (list, tuple)):
        list_out = []
        for index, child in enumerate(value):
            if index >= 100 or remaining[0] <= 0:
                list_out.append("... [truncated]")
                break
            list_out.append(_bounded_json(child, remaining, depth=depth + 1))
        return list_out
    remaining[0] -= min(len(repr(value)), remaining[0])
    return value


def _truncate_keep_lines(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\n… [truncated]"
