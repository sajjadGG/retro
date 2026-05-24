"""Heuristic and regex signals — pure functions over normalized events."""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from ..schema import NormalizedEvent
from .base import SessionContext, event_text, iter_messages, reading, register

# ---- activity ---------------------------------------------------------------


@register(
    "command_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of shell/exec command tool calls.",
)
def _command_count(ctx: SessionContext):
    hits = [e for e in ctx.events if e.event_type == "command" and e.actor == "assistant"]
    return reading(ctx, _command_count, len(hits), evidence=hits)


@register(
    "file_edit_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of file_edit events (write/edit/apply_patch).",
)
def _file_edit_count(ctx: SessionContext):
    hits = [e for e in ctx.events if e.event_type == "file_edit"]
    return reading(ctx, _file_edit_count, len(hits), evidence=hits)


@register(
    "unique_files_edited",
    group="activity",
    kind="numeric",
    unit="count",
    description="Distinct file paths touched by file_edit events.",
)
def _unique_files_edited(ctx: SessionContext):
    files: set[str] = set()
    for ev in ctx.events:
        if ev.event_type != "file_edit":
            continue
        files.update(_extract_paths(ev))
    return reading(
        ctx,
        _unique_files_edited,
        len(files),
        metadata={"files": sorted(files)[:50]},
    )


@register(
    "unique_files_read",
    group="activity",
    kind="numeric",
    unit="count",
    description="Distinct file paths touched by file_read events.",
)
def _unique_files_read(ctx: SessionContext):
    files: set[str] = set()
    for ev in ctx.events:
        if ev.event_type != "file_read":
            continue
        files.update(_extract_paths(ev))
    return reading(ctx, _unique_files_read, len(files))


@register(
    "web_search_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of web search invocations.",
)
def _web_search_count(ctx: SessionContext):
    hits = []
    for ev in ctx.events:
        payload = ev.payload or {}
        name = payload.get("name") or payload.get("tool_name")
        summary = (ev.summary or "").lower()
        if name in {"WebSearch", "web_search"} or "web_search" in summary:
            hits.append(ev)
    return reading(ctx, _web_search_count, len(hits), evidence=hits)


@register(
    "user_message_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of user-authored message events.",
)
def _user_message_count(ctx: SessionContext):
    msgs = list(iter_messages(ctx.events, "user"))
    return reading(ctx, _user_message_count, len(msgs))


@register(
    "assistant_message_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of assistant-authored message events (excludes reasoning).",
)
def _assistant_message_count(ctx: SessionContext):
    msgs = list(iter_messages(ctx.events, "assistant"))
    return reading(ctx, _assistant_message_count, len(msgs))


# ---- cost / effort ----------------------------------------------------------


@register(
    "session_duration_seconds",
    group="cost",
    kind="numeric",
    unit="seconds",
    description="Wall-clock span between first and last timestamped event.",
)
def _session_duration_seconds(ctx: SessionContext):
    parsed = [_parse_ts(e.timestamp) for e in ctx.events if e.timestamp]
    timestamps: list[datetime] = [t for t in parsed if t is not None]
    if len(timestamps) < 2:
        return None
    duration = int((max(timestamps) - min(timestamps)).total_seconds())
    return reading(ctx, _session_duration_seconds, duration)


@register(
    "time_to_first_edit_seconds",
    group="cost",
    kind="numeric",
    unit="seconds",
    description="Seconds between first user message and first file_edit event.",
)
def _time_to_first_edit_seconds(ctx: SessionContext):
    first_user = next(
        (e for e in ctx.events if e.actor == "user" and e.event_type == "message" and e.timestamp),
        None,
    )
    first_edit = next(
        (e for e in ctx.events if e.event_type == "file_edit" and e.timestamp),
        None,
    )
    if first_user is None or first_edit is None:
        return None
    t0 = _parse_ts(first_user.timestamp)
    t1 = _parse_ts(first_edit.timestamp)
    if t0 is None or t1 is None or t1 < t0:
        return None
    return reading(
        ctx,
        _time_to_first_edit_seconds,
        int((t1 - t0).total_seconds()),
        evidence=[first_user, first_edit],
    )


_CORRECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bno+\b[, ]+(that('?| i)s|it'?s)? (not|wrong|incorrect)",
        r"\b(don'?t|do not) (do|use|run|add|delete|remove)",
        r"\bstop\b.*\b(doing|using|saying)",
        r"\bnot\b\s+(what|exactly)\s+i\s+",
        r"\bthat'?s (wrong|incorrect|not right)",
        r"\bgood catch\b",
        r"\byou'?re right\b",
        r"\bactually,?\s+(i|it|we|that)",
        r"\bovers(t|ta)ated",
        r"\b(use|prefer)\s+\w+\s+instead\b",
        r"\brevert\b",
        r"\bundo\b",
    )
]


@register(
    "user_correction_count",
    group="cost",
    kind="numeric",
    method="regex",
    unit="count",
    description="User messages that look like a correction or course-change.",
)
def _user_correction_count(ctx: SessionContext):
    hits: list[NormalizedEvent] = []
    for ev in iter_messages(ctx.events, "user"):
        text = event_text(ev)
        if any(p.search(text) for p in _CORRECTION_PATTERNS):
            hits.append(ev)
    return reading(
        ctx,
        _user_correction_count,
        len(hits),
        evidence=hits,
        confidence=0.6,
        metadata={"first_match_snippets": [event_text(e)[:120] for e in hits[:3]]},
    )


# ---- outcome ----------------------------------------------------------------


@register(
    "failed_command_count",
    group="outcome",
    kind="numeric",
    unit="count",
    description="Tool results / commands that report an error or non-zero exit.",
)
def _failed_command_count(ctx: SessionContext):
    fails = [e for e in ctx.events if _is_failed(e)]
    return reading(ctx, _failed_command_count, len(fails), evidence=fails)


@register(
    "failed_command_ratio",
    group="outcome",
    kind="numeric",
    unit="ratio",
    description="Failed tool/command results divided by total tool/command results.",
)
def _failed_command_ratio(ctx: SessionContext):
    total = 0
    failed = 0
    for ev in ctx.events:
        if ev.event_type in {"tool_result", "command"} and ev.actor == "tool":
            total += 1
            if _is_failed(ev):
                failed += 1
        elif ev.event_type == "command" and ev.actor != "tool":
            continue
    if total == 0:
        return None
    return reading(
        ctx,
        _failed_command_ratio,
        round(failed / total, 4),
        metadata={"total": total, "failed": failed},
    )


_SATISFACTION_POS = re.compile(
    r"\b(thanks?|thank you|nice|great|perfect|awesome|amazing|love it|works|fixed it|ship it|lgtm)\b",
    re.IGNORECASE,
)
_SATISFACTION_NEG = re.compile(
    r"\b(broken|wrong|doesn'?t work|still failing|that'?s bad|terrible|hate|undo|revert)\b",
    re.IGNORECASE,
)


@register(
    "user_satisfaction_lexical",
    group="outcome",
    kind="categorical",
    method="regex",
    unit="label",
    description="Coarse lexical label on the last user message: positive/negative/neutral.",
)
def _user_satisfaction_lexical(ctx: SessionContext):
    last = None
    for ev in iter_messages(ctx.events, "user"):
        last = ev
    if last is None:
        return None
    text = event_text(last)
    pos = bool(_SATISFACTION_POS.search(text))
    neg = bool(_SATISFACTION_NEG.search(text))
    if pos and not neg:
        label = "positive"
    elif neg and not pos:
        label = "negative"
    else:
        label = "neutral"
    return reading(
        ctx,
        _user_satisfaction_lexical,
        label,
        evidence=[last],
        confidence=0.4,
        metadata={"last_user_snippet": text[:160]},
    )


@register(
    "interrupted_signal",
    group="outcome",
    kind="boolean",
    unit="flag",
    description="True if the session shows interruption/abort markers.",
)
def _interrupted_signal(ctx: SessionContext):
    needles = ("interrupt", "user aborted", "request was canceled", "cancelled by user")
    for ev in ctx.events:
        text = (ev.summary or "").lower() + " " + str(ev.payload or "").lower()[:500]
        if any(n in text for n in needles):
            return reading(ctx, _interrupted_signal, True, evidence=[ev], confidence=0.7)
    return reading(ctx, _interrupted_signal, False)


# ---- risk -------------------------------------------------------------------


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[opsru]_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{25,}\b")),
    ("stripe_secret_key", re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|private[_-]?key)\b"
            r"\s*[:=]\s*['\"]?[^'\"\s]{12,}"
        ),
    ),
]


@register(
    "secret_exposure_count",
    group="risk",
    kind="numeric",
    method="regex",
    unit="count",
    description="Likely API keys, tokens, passwords, or private secrets visible in the captured rollout.",
)
def _secret_exposure_count(ctx: SessionContext):
    matches = _find_secret_exposures(ctx)
    return reading(
        ctx,
        _secret_exposure_count,
        len(matches),
        evidence=[m["event_id"] for m in matches if m.get("event_id")],
        confidence=0.75 if matches else 1.0,
        metadata={
            "types": sorted({m["type"] for m in matches}),
            "examples": matches[:8],
            "redacted": True,
        },
    )


@register(
    "secret_exposure_signal",
    group="risk",
    kind="boolean",
    method="regex",
    unit="flag",
    description="True if the captured rollout appears to contain a secret or credential.",
)
def _secret_exposure_signal(ctx: SessionContext):
    matches = _find_secret_exposures(ctx)
    return reading(
        ctx,
        _secret_exposure_signal,
        bool(matches),
        evidence=[m["event_id"] for m in matches if m.get("event_id")],
        confidence=0.75 if matches else 1.0,
        metadata={
            "count": len(matches),
            "types": sorted({m["type"] for m in matches}),
            "redacted": True,
        },
    )


@register(
    "unknown_event_count",
    group="risk",
    kind="numeric",
    unit="count",
    description="Normalized events that the importer could not classify.",
)
def _unknown_event_count(ctx: SessionContext):
    hits = [e for e in ctx.events if e.event_type == "unknown"]
    return reading(ctx, _unknown_event_count, len(hits))


@register(
    "events_without_timestamps",
    group="risk",
    kind="numeric",
    unit="count",
    description="Events that lack a timestamp (cannot be placed on the timeline).",
)
def _events_without_timestamps(ctx: SessionContext):
    return reading(
        ctx,
        _events_without_timestamps,
        sum(1 for e in ctx.events if not e.timestamp),
    )


@register(
    "capture_gap_signal",
    group="risk",
    kind="boolean",
    unit="flag",
    description="True if any unknown events or missing timestamps were observed.",
)
def _capture_gap_signal(ctx: SessionContext):
    has_gap = any(e.event_type == "unknown" for e in ctx.events) or any(
        not e.timestamp for e in ctx.events
    )
    return reading(ctx, _capture_gap_signal, has_gap)


# ---- helpers ----------------------------------------------------------------


def _extract_paths(ev: NormalizedEvent) -> Iterable[str]:
    payload = ev.payload or {}
    paths: set[str] = set()
    for key in ("file_path", "path", "filepath"):
        v = payload.get(key)
        if isinstance(v, str):
            paths.add(v)
    inp = payload.get("input")
    if isinstance(inp, dict):
        for key in ("file_path", "path", "filepath"):
            v = inp.get(key)
            if isinstance(v, str):
                paths.add(v)
    changes = payload.get("changes")
    if isinstance(changes, dict):
        paths.update(k for k in changes.keys() if isinstance(k, str))
    return paths


def _find_secret_exposures(ctx: SessionContext) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for ev in ctx.events:
        text = _searchable_event_text(ev)
        if not text:
            continue
        for secret_type, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                hits.append(
                    {
                        "type": secret_type,
                        "event_id": ev.event_id,
                        "actor": ev.actor,
                        "event_type": ev.event_type,
                        "snippet": _redacted_window(text, match.start(), match.end()),
                    }
                )
                if len(hits) >= 50:
                    return hits
    return hits


def _searchable_event_text(ev: NormalizedEvent) -> str:
    parts = [ev.summary or ""]
    payload = ev.payload or {}
    parts.append(_stringify_for_secret_scan(payload))
    return "\n".join(p for p in parts if p)


def _stringify_for_secret_scan(value, *, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        out = []
        for key, child in value.items():
            out.append(str(key))
            out.append(_stringify_for_secret_scan(child, depth=depth + 1))
        return "\n".join(out)
    if isinstance(value, list):
        return "\n".join(_stringify_for_secret_scan(v, depth=depth + 1) for v in value)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return ""


def _redacted_window(text: str, start: int, end: int) -> str:
    prefix = text[max(0, start - 40) : start]
    suffix = text[end : min(len(text), end + 40)]
    return f"{prefix}[REDACTED]{suffix}".replace("\n", " ")[:180]


def _is_failed(ev: NormalizedEvent) -> bool:
    payload = ev.payload or {}
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error"}:
        return True
    output = payload.get("output")
    if isinstance(output, str):
        m = re.search(r'"exit_code"\s*:\s*(\d+)', output)
        if m and int(m.group(1)) != 0:
            return True
        if "Process exited with code " in output:
            m2 = re.search(r"Process exited with code (\d+)", output)
            if m2 and int(m2.group(1)) != 0:
                return True
    return ev.event_type == "error"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
