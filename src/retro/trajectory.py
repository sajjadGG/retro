"""Trajectory helpers for experimental workflow-shape signals.

The model is inspired by thought/action/result trajectory analyses, but it is
best-effort over Retro's normalized event stream: many captured sessions do not
include explicit hidden reasoning, so a step may have an empty thought.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .schema import NormalizedEvent
from .utils import event_text, truncate_summary

ActionCategory = Literal[
    "explore",
    "locate",
    "search",
    "reproduce",
    "generate_fix",
    "run_tests",
    "refactor",
    "explain",
    "permission",
    "error",
    "other",
]

CORE_CATEGORIES: tuple[ActionCategory, ...] = (
    "explore",
    "locate",
    "search",
    "reproduce",
    "generate_fix",
    "run_tests",
    "refactor",
    "explain",
)

ALL_CATEGORIES: tuple[ActionCategory, ...] = (
    *CORE_CATEGORIES,
    "permission",
    "error",
    "other",
)


@dataclass(frozen=True)
class TrajectoryStep:
    index: int
    thought_event_ids: tuple[str, ...]
    action_event_id: str
    result_event_ids: tuple[str, ...]
    thought_text: str
    action_text: str
    result_text: str
    action_category: ActionCategory
    action_fingerprint: str
    action_fingerprint_key: str


def build_trajectory(events: Sequence[NormalizedEvent]) -> list[TrajectoryStep]:
    """Infer action-centered trajectory steps from normalized events."""
    steps: list[TrajectoryStep] = []
    pending_thoughts: list[NormalizedEvent] = []
    current_action: NormalizedEvent | None = None
    current_results: list[NormalizedEvent] = []
    current_thoughts: list[NormalizedEvent] = []

    def flush() -> None:
        nonlocal current_action, current_results, current_thoughts
        if current_action is None:
            return
        action_text = _action_text(current_action)
        result_text = "\n".join(
            truncate_summary(event_text(ev) or ev.summary or "", 500) for ev in current_results
        ).strip()
        thought_text = "\n".join(
            truncate_summary(event_text(ev) or ev.summary or "", 500) for ev in current_thoughts
        ).strip()
        fingerprint = action_fingerprint(current_action, action_text)
        steps.append(
            TrajectoryStep(
                index=len(steps) + 1,
                thought_event_ids=tuple(ev.event_id for ev in current_thoughts),
                action_event_id=current_action.event_id,
                result_event_ids=tuple(ev.event_id for ev in current_results),
                thought_text=thought_text,
                action_text=action_text,
                result_text=result_text,
                action_category=categorize_action(current_action, action_text),
                action_fingerprint=fingerprint,
                action_fingerprint_key=_fingerprint_key(fingerprint),
            )
        )
        current_action = None
        current_results = []
        current_thoughts = []

    for ev in events:
        if _is_thought_candidate(ev):
            if current_action is not None:
                flush()
            pending_thoughts.append(ev)
            pending_thoughts = pending_thoughts[-3:]
            continue
        if _is_action_event(ev):
            flush()
            current_action = ev
            current_thoughts = pending_thoughts
            pending_thoughts = []
            current_results = []
            continue
        if current_action is not None and _is_result_event(ev):
            current_results.append(ev)

    flush()
    return steps


def categorize_action(ev: NormalizedEvent, text: str | None = None) -> ActionCategory:
    """Map one action event to a high-level workflow category."""
    hay = _event_haystack(ev, text)
    command = _command_text(ev)
    first = _first_command_token(command)

    if ev.event_type == "error" or _result_is_failed(ev):
        return "error"
    if ev.event_type == "permission" or "permission" in hay or "approval" in hay:
        return "permission"
    if ev.event_type == "file_edit" or _name(ev) in {"apply_patch", "edit", "write", "multiedit"}:
        if _looks_refactor(hay):
            return "refactor"
        return "generate_fix"
    if _looks_test(command) or _looks_test(hay):
        return "run_tests"
    if _looks_reproduce(hay):
        return "reproduce"
    if _looks_search(first, hay):
        return "search"
    if ev.event_type == "file_read" or _looks_explore(first, hay):
        return "explore"
    if _looks_locate(hay):
        return "locate"
    if ev.event_type == "message" and ev.actor == "assistant":
        return "explain"
    if _looks_refactor(hay):
        return "refactor"
    return "other"


def action_fingerprint(ev: NormalizedEvent, text: str | None = None) -> str:
    """Return a human-readable normalized action fingerprint."""
    payload = ev.payload or {}
    parts: list[str] = [ev.event_type, ev.actor]
    name = _name(ev)
    if name:
        parts.append(name)
    command = _command_text(ev)
    if command:
        parts.append(command)
    for key in ("file_path", "path", "filepath", "cwd"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value}")
    inp = payload.get("input")
    if isinstance(inp, dict):
        for key in ("file_path", "path", "filepath", "cmd", "command"):
            value = inp.get(key)
            if isinstance(value, str) and value:
                parts.append(f"{key}={value}")
    if len(parts) <= 2 and text:
        parts.append(text)
    normalized = _normalize_fingerprint(" ".join(parts))
    return truncate_summary(normalized, 240)


def ngrams(labels: Sequence[str], n: int = 4) -> list[tuple[str, ...]]:
    if n <= 0 or len(labels) < n:
        return []
    return [tuple(labels[i : i + n]) for i in range(0, len(labels) - n + 1)]


def shannon_entropy(labels: Sequence[str]) -> float:
    if not labels:
        return 0.0
    total = len(labels)
    entropy = 0.0
    for count in Counter(labels).values():
        p = count / total
        entropy -= p * math.log2(p)
    return round(entropy, 4)


def result_is_failed_text(text: str) -> bool:
    low = text.lower()
    return (
        "process exited with code " in low
        and not re.search(r"process exited with code 0\b", low)
    ) or any(
        needle in low
        for needle in (
            "traceback",
            "exception",
            "error:",
            "failed",
            "failure",
            "is_error",
            "success': false",
            '"success": false',
        )
    )


def _is_thought_candidate(ev: NormalizedEvent) -> bool:
    return ev.actor == "assistant" and ev.event_type in {"reasoning", "message"}


def _is_action_event(ev: NormalizedEvent) -> bool:
    return (
        ev.actor == "assistant"
        and ev.event_type in {"tool_call", "command", "file_read", "file_edit", "permission"}
    )


def _is_result_event(ev: NormalizedEvent) -> bool:
    return ev.actor == "tool" or ev.event_type in {"tool_result", "error"}


def _action_text(ev: NormalizedEvent) -> str:
    text = event_text(ev)
    if text and text != ev.summary:
        return truncate_summary(text, 1000)
    command = _command_text(ev)
    if command:
        return truncate_summary(command, 1000)
    return truncate_summary(ev.summary or text or "", 1000)


def _event_haystack(ev: NormalizedEvent, text: str | None = None) -> str:
    payload = ev.payload or {}
    return " ".join(
        [
            ev.event_type,
            ev.actor,
            ev.summary or "",
            text or "",
            str(payload.get("name") or ""),
            str(payload.get("tool_name") or ""),
            _command_text(ev),
            str(payload.get("input") or ""),
        ]
    ).lower()


def _name(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    value = payload.get("name") or payload.get("tool_name")
    return str(value).lower() if value else ""


def _command_text(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    for key in ("cmd", "command", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    inp = payload.get("input")
    if isinstance(inp, dict):
        for key in ("cmd", "command"):
            value = inp.get(key)
            if isinstance(value, str) and value:
                return value
    output = payload.get("output")
    if isinstance(output, str):
        match = re.search(r"(?:cmd|command)=([^,\n)]+)", output)
        if match:
            return match.group(1)
    return ev.summary or ""


def _first_command_token(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    # Handle common wrappers such as `python -m pytest`.
    return stripped.split()[0].lower()


def _looks_search(first: str, hay: str) -> bool:
    return first in {"rg", "grep", "find", "fd", "ag"} or any(
        needle in hay
        for needle in (
            "web_search",
            "search_query",
            "search tool",
            "ripgrep",
            "search_code",
            "search_method",
        )
    )


def _looks_explore(first: str, hay: str) -> bool:
    return first in {"ls", "pwd", "sed", "cat", "head", "tail", "tree", "wc"} or any(
        needle in hay for needle in ("read_file", "open(", "list files")
    )


def _looks_locate(hay: str) -> bool:
    return any(
        needle in hay
        for needle in (
            "stack trace",
            "traceback",
            "definition",
            "references",
            "git blame",
            "symbol",
            "line ",
            "locate",
        )
    )


def _looks_test(text: str) -> bool:
    test_commands = (
        r"\b("
        r"pytest|npm (?:run )?test|pnpm (?:run )?test|yarn (?:run )?test|"
        r"go test|cargo test|mvn test|gradle test|tox|tsc|mypy|ruff|eslint|"
        r"pre-commit|make (?:check|test)"
        r")\b"
    )
    return bool(
        re.search(
            test_commands,
            text,
            re.IGNORECASE,
        )
        or re.search(r"-m (?:pytest|unittest)\b", text, re.IGNORECASE)
    )


def _looks_reproduce(hay: str) -> bool:
    return any(
        needle in hay
        for needle in (
            "reproduce",
            "repro",
            "regression",
            "failing test",
            "minimal test",
        )
    )


def _looks_refactor(hay: str) -> bool:
    return any(
        needle in hay
        for needle in (
            "refactor",
            "rename",
            "format",
            "black",
            "prettier",
            "ruff --fix",
            "doc-only",
            "comments only",
        )
    )


def _result_is_failed(ev: NormalizedEvent) -> bool:
    payload = ev.payload or {}
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error"}:
        return True
    return result_is_failed_text(str(payload) + " " + (ev.summary or ""))


def _normalize_fingerprint(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\x1b\[[0-9;]*m", "", value)
    value = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", value)
    value = re.sub(r"\b0x[0-9a-f]+\b", "<addr>", value)
    value = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ][0-9:.\-+z]+\b", "<timestamp>", value)
    value = re.sub(r"/(?:private/)?tmp/[^\s'\",)]+", "/tmp/<tmp>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _fingerprint_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
