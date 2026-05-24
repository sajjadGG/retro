"""skill_pro: mine reusable skills with explicit lifecycle blocks.

A "skill" is a multi-step coding workflow distilled from a successful turn:
the user asked for X, the agent ran a coherent sequence of tools, and the
turn ended without error or user pushback. Each candidate carries four
explicit blocks so a future agent can decide whether to invoke the skill:

    activation:    when this skill should be used (precondition)
    steps:         the ordered tool sequence (abstracted)
    termination:   how the skill knows it's done
    verification:  how to confirm the work landed

Inspired by Skill-Pro (https://arxiv.org/abs/2602.01869). No LLM call;
extraction is heuristic over normalized events. Output is conservative —
turns containing tool errors, user corrections, or very short sequences are
skipped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ...schema import NormalizedEvent
from ..base import (
    MemoryCandidate,
    MiningContext,
    MiningResult,
    event_text,
    memory_id,
    register_method,
    truncate,
)

# A turn must have at least this many tool calls to be worth abstracting.
_MIN_STEPS = 2
# Below this size we don't bother.
_MAX_TURNS_EMITTED = 4
# Words that signal the user was unhappy with the previous turn.
_NEG_SIGNALS = re.compile(
    r"\b(no+|nope|wrong|incorrect|don'?t|stop|revert|undo|that'?s wrong|not (what|right))\b",
    re.IGNORECASE,
)
_POS_SIGNALS = re.compile(
    r"\b(thanks?|great|perfect|nice|works|ship it|lgtm|love it|exactly)\b",
    re.IGNORECASE,
)
# Patterns that signal an intentional verification step (test run, type check,
# linter). Patterns must include a runner — bare `test` is too easy to hit
# inside filenames like `exec-test.md`.
_VERIFICATION_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bpytest\b",
        r"\bnpm (?:run )?test\b",
        r"\b(?:pnpm|yarn) (?:run )?test\b",
        r"\bgo test\b",
        r"\bcargo test\b",
        r"\btypecheck\b",
        r"\btsc(?:\s|$)",
        r"\bmypy\b",
        r"\bruff\b",
        r"\beslint\b",
        r"\bmake (?:check|test)\b",
        r"\bpre-commit\b",
        # `python -m pytest`, `python3 -m unittest`:
        r"-m (?:pytest|unittest)\b",
    )
)


@dataclass
class _Turn:
    user_msg: NormalizedEvent | None  # the user message that opened this turn
    next_user_msg: NormalizedEvent | None  # the message that closed it (if any)
    tool_calls: list[NormalizedEvent]
    tool_results: list[NormalizedEvent]
    file_edits: list[NormalizedEvent]
    assistant_final: NormalizedEvent | None
    had_error: bool


@register_method(
    "skill_pro",
    description=(
        "Distill successful turns into reusable skills with explicit "
        "activation / steps / termination / verification blocks."
    ),
)
def mine_skill_pro(ctx: MiningContext) -> MiningResult:
    method = "skill_pro"
    origin = ctx.origin_repo()
    turns = _segment_turns(ctx.events)
    successful = [t for t in turns if _is_successful_turn(t)]
    # Most informative turns first: more steps, more file edits.
    successful.sort(key=lambda t: (len(t.file_edits), len(t.tool_calls)), reverse=True)

    candidates: list[MemoryCandidate] = []
    for turn in successful[:_MAX_TURNS_EMITTED]:
        c = _turn_to_skill(turn, ctx, len(candidates) + 1, method, origin)
        if c is not None:
            candidates.append(c)

    if not candidates:
        candidates.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, method, 1),
                method=method,
                kind="procedure",
                text=(
                    "No coherent multi-step skill could be extracted from this rollout. "
                    "Treat the session as exploratory and rely on default behaviors."
                ),
                when_to_use="Use when no comparable skill is mined for the task at hand.",
                evidence_refs=[ctx.events[0].event_id],
                confidence=0.3,
                priority=1,
                risk="medium",
                scope="task",
                scope_reason="fallback when no skill was extracted",
                origin_repo=origin,
            )
        )

    return MiningResult(
        session_id=ctx.session_id,
        host=ctx.host,
        method=method,
        task_summary=ctx.task_summary(),
        candidates=candidates,
        notes=[
            f"Segmented rollout into {len(turns)} turn(s); {len(successful)} looked successful.",
            "Heuristic skill extraction; no LLM call.",
        ],
    )


# ---- turn segmentation -----------------------------------------------------


def _segment_turns(events: Sequence[NormalizedEvent]) -> list[_Turn]:
    """Slice events into turns bounded by user messages."""
    turns: list[_Turn] = []
    current_user: NormalizedEvent | None = None
    bucket = _new_bucket()

    def flush(closing: NormalizedEvent | None) -> None:
        if current_user is None and not bucket["tool_calls"]:
            return
        turns.append(
            _Turn(
                user_msg=current_user,
                next_user_msg=closing,
                tool_calls=bucket["tool_calls"],
                tool_results=bucket["tool_results"],
                file_edits=bucket["file_edits"],
                assistant_final=bucket["assistant_final"],
                had_error=bucket["had_error"],
            )
        )

    for ev in events:
        if ev.actor == "user" and ev.event_type == "message":
            # Close out the previous turn, then start a new one rooted at this message.
            flush(ev)
            current_user = ev
            bucket = _new_bucket()
            continue
        if ev.event_type in {"tool_call", "command", "file_edit", "file_read"} and ev.actor == "assistant":
            bucket["tool_calls"].append(ev)
            if ev.event_type == "file_edit":
                bucket["file_edits"].append(ev)
        elif ev.actor == "tool":
            bucket["tool_results"].append(ev)
            if _result_is_error(ev):
                bucket["had_error"] = True
        elif ev.actor == "assistant" and ev.event_type == "message":
            bucket["assistant_final"] = ev
    flush(None)
    return turns


def _new_bucket() -> dict[str, Any]:
    return {
        "tool_calls": [],
        "tool_results": [],
        "file_edits": [],
        "assistant_final": None,
        "had_error": False,
    }


def _result_is_error(ev: NormalizedEvent) -> bool:
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
    return False


# ---- success classification ------------------------------------------------


def _is_successful_turn(turn: _Turn) -> bool:
    if turn.had_error:
        return False
    if len(turn.tool_calls) < _MIN_STEPS:
        return False
    if turn.next_user_msg is not None:
        text = event_text(turn.next_user_msg)
        if _NEG_SIGNALS.search(text):
            return False
    return True


# ---- skill abstraction -----------------------------------------------------


def _turn_to_skill(
    turn: _Turn,
    ctx: MiningContext,
    index: int,
    method: str,
    origin: str | None,
) -> MemoryCandidate | None:
    user_text = event_text(turn.user_msg) if turn.user_msg else ""
    activation = truncate(user_text.replace("\n", " "), 180) or "(no opening user message)"

    steps = _abstract_steps(turn.tool_calls)
    if len(steps) < _MIN_STEPS:
        return None
    termination = _describe_termination(turn)
    verification = _describe_verification(turn)
    title, title_was_hint = _skill_title(user_text, turn)

    confidence = 0.55
    if turn.next_user_msg is not None and _POS_SIGNALS.search(event_text(turn.next_user_msg)):
        confidence = 0.75
    if verification:
        confidence = min(0.85, confidence + 0.05)

    scope, scope_reason = _infer_scope(turn, origin, title_was_hint)

    structured = {
        "activation": activation,
        "steps": steps,
        "termination": termination,
        "verification": verification or "(no explicit verification observed)",
    }

    evidence = [turn.user_msg, *turn.tool_calls[:4]]
    if turn.next_user_msg:
        evidence.append(turn.next_user_msg)

    return MemoryCandidate(
        id=memory_id(ctx.session_id, method, index),
        method=method,
        kind="skill",
        text=f"Skill: {title}",
        when_to_use=f"Use when the user asks for: {activation}",
        evidence_refs=[e.event_id for e in evidence if e is not None][:6],
        confidence=confidence,
        priority=4,
        risk="medium",
        scope=scope,
        scope_reason=scope_reason,
        origin_repo=origin,
        structured=structured,
    )


def _infer_scope(turn: _Turn, origin: str | None, title_was_hint: bool):
    """Classify a skill as task-scoped (broad pattern) vs repo-scoped (uses
    local files heavily). Returns (scope, scope_reason).
    """
    # Count how many tool calls in this turn touched a path inside the rollout
    # cwd. If most steps are inside the local repo it's repo-bound; if the
    # title matched a broad verb hint and the workflow is paths-light, call it
    # task-scoped.
    repo_local = 0
    for ev in turn.tool_calls:
        payload = ev.payload or {}
        inp = payload.get("input") or payload.get("arguments") or {}
        if isinstance(inp, str):
            text = inp
        elif isinstance(inp, dict):
            text = " ".join(str(v) for v in inp.values() if isinstance(v, (str, int)))
        else:
            text = ""
        if origin and origin in text:
            repo_local += 1
    if title_was_hint and repo_local <= max(1, len(turn.tool_calls) // 4):
        return "task", "broad task-shaped workflow, only lightly tied to repo paths"
    if repo_local >= 2:
        return "repo", f"references files inside {origin} repeatedly"
    return "repo", "default: skills tend to be repo-bound"


def _abstract_steps(tool_calls: Sequence[NormalizedEvent], *, limit: int = 10) -> list[str]:
    """Render each tool call as a single line, abstracting concrete paths."""
    out: list[str] = []
    for ev in tool_calls[:limit]:
        out.append(_describe_call(ev))
    if len(tool_calls) > limit:
        out.append(f"… (+{len(tool_calls) - limit} more)")
    return out


def _describe_call(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    name = payload.get("name") or payload.get("tool_name") or ev.event_type
    inp = payload.get("input")
    if inp is None:
        inp = payload.get("arguments")
    detail = _summarize_input(inp)
    label = {
        "command": "run shell",
        "file_edit": "edit file",
        "file_read": "read file",
        "tool_call": str(name) if name and name != "?" else "tool_call",
    }.get(ev.event_type, str(name))
    return f"{label} ({detail})" if detail else label


_APPLY_PATCH_FILE_RE = re.compile(
    r"\*\*\* (?:Add|Update|Delete) File:\s*(\S[^\n]*)", re.IGNORECASE
)


def _summarize_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        # Codex `apply_patch` ships the patch text as a string; extract the
        # target file from the patch header rather than dumping raw diff.
        match = _APPLY_PATCH_FILE_RE.search(value)
        if match:
            return f"file={_anonymize_path(match.group(1).strip())[:80]}"
        parsed = _maybe_json(value)
        if isinstance(parsed, dict):
            value = parsed
        else:
            return truncate(value, 80)
    if isinstance(value, dict):
        # Order matters: prefer path/file then command/cmd then descriptive keys.
        for k in ("file_path", "path", "file", "cmd", "command", "pattern", "query", "url"):
            v = value.get(k)
            if isinstance(v, str):
                return f"{k}={_anonymize_path(v)[:80]}"
        if "description" in value and isinstance(value["description"], str):
            return value["description"][:80]
    return truncate(str(value), 80)


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


_HOME_RE = re.compile(r"^/Users/[^/]+/?")


def _anonymize_path(path: str) -> str:
    # Replace concrete home prefix with ~, then keep the last 2 components for
    # context (e.g. `~/.../specs/foo.md`).
    p = _HOME_RE.sub("~/", path)
    parts = p.split("/")
    if len(parts) > 5:
        return "/".join([parts[0] or "/", "…", *parts[-3:]])
    return p


def _describe_termination(turn: _Turn) -> str:
    if turn.file_edits:
        last_edit = turn.file_edits[-1]
        path = _last_edited_path(last_edit)
        if path:
            return f"after writing {_anonymize_path(path)}"
        return "after the last file edit succeeds"
    if turn.tool_calls:
        last = turn.tool_calls[-1]
        if last.event_type == "command":
            return "after the final command returns exit code 0"
        return f"after `{(last.payload or {}).get('name', 'the last tool call')}` returns success"
    return "when the agent emits a final message"


def _describe_verification(turn: _Turn) -> str:
    """Look for a verification step (test run, type check, ...) near the end."""
    tail = turn.tool_calls[-6:]
    for ev in reversed(tail):
        if ev.event_type != "command":
            continue
        text = _summarize_input((ev.payload or {}).get("input") or (ev.payload or {}).get("arguments"))
        if any(p.search(text) for p in _VERIFICATION_PATTERNS):
            return f"verified by running `{text[:80]}`"
    # File-edit-only flows: imply manual review.
    if turn.file_edits and not any(t.event_type == "command" for t in turn.tool_calls):
        return "verified by reviewing the resulting file diff"
    return ""


def _last_edited_path(ev: NormalizedEvent) -> str | None:
    payload = ev.payload or {}
    inp = payload.get("input") or {}
    for k in ("file_path", "path", "file"):
        v = inp.get(k) if isinstance(inp, dict) else None
        if isinstance(v, str):
            return v
    changes = payload.get("changes")
    if isinstance(changes, dict) and changes:
        return next(iter(changes.keys()))
    return None


_TITLE_VERB_HINTS = (
    ("spec", "draft a spec doc"),
    ("test", "add or update tests"),
    ("benchmark", "run a benchmark"),
    ("plan", "write an implementation plan"),
    ("refactor", "refactor existing code"),
    ("fix", "investigate and patch a bug"),
    ("research", "research a topic and summarize findings"),
    ("review", "review the changes on this branch"),
    ("dashboard", "build or update a dashboard"),
    ("import", "import data from a host"),
)


def _skill_title(user_text: str, turn: _Turn) -> tuple[str, bool]:
    """Returns (title, came_from_hint_table) so the caller can decide scope."""
    lowered = user_text.lower()
    for needle, title in _TITLE_VERB_HINTS:
        if needle in lowered:
            return title, True
    if turn.file_edits:
        return "edit-then-verify workflow", True
    if any(t.event_type == "command" for t in turn.tool_calls):
        return "investigate via shell commands", True
    return "multi-step tool workflow", False
