from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from retro.sdk import Actor, EventType, NormalizedEvent, RawRef

_TOOL_EVENT_TYPES: dict[str, EventType] = {
    "read": "file_read",
    "view": "file_read",
    "edit": "file_edit",
    "write": "file_edit",
    "multiedit": "file_edit",
    "patch": "file_edit",
    "bash": "command",
    "shell": "command",
    "exec": "command",
    "run": "command",
}
_DIRECT_EVENT_TYPES: dict[str, EventType] = {
    "file_read": "file_read",
    "file_edit": "file_edit",
    "command": "command",
    "reasoning": "reasoning",
    "permission": "permission",
    "attachment": "attachment",
    "session_start": "session_start",
    "session_end": "session_end",
}


def iter_source_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_no, {
                    "_retro_error": "malformed_json",
                    "raw_line": line,
                    "error": str(exc),
                }
                continue
            if not isinstance(value, dict):
                yield line_no, {
                    "_retro_error": "non_object_json",
                    "value": value,
                }
                continue
            yield line_no, value


class OpenCodeNormalizer:
    host = "opencode"

    def normalize(self, *, raw_dir: Path, session_id: str) -> Iterable[NormalizedEvent]:
        transcript = raw_dir / "session.jsonl"
        raw_ref_path = f"raw/{self.host}/{session_id}/session.jsonl"
        call_names: dict[str, str] = {}
        sequence = 0
        for line_no, raw in iter_source_records(transcript):
            for event in self._normalize_record(
                raw=raw,
                session_id=session_id,
                raw_ref_path=raw_ref_path,
                line_no=line_no,
                call_names=call_names,
            ):
                sequence += 1
                event.sequence = sequence
                yield event

    def _normalize_record(
        self,
        *,
        raw: dict[str, Any],
        session_id: str,
        raw_ref_path: str,
        line_no: int,
        call_names: dict[str, str],
    ) -> list[NormalizedEvent]:
        source_type = _source_type(raw)
        common: dict[str, Any] = {
            "event_id": str(raw.get("id") or raw.get("uuid") or f"{session_id}:{line_no}"),
            "session_id": session_id,
            "host": self.host,
            "sequence": 0,
            "timestamp": _timestamp(raw.get("timestamp")),
            "parent_event_id": _optional_text(raw.get("parent_id") or raw.get("parentId")),
            "raw_ref": RawRef(path=raw_ref_path, line=line_no),
        }
        if raw.get("_retro_error"):
            return [
                NormalizedEvent(
                    actor="system",
                    event_type="unknown",
                    summary=str(raw["_retro_error"]),
                    payload=raw,
                    **common,
                )
            ]
        if source_type in {"message", "user", "assistant", "system"}:
            return [self._message(raw, source_type, common)]
        if source_type in {"tool_call", "tool_use"}:
            return [self._tool_call(raw, common, call_names)]
        if source_type in {"tool_result", "tool_output"}:
            return [self._tool_result(raw, common, call_names)]
        if source_type in _DIRECT_EVENT_TYPES:
            return [self._direct(raw, _DIRECT_EVENT_TYPES[source_type], common)]
        return [
            NormalizedEvent(
                actor="system",
                event_type="unknown",
                summary=f"unknown type={source_type or raw.get('type')}",
                payload=raw,
                **common,
            )
        ]

    def _message(
        self,
        raw: dict[str, Any],
        source_type: str,
        common: dict[str, Any],
    ) -> NormalizedEvent:
        role = raw.get("role") or raw.get("actor") or source_type
        actor: Actor
        if role == "user":
            actor = "user"
        elif role == "assistant":
            actor = "assistant"
        elif role == "tool":
            actor = "tool"
        else:
            actor = "system"
        event_type: EventType = "tool_result" if actor == "tool" else "message"
        text = _coerce_text(raw.get("content") or raw.get("message") or raw.get("text"))
        return NormalizedEvent(
            actor=actor,
            event_type=event_type,
            summary=(text or str(raw.get("summary") or event_type))[:200],
            payload={"text": text, "record": raw},
            **common,
        )

    def _tool_call(
        self,
        raw: dict[str, Any],
        common: dict[str, Any],
        call_names: dict[str, str],
    ) -> NormalizedEvent:
        name = str(raw.get("name") or raw.get("tool") or raw.get("tool_name") or "?")
        call_id = raw.get("call_id") or raw.get("tool_call_id") or raw.get("id")
        if call_id is not None:
            call_names[str(call_id)] = name
        arguments = raw.get("input") if "input" in raw else raw.get("arguments", {})
        normalized_name = name.lower().replace("_", "").replace("-", "")
        event_type: EventType = _TOOL_EVENT_TYPES.get(normalized_name, "tool_call")
        return NormalizedEvent(
            actor="assistant",
            event_type=event_type,
            summary=f"{name}({_summarize(arguments)})",
            payload={
                "name": name,
                "call_id": call_id,
                "input": arguments,
                "record": raw,
            },
            **common,
        )

    def _tool_result(
        self,
        raw: dict[str, Any],
        common: dict[str, Any],
        call_names: dict[str, str],
    ) -> NormalizedEvent:
        call_id = raw.get("call_id") or raw.get("tool_call_id")
        name = str(
            raw.get("name")
            or raw.get("tool")
            or call_names.get(str(call_id), "?")
        )
        return NormalizedEvent(
            actor="tool",
            event_type="tool_result",
            summary=f"tool_result: {name}",
            payload={
                "name": name,
                "call_id": call_id,
                "output": raw.get("output"),
                "record": raw,
            },
            **common,
        )

    def _direct(
        self,
        raw: dict[str, Any],
        source_type: EventType,
        common: dict[str, Any],
    ) -> NormalizedEvent:
        actor: Actor = "assistant" if source_type == "reasoning" else "system"
        if source_type in {"file_read", "file_edit", "command"}:
            actor = "tool" if raw.get("result") or raw.get("output") else "assistant"
        summary = raw.get("summary") or raw.get("text") or raw.get("command") or source_type
        return NormalizedEvent(
            actor=actor,
            event_type=source_type,
            summary=str(summary)[:200],
            payload=raw,
            **common,
        )


def _source_type(raw: dict[str, Any]) -> str:
    source = raw.get("type") or raw.get("event") or raw.get("kind")
    if not isinstance(source, str):
        return ""
    return source.strip().lower().replace("-", "_")


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    values.append(text)
            elif item is not None:
                values.append(str(item))
        return "\n".join(values)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return "" if value is None else str(value)


def _summarize(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("path", "file", "file_path", "command", "cmd", "pattern", "url", "query"):
            if key in value:
                return f"{key}={str(value[key])[:80]}"
        return json.dumps(value, ensure_ascii=False)[:80]
    return str(value)[:80]


def _timestamp(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
