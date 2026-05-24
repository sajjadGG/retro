"""codex_headless: use Codex exec as an LLM-backed memory miner.

This method is intentionally opt-in. It sends a redacted, compact view of a
captured rollout to `codex exec`, saves Codex's headless JSONL stream, and
converts the final structured response into normal MemoryCandidate records.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..base import (
    MemoryCandidate,
    MiningContext,
    MiningResult,
    memory_id,
    register_method,
    truncate,
)

METHOD = "codex_headless"
MAX_EVENTS = 90
MAX_TEXT_PER_EVENT = 900
MAX_PROMPT_CHARS = 55_000


@register_method(
    METHOD,
    description=(
        "LLM-backed miner: calls `codex exec --json` on a redacted rollout "
        "summary and saves the headless Codex JSONL capture."
    ),
)
def mine_codex_headless(ctx: MiningContext) -> MiningResult:
    prompt = _build_prompt(ctx)
    schema = _response_schema()
    capture_path = _headless_capture_path(ctx)

    with tempfile.TemporaryDirectory(prefix="retro-codex-headless-") as td:
        tmp = Path(td)
        schema_path = tmp / "schema.json"
        response_path = tmp / "response.json"
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")

        cmd = [
            "codex",
            "-a",
            "never",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "-o",
            str(response_path),
            "-",
        ]
        cwd = ctx.origin_repo()
        if cwd and Path(cwd).exists():
            cmd[3:3] = ["-C", cwd]

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=900,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("`codex` executable was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            _write_headless_capture(capture_path, exc.stdout or "", exc.stderr or "")
            raise RuntimeError("codex headless mining timed out") from exc

        _write_headless_capture(capture_path, proc.stdout, proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex headless mining failed with exit {proc.returncode}; "
                f"capture={capture_path}"
            )
        if not response_path.exists():
            raise RuntimeError(f"codex headless produced no final response; capture={capture_path}")

        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"codex headless final response was not JSON; capture={capture_path}") from exc

    candidates = _candidates_from_response(ctx, response)
    if not candidates:
        candidates = [
            MemoryCandidate(
                id=memory_id(ctx.session_id, METHOD, 1),
                method=METHOD,
                kind="procedure",
                text="No durable memory was confidently extracted by the Codex headless miner.",
                when_to_use="Use only as a signal that this rollout may not contain reusable lessons.",
                evidence_refs=[],
                confidence=0.3,
                priority=1,
                risk="medium",
                scope="task",
                scope_reason="fallback from empty LLM extraction",
                origin_repo=ctx.origin_repo(),
            )
        ]

    return MiningResult(
        session_id=ctx.session_id,
        host=ctx.host,
        method=METHOD,
        task_summary=ctx.task_summary(),
        candidates=candidates,
        notes=[
            "LLM-backed extraction via `codex exec --json`.",
            "The rollout sent to Codex was compacted and redacted for likely secrets.",
            f"Headless Codex JSONL capture: {capture_path}",
        ],
    )


def _build_prompt(ctx: MiningContext) -> str:
    events = _compact_events(ctx)
    payload = {
        "source": f"{ctx.host}/{ctx.session_id}",
        "task_summary": ctx.task_summary(),
        "origin_repo": ctx.origin_repo(),
        "events": events,
    }
    prompt = f"""
You are mining a coding-agent rollout into durable prompt-time memories.

Return ONLY JSON that matches the supplied schema. Extract a small number of
future-useful memories. Prefer lessons supported by evidence in the rollout:
user preferences, repo conventions, tool lessons, failure triggers, risk rules,
or reusable procedures. Avoid one-off facts. Do not include any secret value.
If a secret appears in the evidence, mention only that a secret exposure happened.

For every candidate:
- use one of the allowed `kind`, `scope`, and `risk` values;
- include `evidence_refs` as event ids from the compact rollout;
- keep `text` actionable and short;
- set `confidence` from 0.0 to 1.0 and `priority` from 1 to 5.

Compact redacted rollout JSON:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n[truncated compact rollout]"
    return prompt


def _compact_events(ctx: MiningContext) -> list[dict[str, Any]]:
    interesting = []
    for ev in ctx.events:
        if ev.event_type in {
            "message",
            "tool_call",
            "tool_result",
            "command",
            "file_edit",
            "error",
            "unknown",
        } or ev.actor == "user":
            interesting.append(ev)

    selected = interesting[:MAX_EVENTS]
    if len(interesting) > MAX_EVENTS:
        tail = interesting[-20:]
        selected = interesting[: MAX_EVENTS - len(tail)] + tail

    out = []
    for ev in selected:
        text = _event_text(ev)
        out.append(
            {
                "event_id": ev.event_id,
                "timestamp": ev.timestamp,
                "actor": ev.actor,
                "event_type": ev.event_type,
                "summary": _redact(ev.summary or ""),
                "text": _redact(truncate(text, MAX_TEXT_PER_EVENT)),
            }
        )
    return out


def _event_text(ev) -> str:
    payload = ev.payload or {}
    pieces = []
    for key in ("text", "message", "thinking", "output", "arguments", "input"):
        value = payload.get(key)
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, (dict, list)):
            pieces.append(json.dumps(value, ensure_ascii=False)[:MAX_TEXT_PER_EVENT])
    if not pieces:
        pieces.append(json.dumps(payload, ensure_ascii=False)[:MAX_TEXT_PER_EVENT])
    return "\n".join(pieces)


_SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opsru]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{25,}\b"),
    re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|private[_-]?key)\b"
        r"\s*[:=]\s*['\"]?[^'\"\s]{12,}"
    ),
]


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_summary": {"type": "string"},
            "candidates": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "procedure",
                                "skill",
                                "failure_trigger",
                                "user_preference",
                                "repo_convention",
                                "tool_lesson",
                                "risk_rule",
                                "case",
                            ],
                        },
                        "text": {"type": "string"},
                        "when_to_use": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                        "scope": {
                            "type": "string",
                            "enum": ["user", "repo", "task", "global"],
                        },
                        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                        "scope_reason": {"type": "string"},
                    },
                    "required": [
                        "kind",
                        "text",
                        "when_to_use",
                        "evidence_refs",
                        "confidence",
                        "priority",
                        "scope",
                        "risk",
                        "scope_reason",
                    ],
                },
            },
        },
        "required": ["task_summary", "candidates"],
    }


def _candidates_from_response(ctx: MiningContext, response: dict[str, Any]) -> list[MemoryCandidate]:
    out: list[MemoryCandidate] = []
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return out
    for index, item in enumerate(candidates[:8], 1):
        if not isinstance(item, dict):
            continue
        out.append(
            MemoryCandidate(
                id=memory_id(ctx.session_id, METHOD, index),
                method=METHOD,
                kind=item.get("kind", "procedure"),
                text=str(item.get("text", "")).strip(),
                when_to_use=str(item.get("when_to_use", "")).strip(),
                evidence_refs=[str(x) for x in item.get("evidence_refs", [])][:8],
                confidence=_clamp_float(item.get("confidence"), 0.0, 1.0, 0.5),
                priority=int(_clamp_float(item.get("priority"), 1, 5, 3)),
                scope=item.get("scope", "task"),
                risk=item.get("risk", "medium"),
                scope_reason=str(item.get("scope_reason", "")).strip(),
                origin_repo=ctx.origin_repo(),
            )
        )
    return [c for c in out if c.text and c.when_to_use]


def _clamp_float(value, lo: float, hi: float, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _headless_capture_path(ctx: MiningContext) -> Path:
    root = ctx.artifact_root() or Path.cwd() / "rollout-memory"
    return root / "headless" / METHOD / ctx.host / f"{ctx.session_id}.codex.jsonl"


def _write_headless_capture(path: Path, stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(stdout, bytes):
        stdout_text = stdout.decode("utf-8", errors="replace")
    else:
        stdout_text = stdout or ""
    if isinstance(stderr, bytes):
        stderr_text = stderr.decode("utf-8", errors="replace")
    else:
        stderr_text = stderr or ""
    text = stdout_text
    if stderr_text.strip():
        text += "\n" + json.dumps({"type": "stderr", "text": stderr_text}, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
