"""Token and cost accounting for every host, in one place.

Providers disagree about what `input_tokens` means: Codex counts cached reads
inside it, Copilot counts both cached reads and cache writes inside it, and
Anthropic keeps all three disjoint. Everything here converts to one normalized
bucket so pricing never needs to know which host produced the numbers:

    input         fresh, uncached prompt tokens
    cache_read    tokens served from a prompt cache
    cache_create  tokens written into a prompt cache
    output        completion tokens (includes reasoning tokens when the
                  provider bills them as output)
    reasoning     reasoning tokens, reported separately for visibility only

Usage is read from normalized events when the importer kept it (Codex
`token_count`, and any host once normalization emits `usage` events); otherwise
it falls back to the immutable `raw/` copy. Extraction keys on payload content,
never on `event_type`, so it survives the normalized-schema change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schema import NormalizedEvent
from ..utils import iter_jsonl

BUCKET_KEYS: tuple[str, ...] = ("input", "cache_read", "cache_create", "output", "reasoning")

UsageBucket = dict[str, int]


def empty_bucket() -> UsageBucket:
    return {key: 0 for key in BUCKET_KEYS}


def add_bucket(target: UsageBucket, source: Mapping[str, int]) -> None:
    for key in BUCKET_KEYS:
        value = source.get(key)
        if isinstance(value, (int, float)):
            target[key] += int(value)


def bucket_total(bucket: Mapping[str, int]) -> int:
    return sum(int(bucket.get(key, 0) or 0) for key in ("input", "cache_read", "cache_create", "output"))


@dataclass
class UsageStats:
    by_model: dict[str, UsageBucket] = field(default_factory=dict)
    source: str = "none"  # normalized | raw | none
    requests: int = 0
    embedded_cost_usd: float | None = None
    credits: float | None = None
    latency_ms: int = 0

    def add(self, model: str | None, usage: Mapping[str, int]) -> None:
        if not any(int(usage.get(k, 0) or 0) for k in BUCKET_KEYS):
            return
        bucket = self.by_model.setdefault(model or "unknown", empty_bucket())
        add_bucket(bucket, usage)
        self.requests += 1

    @property
    def totals(self) -> UsageBucket:
        totals = empty_bucket()
        for bucket in self.by_model.values():
            add_bucket(totals, bucket)
        return totals

    @property
    def total_tokens(self) -> int:
        return bucket_total(self.totals)

    @property
    def has_usage(self) -> bool:
        return bool(self.by_model)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(
            model
            for model, _ in sorted(
                self.by_model.items(), key=lambda item: bucket_total(item[1]), reverse=True
            )
        )


def extract_usage(
    host: str,
    session_id: str,
    events: Sequence[NormalizedEvent],
    raw_dir: Path,
) -> UsageStats:
    """Return normalized per-model usage for one session."""
    stats = _from_normalized(events)
    if stats.has_usage:
        stats.source = "normalized"
        return stats
    stats = _from_raw(host, session_id, raw_dir)
    stats.source = "raw" if stats.has_usage else "none"
    return stats


# --- normalized events -------------------------------------------------------


def _from_normalized(events: Sequence[NormalizedEvent]) -> UsageStats:
    stats = UsageStats()
    current_model: str | None = None
    previous_cumulative: Mapping[str, int] | None = None
    final_cumulative: Mapping[str, int] | None = None

    for event in events:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            continue
        model = _context_model(payload)
        if model:
            current_model = model
        if _is_codex_token_count(payload):
            delta, cumulative = _codex_delta(payload, previous_cumulative)
            if cumulative is not None:
                previous_cumulative = cumulative
                final_cumulative = cumulative
            if delta:
                stats.add(current_model, delta)
            continue
        parsed = _usage_from_payload(payload)
        if parsed is not None:
            model_name, usage, extras = parsed
            stats.add(model_name or current_model, usage)
            _apply_extras(stats, extras)

    if not stats.has_usage and final_cumulative:
        stats.add(current_model, final_cumulative)
    return stats


def _context_model(payload: Mapping[str, Any]) -> str | None:
    if payload.get("type") == "turn_context" or "turn_id" in payload:
        model = payload.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _is_codex_token_count(payload: Mapping[str, Any]) -> bool:
    if payload.get("type") != "token_count":
        return False
    return isinstance(payload.get("info"), dict)


def _codex_delta(
    payload: Mapping[str, Any],
    previous: Mapping[str, int] | None,
) -> tuple[UsageBucket | None, Mapping[str, int] | None]:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None, previous
    cumulative_raw = info.get("total_token_usage")
    cumulative = _codex_raw(cumulative_raw) if isinstance(cumulative_raw, dict) else None
    last = info.get("last_token_usage")
    if isinstance(last, dict) and last:
        delta_raw = _codex_raw(last)
    elif cumulative is None:
        return None, previous
    elif previous is None:
        delta_raw = dict(cumulative)
    else:
        delta_raw = {key: max(0, cumulative.get(key, 0) - previous.get(key, 0)) for key in cumulative}
    return _codex_bucket(delta_raw), cumulative


def _codex_raw(usage: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            out[key] = int(value)
    return out


def _codex_bucket(usage: Mapping[str, int]) -> UsageBucket:
    cached = int(usage.get("cached_input_tokens", 0) or 0)
    bucket = empty_bucket()
    bucket["cache_read"] = cached
    bucket["input"] = max(0, int(usage.get("input_tokens", 0) or 0) - cached)
    bucket["output"] = int(usage.get("output_tokens", 0) or 0)
    bucket["reasoning"] = int(usage.get("reasoning_output_tokens", 0) or 0)
    return bucket


def _usage_from_payload(
    payload: Mapping[str, Any],
) -> tuple[str | None, UsageBucket, Mapping[str, Any]] | None:
    """Recognize the usage shapes any host may put in a normalized payload."""
    nested = payload.get("usage")
    if isinstance(nested, dict) and nested:
        model = _string(payload.get("model")) or _string(payload.get("modelId"))
        return model, _anthropic_bucket(nested), payload
    if _looks_like_copilot_usage(payload):
        return _string(payload.get("model")), _copilot_bucket(payload), payload
    if _looks_like_snapshot_usage(payload):
        return _string(payload.get("modelId")), _snapshot_bucket(payload), payload
    message = payload.get("message")
    if isinstance(message, dict):
        inner = message.get("usage")
        if isinstance(inner, dict) and inner:
            return _string(message.get("model")), _anthropic_bucket(inner), payload
    return None


def _apply_extras(stats: UsageStats, payload: Mapping[str, Any]) -> None:
    for key in ("request_multiplier", "copilotCredits"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            stats.credits = (stats.credits or 0.0) + float(value)
            break
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        stats.latency_ms += int(duration)
    for key in ("costUSD", "cost_usd"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            stats.embedded_cost_usd = (stats.embedded_cost_usd or 0.0) + float(value)
            break


def _looks_like_copilot_usage(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload.get("input_tokens"), (int, float)):
        return False
    return any(
        key in payload
        for key in ("cache_read_tokens", "cache_write_tokens", "total_nano_aiu", "request_multiplier")
    )


def _looks_like_snapshot_usage(payload: Mapping[str, Any]) -> bool:
    return isinstance(payload.get("promptTokens"), (int, float)) or isinstance(
        payload.get("completionTokens"), (int, float)
    )


def _anthropic_bucket(usage: Mapping[str, Any]) -> UsageBucket:
    bucket = empty_bucket()
    bucket["input"] = _int(usage.get("input_tokens"))
    bucket["output"] = _int(usage.get("output_tokens"))
    bucket["cache_read"] = _int(usage.get("cache_read_input_tokens")) or _int(
        usage.get("cached_input_tokens")
    )
    cache_create = _int(usage.get("cache_creation_input_tokens"))
    if not cache_create and isinstance(usage.get("cache_creation"), dict):
        cache_create = sum(_int(value) for value in usage["cache_creation"].values())
    bucket["cache_create"] = cache_create
    bucket["reasoning"] = _int(usage.get("reasoning_output_tokens"))
    return bucket


def _copilot_bucket(usage: Mapping[str, Any]) -> UsageBucket:
    bucket = empty_bucket()
    read = _int(usage.get("cache_read_tokens"))
    write = _int(usage.get("cache_write_tokens"))
    bucket["cache_read"] = read
    bucket["cache_create"] = write
    bucket["input"] = max(0, _int(usage.get("input_tokens")) - read - write)
    bucket["output"] = _int(usage.get("output_tokens"))
    bucket["reasoning"] = _int(usage.get("reasoning_tokens"))
    return bucket


def _snapshot_bucket(usage: Mapping[str, Any]) -> UsageBucket:
    bucket = empty_bucket()
    bucket["input"] = _int(usage.get("promptTokens"))
    bucket["output"] = _int(usage.get("completionTokens"))
    return bucket


# --- raw fallbacks -----------------------------------------------------------


def _from_raw(host: str, session_id: str, raw_dir: Path) -> UsageStats:
    if host == "claude-code":
        return _claude_raw(raw_dir)
    if host == "codex":
        return _codex_raw_file(raw_dir)
    if host == "vscode-copilot":
        return _copilot_raw(raw_dir)
    return UsageStats()


def _claude_raw(raw_dir: Path) -> UsageStats:
    stats = UsageStats()
    path = raw_dir / "transcript.jsonl"
    if not path.exists():
        return stats
    seen: set = set()
    for _, record in iter_jsonl(path):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        message_id = message.get("id")
        request_id = record.get("requestId") or record.get("request_id")
        if message_id and request_id:
            key = (message_id, request_id)
            if key in seen:
                continue
            seen.add(key)
        model = _string(message.get("model"))
        if model == "<synthetic>":
            continue
        stats.add(model, _anthropic_bucket(usage))
        for container in (record, message):
            for cost_key in ("costUSD", "cost_usd"):
                value = container.get(cost_key)
                if isinstance(value, (int, float)):
                    stats.embedded_cost_usd = (stats.embedded_cost_usd or 0.0) + float(value)
                    break
    return stats


def _codex_raw_file(raw_dir: Path) -> UsageStats:
    stats = UsageStats()
    path = raw_dir / "rollout.jsonl"
    if not path.exists():
        return stats
    current_model: str | None = None
    previous: Mapping[str, int] | None = None
    final: Mapping[str, int] | None = None
    for _, record in iter_jsonl(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = record
        model = _context_model(payload)
        if model:
            current_model = model
        if not _is_codex_token_count(payload):
            continue
        delta, cumulative = _codex_delta(payload, previous)
        if cumulative is not None:
            previous = cumulative
            final = cumulative
        if delta:
            stats.add(current_model, delta)
    if not stats.has_usage and final:
        stats.add(current_model, final)
    return stats


def _copilot_raw(raw_dir: Path) -> UsageStats:
    stats = UsageStats()
    store = _read_json(raw_dir / "sidecars" / "session-store.json")
    events = store.get("assistant_usage_events") if isinstance(store, dict) else None
    if isinstance(events, list) and events:
        for event in events:
            if not isinstance(event, dict):
                continue
            stats.add(_string(event.get("model")), _copilot_bucket(event))
            _apply_extras(stats, event)
        if stats.has_usage:
            return stats

    snapshot = _read_json(raw_dir / "session.snapshot.json")
    requests = snapshot.get("requests") if isinstance(snapshot, dict) else None
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        model = _string(request.get("modelId"))
        detailed = request.get("modelTotals")
        used_detailed = False
        if isinstance(detailed, dict):
            for detailed_model, usage in detailed.items():
                if not isinstance(usage, dict):
                    continue
                bucket = _snapshot_bucket(usage)
                if bucket_total(bucket):
                    used_detailed = True
                    stats.add(_string(detailed_model) or model, bucket)
        if not used_detailed:
            stats.add(model, _snapshot_bucket(request))
        credits = request.get("copilotCredits")
        if isinstance(credits, (int, float)):
            stats.credits = (stats.credits or 0.0) + float(credits)
    return stats


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def iter_models(stats: UsageStats, fallback: Iterable[str] = ()) -> tuple[str, ...]:
    models = tuple(model for model in stats.models if model != "unknown")
    if models:
        return models
    return tuple(dict.fromkeys(model for model in fallback if model))
