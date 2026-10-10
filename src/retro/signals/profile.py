"""One pass over a session's events produces every fact the signals need.

The v1 design re-scanned the whole event list once per signal (65 scans per
session). Here the scan happens exactly once and produces a `SessionProfile`;
signal functions are then pure, cheap reads over that struct. Everything is
keyed on payload content and actor rather than on `event_type` alone, because
the normalized event taxonomy is changing underneath us and the same logical
thing (a tool result, a usage snapshot) is typed differently by different
importers and schema versions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..schema import NormalizedEvent
from ..storage import Layout
from ..utils import event_file_paths, event_text, event_tool_input, payload_tool_output
from . import commands as cmd
from . import patterns as pat
from .meta import is_temp_cwd, meta_cwd, read_raw_meta
from .pricing import CostBreakdown, PricingMap, default_pricing
from .usage import UsageStats, extract_usage

ACTION_TYPES = frozenset({"tool_call", "command", "file_read", "file_edit"})
NON_CONVERSATIONAL_TYPES = frozenset(
    {"usage", "lifecycle", "metadata", "session_start", "session_end", "subagent_start", "subagent_end"}
)
RESULT_KEYS = ("tool_use_id", "output", "result", "is_error", "success", "stdout", "exit_code")
CALL_ID_KEYS = ("call_id", "tool_id", "tool_use_id", "toolCallId", "tool_call_id", "id")

ACTIVE_GAP_CAP_S = 300.0
REPLY_GAP_CAP_S = 3600.0
RESULT_TEXT_LIMIT = 20_000
DIFF_LINE_RE = re.compile(r"^[+-](?!\+\+|--)", re.MULTILINE)
EXIT_CODE_RE = re.compile(
    r"(?:process exited with code|exited with code|exit code|exit status)\D{0,3}(-?\d+)",
    re.IGNORECASE,
)
_SEARCH_TOOL_RE = re.compile(r"(?i)search|grep|glob|find|lookup")
_WEB_TOOL_RE = re.compile(r"(?i)web_?search|web_?fetch|fetch_?url|browser|websearch")
_COMPACTION_KINDS = frozenset(
    {"context_compacted", "compacted", "session.compaction_start", "session.compaction_end", "compaction"}
)


@dataclass
class ValidationRun:
    category: str
    ok: bool
    event_id: str
    index: int


@dataclass
class SessionProfile:
    key: str
    host: str
    session_id: str

    agent: str | None = None
    client: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    interactive: bool | None = None
    tags: tuple[str, ...] = ()
    started_at: str | None = None
    cwd: str | None = None
    title: str = ""

    events_total: int = 0
    user_turns: int = 0
    injected_user_messages: int = 0
    assistant_messages: int = 0
    reasoning_events: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    tool_errors: int = 0
    unlinked_results: int = 0
    command_calls: int = 0
    command_errors: int = 0
    file_edits: int = 0
    file_reads: int = 0
    searches: int = 0
    web_searches: int = 0
    subagent_events: int = 0
    unknown_events: int = 0
    untimed_events: int = 0
    compactions: int = 0
    permission_requests: int = 0
    permission_denials: int = 0
    provider_errors: int = 0
    rate_limit_events: int = 0
    context_limit_events: int = 0

    files_edited: set[str] = field(default_factory=set)
    files_read: set[str] = field(default_factory=set)
    edits_per_file: Counter[str] = field(default_factory=Counter)
    edited_lines: int = 0
    edits_with_diff: int = 0
    edits_outside_workspace: int = 0

    first_ts: datetime | None = None
    last_ts: datetime | None = None
    duration_s: float | None = None
    active_s: float | None = None
    time_to_first_edit_s: float | None = None
    reply_gaps: list[float] = field(default_factory=list)
    error_streak_max: int = 0
    trailing_failures: int = 0

    first_prompt: str = ""
    last_human_text: str = ""
    final_assistant_text: str = ""
    turns_before_first_edit: int = 0
    correction_events: list[str] = field(default_factory=list)
    interruption_events: list[str] = field(default_factory=list)

    validation_runs: list[ValidationRun] = field(default_factory=list)
    commit_events: list[tuple[str, bool]] = field(default_factory=list)
    error_events: list[str] = field(default_factory=list)

    command_categories: Counter[str] = field(default_factory=Counter)
    base_commands: Counter[str] = field(default_factory=Counter)
    base_command_failures: Counter[str] = field(default_factory=Counter)
    tool_names: Counter[str] = field(default_factory=Counter)
    tool_failures: Counter[str] = field(default_factory=Counter)

    secret_kinds: set[str] = field(default_factory=set)
    secret_events: list[str] = field(default_factory=list)
    secret_scan_truncated: bool = False
    destructive_events: list[tuple[str, str]] = field(default_factory=list)

    usage: UsageStats = field(default_factory=UsageStats)
    cost: CostBreakdown = field(default_factory=lambda: CostBreakdown(0.0, {}, {}, "none", ()))
    models: tuple[str, ...] = ()

    @property
    def validated(self) -> bool | None:
        if not self.validation_runs:
            return None
        return self.validation_runs[-1].ok

    @property
    def strong_validation(self) -> bool:
        return any(run.category in cmd.STRONG_VALIDATION for run in self.validation_runs)

    @property
    def committed(self) -> bool:
        return any(ok for _, ok in self.commit_events)

    @property
    def ended_in_error(self) -> bool:
        return self.trailing_failures > 0

    @property
    def tool_error_rate(self) -> float | None:
        if not self.tool_results:
            return None
        return round(self.tool_errors / self.tool_results, 4)

    @property
    def edit_churn(self) -> float | None:
        if self.file_edits < 2 or not self.files_edited:
            return None
        return round(max(0, self.file_edits - len(self.files_edited)) / self.file_edits, 4)

    @property
    def has_activity(self) -> bool:
        return bool(self.user_turns or self.tool_calls or self.assistant_messages)


def build_profile(
    *,
    key: str,
    host: str,
    session_id: str,
    events: Sequence[NormalizedEvent],
    raw_dir: Path,
    record: Any = None,
    pricing: PricingMap | None = None,
) -> SessionProfile:
    """Build the fact bundle for one session from its normalized events."""
    profile = SessionProfile(key=key, host=host, session_id=session_id)
    _apply_record(profile, record)
    raw_meta = read_raw_meta(raw_dir)
    profile.cwd = profile.cwd or meta_cwd(raw_meta)
    profile.title = profile.title or _meta_title(raw_meta)

    state = _ScanState(profile)
    for event in events:
        state.visit(event)
    state.finish()

    if profile.cwd is None:
        profile.cwd = state.event_cwd
    _score_edit_locations(profile)

    profile.usage = extract_usage(host, session_id, events, raw_dir)
    pricing_map = pricing or default_pricing()
    profile.cost = pricing_map.price(profile.usage.by_model, embedded_usd=profile.usage.embedded_cost_usd)
    models = [m for m in profile.usage.models if m != "unknown"]
    if not models:
        models = [m for m in (profile.models or ()) if m]
    profile.models = tuple(dict.fromkeys(models))
    return profile


def load_profile(
    layout: Layout,
    host: str,
    session_id: str,
    *,
    key: str | None = None,
    record: Any = None,
    pricing: PricingMap | None = None,
) -> SessionProfile:
    from ..schema import read_events

    path = layout.normalized_path(host, session_id)
    events = list(read_events(path)) if path.exists() else []
    return build_profile(
        key=key or f"{host}/{session_id}",
        host=host,
        session_id=session_id,
        events=events,
        raw_dir=layout.raw_dir(host, session_id),
        record=record,
        pricing=pricing,
    )


def _apply_record(profile: SessionProfile, record: Any) -> None:
    if record is None:
        return
    profile.agent = _opt_str(getattr(record, "agent", None))
    profile.client = _opt_str(getattr(record, "client", None))
    interactive = getattr(record, "interactive", None)
    profile.interactive = interactive if isinstance(interactive, bool) else None
    profile.started_at = _opt_str(getattr(record, "started_at", None))
    tags = getattr(record, "tags", None)
    if isinstance(tags, (list, tuple)):
        profile.tags = tuple(str(tag) for tag in tags)
    project = getattr(record, "project", None)
    if isinstance(project, Mapping):
        profile.project_id = _opt_str(project.get("id"))
        profile.project_name = _opt_str(project.get("name"))
    models = getattr(record, "models", None)
    if isinstance(models, (list, tuple)):
        profile.models = tuple(str(m) for m in models if m)
    cwd = getattr(record, "cwd_resolved", None) or getattr(record, "cwd", None)
    profile.cwd = _opt_str(cwd)
    profile.title = _opt_str(getattr(record, "title", None)) or ""


class _ScanState:
    """Mutable bookkeeping for the single pass."""

    def __init__(self, profile: SessionProfile):
        self.p = profile
        self.calls_by_id: dict[str, str] = {}
        self.call_meta: dict[str, tuple[str, str, AbstractSet[str], str]] = {}
        self.pending_calls: list[str] = []
        self.event_cwd: str | None = None
        self.secret_budget = pat.SECRET_SCAN_SESSION_LIMIT
        self.first_ts: datetime | None = None
        self.last_ts: datetime | None = None
        self.stamps: list[float] = []
        self.first_user_ts: datetime | None = None
        self.first_edit_ts: datetime | None = None
        self.saw_edit = False
        self.streak = 0
        self.index = 0
        self.awaiting_reply = False
        self.last_agent_ts: datetime | None = None
        self.result_failed_by_call: dict[str, bool] = {}

    # -- per event ----------------------------------------------------------

    def visit(self, event: NormalizedEvent) -> None:
        p = self.p
        p.events_total += 1
        self.index += 1
        payload = event.payload if isinstance(event.payload, dict) else {}

        if self.event_cwd is None:
            candidate = payload.get("cwd")
            if isinstance(candidate, str) and candidate:
                self.event_cwd = candidate

        timestamp = _parse_ts(event.timestamp)
        if timestamp is None:
            p.untimed_events += 1
        else:
            self.stamps.append(timestamp.timestamp())
            if self.first_ts is None:
                self.first_ts = timestamp
            self.last_ts = timestamp

        if event.event_type == "unknown":
            p.unknown_events += 1
        if event.event_type == "compaction" or _payload_kind(payload) in _COMPACTION_KINDS:
            p.compactions += 1
        if event.actor == "subagent" or event.event_type in ("subagent_start", "subagent_end"):
            p.subagent_events += 1

        self._scan_text_markers(event, payload)

        if event.event_type == "permission" or str(_payload_kind(payload) or "").startswith("permission."):
            self._handle_permission(event, payload)

        if _is_result(event, payload):
            self._handle_result(event, payload)
            if timestamp is not None:
                self.last_agent_ts = timestamp
            return
        if event.event_type == "message":
            self._handle_message(event, payload, timestamp)
            return
        if event.event_type == "reasoning":
            p.reasoning_events += 1
            return
        if event.event_type in ACTION_TYPES and event.actor in ("assistant", "subagent"):
            self._handle_call(event, payload, timestamp)
            if timestamp is not None:
                self.last_agent_ts = timestamp
            return
        if event.event_type == "error" and len(p.error_events) < 50:
            p.error_events.append(event.event_id)

    # -- handlers -----------------------------------------------------------

    def _handle_message(
        self,
        event: NormalizedEvent,
        payload: Mapping[str, Any],
        timestamp: datetime | None,
    ) -> None:
        p = self.p
        text = event_text(event)
        if event.actor == "user":
            if pat.is_injected_user_text(text, payload):
                p.injected_user_messages += 1
                return
            human = pat.human_text(text) or text.strip()
            p.user_turns += 1
            p.last_human_text = human[:4000]
            if not p.first_prompt:
                p.first_prompt = human[:4000]
                self.first_user_ts = timestamp
            elif pat.is_correction(human) and len(p.correction_events) < 50:
                p.correction_events.append(event.event_id)
            if not self.saw_edit:
                p.turns_before_first_edit += 1
            if self.awaiting_reply and timestamp is not None and self.last_agent_ts is not None:
                gap = (timestamp - self.last_agent_ts).total_seconds()
                if 0 < gap <= REPLY_GAP_CAP_S:
                    p.reply_gaps.append(gap)
            self.awaiting_reply = False
        elif event.actor == "assistant":
            p.assistant_messages += 1
            if text.strip():
                p.final_assistant_text = text.strip()[:4000]
            self.awaiting_reply = True
            if timestamp is not None:
                self.last_agent_ts = timestamp

    def _handle_call(
        self,
        event: NormalizedEvent,
        payload: Mapping[str, Any],
        timestamp: datetime | None,
    ) -> None:
        p = self.p
        p.tool_calls += 1
        call_id = _call_id(payload)
        if call_id:
            self.calls_by_id[call_id] = event.event_id
        self.pending_calls.append(event.event_id)
        if len(self.pending_calls) > 256:
            for stale in self.pending_calls[:128]:
                self.call_meta.pop(stale, None)
            del self.pending_calls[:128]

        name = _tool_name(event, payload)
        if name:
            p.tool_names[name] += 1
            if _WEB_TOOL_RE.search(name):
                p.web_searches += 1
            elif _SEARCH_TOOL_RE.search(name):
                p.searches += 1

        if event.event_type == "file_edit":
            p.file_edits += 1
            self.saw_edit = True
            if self.first_edit_ts is None and timestamp is not None:
                self.first_edit_ts = timestamp
            paths = _file_paths(event)
            p.files_edited |= paths
            for path in paths:
                p.edits_per_file[path] += 1
            lines = _edited_lines(payload, event_tool_input(event))
            if lines:
                p.edited_lines += lines
                p.edits_with_diff += 1
        elif event.event_type == "file_read":
            p.file_reads += 1
            p.files_read |= _file_paths(event)

        text = cmd.command_text(event)
        if not text:
            self.call_meta[event.event_id] = ("", "", frozenset(), name)
            return
        p.command_calls += 1
        bases = cmd.segment_bases(text)
        for base in bases:
            p.base_commands[base] += 1
        base = bases[0] if bases else ""
        categories = cmd.classify_command(text)
        for category in categories:
            p.command_categories[category] += 1
        if cmd.SEARCH in categories:
            p.searches += 1
        for kind in cmd.destructive_kinds(text):
            if len(p.destructive_events) < 25:
                p.destructive_events.append((kind, event.event_id))
        self.call_meta[event.event_id] = (text, base, categories, name)

    def _handle_result(self, event: NormalizedEvent, payload: Mapping[str, Any]) -> None:
        p = self.p
        call_id = _call_id(payload)
        if call_id and call_id in self.result_failed_by_call:
            self._handle_result_continuation(event, payload, call_id)
            return
        p.tool_results += 1
        matched = self._match_call(event, payload)
        if matched is None:
            p.unlinked_results += 1
        call_meta = self.call_meta.pop(matched, None) if matched else None
        if call_meta is not None and not call_meta[0]:
            call_meta = None
        text = _result_text(payload, event.summary)
        failed = _result_failed(event, payload, text)
        if call_id:
            self.result_failed_by_call[call_id] = failed
        if failed:
            self._record_failure(event)
        else:
            self.streak = 0
            p.trailing_failures = 0

        name = _tool_name(event, payload) or (call_meta[3] if call_meta else "")
        if name and failed:
            p.tool_failures[name] += 1

        command = call_meta[0] if call_meta else cmd.command_text(event)
        if not command:
            return
        if failed:
            p.command_errors += 1
        if failed:
            for base in cmd.segment_bases(command):
                p.base_command_failures[base] += 1
        categories = call_meta[2] if call_meta else cmd.classify_command(command)
        for category in sorted(categories & cmd.VALIDATION_CATEGORIES):
            p.validation_runs.append(
                ValidationRun(category=category, ok=not failed, event_id=event.event_id, index=self.index)
            )
        if cmd.is_git_commit(command):
            p.commit_events.append((event.event_id, not failed and not cmd.commit_was_noop(text)))

    def _handle_result_continuation(
        self, event: NormalizedEvent, payload: Mapping[str, Any], call_id: str
    ) -> None:
        """A later record of a call already scored (begin/end, output deltas).

        v3 emits every host record of one tool invocation, so one `apply_patch`
        can produce a begin and an end event. They describe a single operation:
        count it once, but let a late failure overturn an early success.
        """
        if self.result_failed_by_call.get(call_id):
            return
        text = _result_text(payload, event.summary)
        if not _result_failed(event, payload, text):
            return
        self.result_failed_by_call[call_id] = True
        self._record_failure(event)

    def _record_failure(self, event: NormalizedEvent) -> None:
        p = self.p
        p.tool_errors += 1
        self.streak += 1
        p.error_streak_max = max(p.error_streak_max, self.streak)
        p.trailing_failures += 1
        if len(p.error_events) < 50:
            p.error_events.append(event.event_id)

    def _handle_permission(self, event: NormalizedEvent, payload: Mapping[str, Any]) -> None:
        p = self.p
        kind = str(_payload_kind(payload) or "")
        if kind.endswith("requested"):
            p.permission_requests += 1
        blob = " ".join(
            str(value) for value in (payload.get("result"), payload.get("data"), event.summary) if value
        ).lower()
        if "denied" in blob or "rejected" in blob or "not approved" in blob:
            p.permission_denials += 1

    def _scan_text_markers(self, event: NormalizedEvent, payload: Mapping[str, Any]) -> None:
        p = self.p
        kind = _payload_kind(payload)
        if kind in pat.INTERRUPT_PAYLOAD_TYPES or payload.get("lifecycle") in pat.INTERRUPT_LIFECYCLE:
            if _is_user_abort(payload):
                p.interruption_events.append(event.event_id)
        if payload.get("isCanceled") is True or payload.get("isCancelled") is True:
            p.interruption_events.append(event.event_id)

        if self.secret_budget <= 0:
            p.secret_scan_truncated = True
            return
        found_interrupt = False
        for chunk in pat.iter_payload_strings(payload, budget=pat.SECRET_SCAN_EVENT_LIMIT):
            self.secret_budget -= len(chunk)
            if len(chunk) > 24:
                for secret_kind in pat.find_secrets(chunk):
                    p.secret_kinds.add(secret_kind)
                    if len(p.secret_events) < 25:
                        p.secret_events.append(event.event_id)
            lowered = chunk.lower() if len(chunk) <= 4000 else chunk[:4000].lower()
            if not found_interrupt and ("interrupt" in lowered or "abort" in lowered or "cancel" in lowered):
                if any(marker in lowered for marker in pat.INTERRUPT_TEXTS):
                    p.interruption_events.append(event.event_id)
                    found_interrupt = True
            if ("limit" in lowered or "429" in lowered or "529" in lowered or "quota" in lowered) and (
                pat.RATE_LIMIT_RE.search(lowered)
            ):
                p.rate_limit_events += 1
            elif ("error" in lowered or "econnreset" in lowered or "unavailable" in lowered) and (
                pat.PROVIDER_ERROR_RE.search(lowered)
            ):
                p.provider_errors += 1
            if ("context" in lowered or "too long" in lowered) and pat.CONTEXT_LIMIT_RE.search(lowered):
                p.context_limit_events += 1
            if self.secret_budget <= 0:
                p.secret_scan_truncated = True
                break

    def _match_call(self, event: NormalizedEvent, payload: Mapping[str, Any]) -> str | None:
        """Link a result to its call by id, then parent, then arrival order."""
        call_id = _call_id(payload)
        event_id = self.calls_by_id.pop(call_id, "") if call_id else ""
        if not event_id and event.parent_event_id and event.parent_event_id in self.call_meta:
            event_id = event.parent_event_id
        if not event_id and self.pending_calls:
            event_id = self.pending_calls.pop(0)
        if not event_id:
            return None
        _discard(self.pending_calls, event_id)
        return event_id

    # -- finalization -------------------------------------------------------

    def finish(self) -> None:
        p = self.p
        if self.stamps:
            self.stamps.sort()
            p.first_ts = _from_epoch(self.stamps[0], self.first_ts)
            p.last_ts = _from_epoch(self.stamps[-1], self.last_ts)
            p.duration_s = round(max(0.0, self.stamps[-1] - self.stamps[0]), 3)
            active = 0.0
            for index in range(1, len(self.stamps)):
                gap = self.stamps[index] - self.stamps[index - 1]
                if 0 < gap <= ACTIVE_GAP_CAP_S:
                    active += gap
            p.active_s = round(active, 3)
        if self.first_user_ts and self.first_edit_ts and self.first_edit_ts >= self.first_user_ts:
            p.time_to_first_edit_s = round((self.first_edit_ts - self.first_user_ts).total_seconds(), 3)
        p.interruption_events = list(dict.fromkeys(p.interruption_events))[:25]
        if not p.started_at and p.first_ts is not None:
            p.started_at = p.first_ts.isoformat().replace("+00:00", "Z")


def _score_edit_locations(profile: SessionProfile) -> None:
    root = profile.cwd
    if not root or not profile.files_edited:
        return
    root = root.rstrip("/")
    if not root.startswith("/"):
        return
    outside = 0
    for path in profile.files_edited:
        if not path.startswith("/"):
            continue
        if not (path == root or path.startswith(root + "/")):
            outside += 1
    profile.edits_outside_workspace = outside


# --- event helpers -----------------------------------------------------------


def _is_result(event: NormalizedEvent, payload: Mapping[str, Any]) -> bool:
    """Call or result? Hosts disagree, so decide on actor plus payload shape.

    Claude and Copilot keep the typed event (`command`, `file_edit`) on the
    *result* too, and Copilot emits a large volume of hook `attachment` events
    with `actor="tool"` that are not tool results at all.
    """
    if event.event_type == "tool_result":
        return True
    if event.actor == "tool" and event.event_type in ACTION_TYPES:
        return True
    if event.actor in ("tool", "subagent"):
        if any(key in payload for key in RESULT_KEYS):
            return True
        part = payload.get("part")
        return isinstance(part, dict) and any(key in part for key in RESULT_KEYS)
    return False


def _file_paths(event: NormalizedEvent) -> set[str]:
    """Paths named by a file event, including a JSON-encoded tool input.

    Codex serializes its tool arguments as a JSON string, so `event_file_paths`
    sees text where it expects a mapping and finds nothing for reads.
    """
    paths = event_file_paths(event)
    if paths:
        return paths
    raw = event_tool_input(event)
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return paths
    try:
        parsed = json.loads(raw)
    except ValueError:
        return paths
    if not isinstance(parsed, dict):
        return paths
    return event_file_paths(replace(event, payload=parsed))


def _call_id(payload: Mapping[str, Any]) -> str:
    for key in CALL_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_name(event: NormalizedEvent | None, payload: Mapping[str, Any]) -> str:
    for key in ("name", "tool_name", "tool", "toolName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if event is not None and event.summary:
        head = event.summary.split("(", 1)[0].strip()
        if head and len(head) <= 64 and head != "?":
            return head
    return ""


def _payload_kind(payload: Mapping[str, Any]) -> Any:
    for key in ("type", "kind", "event", "lifecycle"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_user_abort(payload: Mapping[str, Any]) -> bool:
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.lower() in pat.USER_ABORT_REASONS:
        return True
    data = payload.get("data")
    if isinstance(data, dict):
        inner = data.get("reason")
        if isinstance(inner, str) and inner.lower() in pat.USER_ABORT_REASONS:
            return True
        if inner is None and _payload_kind(payload) in ("abort", "turn_aborted"):
            return True
        return False
    return reason is None and _payload_kind(payload) in ("abort", "turn_aborted")


def _result_text(payload: Mapping[str, Any], summary: str) -> str:
    parts: list[str] = []
    budget = RESULT_TEXT_LIMIT
    candidates: list[Any] = [payload_tool_output(dict(payload))]
    candidates.extend(
        payload.get(key) for key in ("output", "result", "content", "stdout", "stderr", "error")
    )
    part = payload.get("part")
    if isinstance(part, dict):
        candidates.extend(part.get(key) for key in ("content", "output", "error"))
    seen_text: set[str] = set()
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, str):
            chunk = value[:budget]
            if chunk and chunk not in seen_text:
                seen_text.add(chunk)
                parts.append(chunk)
                budget -= len(chunk)
        elif isinstance(value, (dict, list)):
            for chunk in pat.iter_payload_strings(value, budget=budget):
                if chunk in seen_text:
                    continue
                seen_text.add(chunk)
                parts.append(chunk)
                budget -= len(chunk)
                if budget <= 0:
                    break
        if budget <= 0:
            break
    if not parts and summary:
        parts.append(summary)
    return "\n".join(parts)[:RESULT_TEXT_LIMIT]


def _result_failed(event: NormalizedEvent, payload: Mapping[str, Any], text: str) -> bool:
    containers: list[Mapping[str, Any]] = [payload]
    part = payload.get("part")
    if isinstance(part, dict):
        containers.append(part)
    for container in containers:
        if container.get("is_error") is True or container.get("success") is False:
            return True
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error", "timeout", "cancelled", "canceled"}:
        return True
    for container in containers:
        error = container.get("error")
        if isinstance(error, str) and error.strip():
            return True
        if isinstance(error, dict) and error:
            return True
    exit_code = _nested_exit_code(payload)
    if exit_code is not None:
        return exit_code != 0
    match = EXIT_CODE_RE.search(text)
    if match:
        try:
            return int(match.group(1)) != 0
        except ValueError:
            return False
    return event.event_type == "error"


def _nested_exit_code(value: Any, depth: int = 0) -> int | None:
    if depth > 5 or not isinstance(value, dict):
        return None
    for key in ("exit_code", "exitCode", "exitStatus", "returncode"):
        candidate = value.get(key)
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.lstrip("-").isdigit():
            return int(candidate)
    for child in value.values():
        if isinstance(child, dict):
            found = _nested_exit_code(child, depth + 1)
            if found is not None:
                return found
    return None


def _edited_lines(payload: Mapping[str, Any], tool_input: Any = None) -> int:
    if isinstance(tool_input, str) and tool_input:
        count = _count_diff_lines(tool_input)
        if count:
            return count
    if isinstance(tool_input, dict):
        count = _edited_lines_from(tool_input)
        if count:
            return count
    return _edited_lines_from(payload)


def _edited_lines_from(payload: Mapping[str, Any]) -> int:
    for key in ("patch", "diff", "arguments", "input", "content", "new_string", "newString", "newText"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            count = _count_diff_lines(value)
            if count:
                return count
        if isinstance(value, dict):
            for inner_key in ("patch", "diff", "content", "code", "new_string", "newString", "newText"):
                inner = value.get(inner_key)
                if isinstance(inner, str) and inner:
                    count = _count_diff_lines(inner)
                    if count:
                        return count
    return 0


def _count_diff_lines(text: str) -> int:
    if len(text) > 400_000:
        text = text[:400_000]
    if "\n@@" in text or text.startswith("@@") or "*** Begin Patch" in text:
        return len(DIFF_LINE_RE.findall(text))
    return text.count("\n") + 1 if text.strip() else 0


def _from_epoch(epoch: float, reference: datetime | None) -> datetime:
    from datetime import timezone

    tz = reference.tzinfo if reference is not None else timezone.utc
    return datetime.fromtimestamp(epoch, tz or timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _discard(items: list[str], target: str) -> None:
    try:
        items.remove(target)
    except ValueError:
        pass


def _meta_title(meta: Mapping[str, Any]) -> str:
    for key in ("title", "aiTitle", "ai_title", "name", "summary"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:300]
    return ""


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "SessionProfile",
    "ValidationRun",
    "build_profile",
    "load_profile",
    "is_temp_cwd",
]
