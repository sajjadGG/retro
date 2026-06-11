"""Build a static dashboard from rollout-memory artifacts.

Ships inside the `retro` package so `retro dashboard build` works from any
install, not just a source checkout. It treats rollout-memory/ as an artifact
store and emits a self-contained dashboard.

CLI:
    retro dashboard build [--mode auto|calculate|display] [--root PATH] [--out PATH]
    python -m retro.dashboard_build [--mode ...] [--artifact-root PATH] [--out PATH]

The artifact root defaults to `./rollout-memory`; override with
`--artifact-root` or the `RETRO_ARTIFACT_ROOT` env var. Output goes to
`./dashboard/` by default; override with `--out`.

Cost modes mirror ccusage:
  - auto:      use embedded costUSD when an event provides it, else compute.
  - calculate: always compute from token counts.
  - display:   only show embedded costUSD; leave None when missing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

# Defaults are cwd-relative; build() rebinds these from its arguments.
ARTIFACT_ROOT = Path.cwd() / "rollout-memory"
OUT_DIR = Path.cwd() / "dashboard"
DATA_DIR = OUT_DIR / "data"
PRICING_SNAPSHOT = Path(__file__).resolve().parent / "pricing" / "litellm-pricing.json"

COST_MODE_AUTO = "auto"
COST_MODE_CALCULATE = "calculate"
COST_MODE_DISPLAY = "display"
COST_MODES = (COST_MODE_AUTO, COST_MODE_CALCULATE, COST_MODE_DISPLAY)

# Rough, editable defaults. Rates are USD per 1M tokens and mirror the
# ccusage-style split between uncached input, cache creation, cache reads, and output.
DEFAULT_RATES = {
    "gpt-5": {"input": 1.25, "cache_create": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.1": {"input": 1.25, "cache_create": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.1-codex": {"input": 1.25, "cache_create": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.2": {"input": 1.75, "cache_create": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.2-codex": {"input": 1.75, "cache_create": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.3-codex": {"input": 1.75, "cache_create": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.4": {"input": 2.5, "cache_create": 2.5, "cache_read": 0.25, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cache_create": 0.75, "cache_read": 0.075, "output": 4.5},
    "gpt-5.5": {"input": 5.0, "cache_create": 5.0, "cache_read": 0.5, "output": 30.0},
    "claude-opus-4-7": {"input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.10, "output": 5.0},
    "claude-3-5-haiku": {"input": 0.8, "cache_create": 1.0, "cache_read": 0.08, "output": 4.0},
    "claude-3-opus": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
    "claude-3-sonnet": {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-3-haiku": {"input": 0.25, "cache_create": 0.30, "cache_read": 0.03, "output": 1.25},
    "unknown": {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0},
}
RATE_NOTE = "estimated from token logs and local model rates; not billing truth"


# --- pricing ----------------------------------------------------------------


class PricingMap:
    """Single source of truth for per-model rates.

    Prefers the bundled `retro/pricing/litellm-pricing.json` (LiteLLM-shape,
    USD per token). Falls back to `DEFAULT_RATES` (USD per million tokens) for
    any model not in the snapshot.
    """

    def __init__(self, snapshot: dict[str, Any], defaults: dict[str, dict[str, float]]):
        self.meta = snapshot.get("_meta", {}) if snapshot else {}
        self.snapshot = {k: v for k, v in (snapshot or {}).items() if k != "_meta"}
        self.defaults = defaults

    @classmethod
    def load(cls, snapshot_path: Path = PRICING_SNAPSHOT) -> PricingMap:
        snapshot: dict[str, Any] = {}
        if snapshot_path.exists():
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                snapshot = {}
        return cls(snapshot, DEFAULT_RATES)

    def rates_for(self, model: str | None) -> dict[str, float]:
        """Return rates in USD per **million** tokens for the given model.

        The returned dict has keys: input, output, cache_create, cache_read.
        Missing fields default to 0 (rather than crashing on unknown models).
        """
        if not model:
            return self.defaults["unknown"]
        # 1. Exact match in LiteLLM snapshot.
        entry = self.snapshot.get(model)
        if not entry:
            # 2. Substring match in snapshot (e.g. `gpt-5.2-2026-05-01` → `gpt-5.2`).
            for known in sorted(self.snapshot, key=len, reverse=True):
                if known != "unknown" and known in model:
                    entry = self.snapshot[known]
                    break
        if entry:
            return _rates_from_litellm(entry)
        # 3. Exact match in defaults table.
        if model in self.defaults:
            return self.defaults[model]
        # 4. Substring match in defaults.
        for known in sorted(self.defaults, key=len, reverse=True):
            if known != "unknown" and known in model:
                return self.defaults[known]
        return self.defaults["unknown"]

    def source_note(self) -> str:
        if self.snapshot:
            taken = self.meta.get("snapshot_taken", "unknown")
            return f"rates from LiteLLM snapshot (taken {taken}); fallback to local defaults"
        return RATE_NOTE


def _rates_from_litellm(entry: dict[str, Any]) -> dict[str, float]:
    """Convert a LiteLLM entry (USD/token) into our shape (USD per 1M tokens)."""

    def per_million(key: str, fallback_key: str | None = None) -> float:
        v = entry.get(key)
        if v is None and fallback_key is not None:
            v = entry.get(fallback_key)
        try:
            return float(v) * 1_000_000 if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    return {
        "input": per_million("input_cost_per_token"),
        "output": per_million("output_cost_per_token"),
        # LiteLLM names: cache_read_input_token_cost, cache_creation_input_token_cost.
        # When cache_create is missing (rare), fall back to input rate.
        "cache_read": per_million("cache_read_input_token_cost", "input_cost_per_token"),
        "cache_create": per_million("cache_creation_input_token_cost", "input_cost_per_token"),
    }


# --- main -------------------------------------------------------------------


def build(
    mode: str = COST_MODE_AUTO,
    artifact_root: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Build the dashboard; returns the path to the generated index.html.

    The module's path globals are rebound here because the collectors below
    read them — the constants are defaults, this is the entry point.
    """
    global ARTIFACT_ROOT, OUT_DIR, DATA_DIR
    if mode not in COST_MODES:
        raise ValueError(f"mode must be one of {COST_MODES}, got {mode!r}")
    if artifact_root is not None:
        ARTIFACT_ROOT = Path(artifact_root).expanduser().resolve()
    if out_dir is not None:
        OUT_DIR = Path(out_dir).expanduser().resolve()
        DATA_DIR = OUT_DIR / "data"
    pricing = PricingMap.load()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sessions = collect_sessions(pricing=pricing, cost_mode=mode)
    signal_readings_by_session, signal_aggregates = load_signal_data()
    attach_signals_to_sessions(sessions, signal_readings_by_session)

    # Load quest state
    quest_state_path = ARTIFACT_ROOT / "quests" / "state.json"
    quest_state = {}
    if quest_state_path.exists():
        try:
            quest_state = json.loads(quest_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    # Compute operator diagnostics profile
    from retro.analyzer import analyze_operator_portfolio
    operator_profile = analyze_operator_portfolio(sessions)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rate_note": pricing.source_note(),
        "rates_usd_per_million": DEFAULT_RATES,
        "pricing_meta": pricing.meta,
        "cost_mode": mode,
        "summary": summarize_portfolio(sessions),
        "memory": summarize_memory(sessions),
        "signals": signal_aggregates,
        "sessions": sessions,
        "quests": quest_state,
        "operator_profile": operator_profile,
    }
    data_path = DATA_DIR / "rollouts.json"
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = OUT_DIR / "index.html"
    index_path.write_text(render_html(payload), encoding="utf-8")
    print(f"wrote {data_path}")
    print(f"wrote {index_path}")
    print(f"  cost_mode={mode}; {pricing.source_note()}")
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=COST_MODES,
        default=os.environ.get("RETRO_COST_MODE", COST_MODE_AUTO),
        help="Cost calculation mode (default: %(default)s)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("RETRO_ARTIFACT_ROOT"),
        help="rollout-memory artifact root (default: ./rollout-memory)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output directory for index.html and data/ (default: ./dashboard)",
    )
    args = parser.parse_args()
    build(
        mode=args.mode,
        artifact_root=Path(args.artifact_root) if args.artifact_root else None,
        out_dir=Path(args.out) if args.out else None,
    )


# --- signals ----------------------------------------------------------------


def load_signal_data() -> tuple[dict[tuple[str, str], list[dict]], dict]:
    """Read rollout-memory/signals/{readings.jsonl,aggregates.json} if present."""
    signals_dir = ARTIFACT_ROOT / "signals"
    readings_path = signals_dir / "readings.jsonl"
    aggregates_path = signals_dir / "aggregates.json"
    readings_by_session: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if readings_path.exists():
        for line in readings_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            readings_by_session[(r["host"], r["session_id"])].append(r)
    aggregates: dict[str, Any] = {}
    if aggregates_path.exists():
        try:
            aggregates = json.loads(aggregates_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            aggregates = {}
    return readings_by_session, aggregates


def attach_signals_to_sessions(
    sessions: list[dict[str, Any]],
    readings_by_session: dict[tuple[str, str], list[dict]],
) -> None:
    for session in sessions:
        key = (session["host"], session["session_id"])
        readings = readings_by_session.get(key, [])
        session["signals"] = readings
        session["signals_index"] = {r["signal"]: r["value"] for r in readings}


def collect_sessions(
    pricing: PricingMap | None = None,
    cost_mode: str = COST_MODE_AUTO,
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    normalized_root = ARTIFACT_ROOT / "normalized"
    if not normalized_root.exists():
        return sessions
    if pricing is None:
        pricing = PricingMap.load()

    for normalized_path in sorted(normalized_root.glob("*/*.events.jsonl")):
        host = normalized_path.parent.name
        session_id = normalized_path.name[: -len(".events.jsonl")]
        events = read_jsonl(normalized_path)
        sessions.append(
            analyze_session(
                host,
                session_id,
                normalized_path,
                events,
                pricing=pricing,
                cost_mode=cost_mode,
            )
        )

    sessions.sort(key=lambda s: (s.get("first_ts") or "", s["host"], s["session_id"]), reverse=True)
    return sessions


def analyze_session(
    host: str,
    session_id: str,
    normalized_path: Path,
    events: list[dict[str, Any]],
    *,
    pricing: PricingMap,
    cost_mode: str = COST_MODE_AUTO,
) -> dict[str, Any]:
    event_counts = Counter(e.get("event_type", "unknown") for e in events)
    actor_counts = Counter(e.get("actor", "unknown") for e in events)
    tool_names = Counter()
    files_touched: set[str] = set()
    timestamps = [parse_ts(e.get("timestamp")) for e in events if e.get("timestamp")]
    timestamps = [t for t in timestamps if t is not None]

    command_count = 0
    failed_count = 0
    web_search_count = 0
    rate_limit_count = 0
    for event in events:
        etype = event.get("event_type")
        name = tool_name(event)
        if name:
            tool_names[name] += 1
        files_touched.update(extract_files(event))
        if etype == "command":
            command_count += 1
        if is_failed_event(event):
            failed_count += 1
        if "web_search" in (event.get("summary") or "") or name in {"WebSearch", "web_search"}:
            web_search_count += 1
        if _is_rate_limit_hit(event):
            rate_limit_count += 1

    first_ts = min(timestamps).isoformat() if timestamps else None
    last_ts = max(timestamps).isoformat() if timestamps else None
    duration_seconds = int((max(timestamps) - min(timestamps)).total_seconds()) if len(timestamps) >= 2 else None

    raw_meta = read_raw_meta(host, session_id)
    token_stats = extract_token_stats(events, host, session_id)
    provider = infer_provider(host, raw_meta, events)
    models = infer_models(host, session_id, raw_meta, events)
    if not token_stats.get("by_model") and any(
        token_stats.get(k, 0)
        for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cached_input_tokens")
    ):
        token_stats["by_model"] = {
            choose_cost_model(provider, host, models) or "unknown": _bucket_only(token_stats),
        }
    cost_breakdown = compute_cost(token_stats, provider, host, models, pricing, cost_mode)
    rendered_path = ARTIFACT_ROOT / "rendered" / host / f"{session_id}.md"
    mined = find_mined_artifacts(host, session_id)
    title = infer_title(events, raw_meta)

    project_slug = raw_meta.get("project_slug")
    cwd = raw_meta.get("cwd")
    project_name = "unknown"
    project_path = ""

    if project_slug:
        project_name = project_slug
        abs_paths = []
        for e in events:
            payload = e.get("payload") or {}
            paths = []
            if "file_path" in payload:
                paths.append(payload["file_path"])
            elif "input" in payload and isinstance(payload["input"], dict) and "file_path" in payload["input"]:
                paths.append(payload["input"]["file_path"])

            for p in paths:
                if isinstance(p, str) and p.startswith("/"):
                    abs_paths.append(p)

        if abs_paths:
            found_path = ""
            for p in abs_paths:
                parts = p.split("/")
                if project_slug in parts:
                    idx = parts.index(project_slug)
                    found_path = "/".join(parts[:idx + 1])
                    break
            if not found_path and abs_paths:
                found_path = str(Path(abs_paths[0]).parent)
            project_path = found_path
    elif cwd:
        project_path = cwd
        project_name = Path(cwd).name or cwd

    return {
        "host": host,
        "session_id": session_id,
        "title": title,
        "date": first_ts[:10] if first_ts else "unknown",
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_seconds": duration_seconds,
        "event_count": len(events),
        "event_counts": dict(sorted(event_counts.items())),
        "actor_counts": dict(sorted(actor_counts.items())),
        "user_messages": count_actor_type(events, "user", "message"),
        "assistant_messages": count_actor_type(events, "assistant", "message"),
        "reasoning_events": event_counts.get("reasoning", 0),
        "tool_call_events": event_counts.get("tool_call", 0),
        "tool_result_events": event_counts.get("tool_result", 0),
        "command_events": command_count,
        "failed_events": failed_count,
        "file_read_events": event_counts.get("file_read", 0),
        "file_edit_events": event_counts.get("file_edit", 0),
        "web_search_events": web_search_count,
        "unknown_events": event_counts.get("unknown", 0),
        "events_without_timestamps": sum(1 for e in events if not e.get("timestamp")),
        "unique_files_touched": len(files_touched),
        "files_touched": sorted(files_touched)[:100],
        "top_tools": [{"name": k, "count": v} for k, v in tool_names.most_common(12)],
        "tokens": token_stats,
        "tokens_by_model": token_stats.get("by_model") or {},
        "provider": provider,
        "models": models,
        "cost_model": choose_cost_model(provider, host, models),
        "estimated_cost_usd": cost_breakdown["total"],
        "cost_by_model": cost_breakdown["by_model"],
        "cost_categories": cost_breakdown.get(
            "categories",
            {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
        ),
        "rate_limit_hits": rate_limit_count,
        "cost_mode_used": cost_breakdown["mode_used"],
        "cost_source": cost_breakdown["source"],
        "cost_note": pricing.source_note(),
        "normalized_path": rel(normalized_path),
        "rendered_path": rel(rendered_path) if rendered_path.exists() else None,
        "rendered_markdown": read_text(rendered_path, limit=1_000_000) if rendered_path.exists() else "",
        "raw_dir": rel(ARTIFACT_ROOT / "raw" / host / session_id),
        "mined": mined,
        "project_name": project_name,
        "project_path": project_path,
    }


def summarize_portfolio(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    by_day = defaultdict(
        lambda: {
            "sessions": 0,
            "tokens": 0,
            "cost": 0.0,
            "tool_calls": 0,
            "edits": 0,
            "sessions_claude": 0,
            "sessions_codex": 0,
            "cost_input": 0.0,
            "cost_cache_create": 0.0,
            "cost_cache_read": 0.0,
            "cost_output": 0.0,
            "rate_limit_hits": 0,
        }
    )
    by_host = Counter(s["host"] for s in sessions)
    totals = Counter()
    total_cost = 0.0
    durations = []

    for session in sessions:
        day = session["date"]
        by_day[day]["sessions"] += 1
        if session["host"] == "claude-code":
            by_day[day]["sessions_claude"] += 1
        elif session["host"] == "codex":
            by_day[day]["sessions_codex"] += 1
        by_day[day]["tokens"] += session["tokens"].get("total_tokens", 0)
        by_day[day]["cost"] += session.get("estimated_cost_usd") or 0.0
        by_day[day]["tool_calls"] += session.get("tool_call_events", 0)
        by_day[day]["edits"] += session.get("file_edit_events", 0)
        by_day[day]["rate_limit_hits"] += session.get("rate_limit_hits", 0)

        # Accumulate daily breakdown
        cats = session.get("cost_categories") or {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
        by_day[day]["cost_input"] += cats.get("input", 0.0)
        by_day[day]["cost_cache_create"] += cats.get("cache_create", 0.0)
        by_day[day]["cost_cache_read"] += cats.get("cache_read", 0.0)
        by_day[day]["cost_output"] += cats.get("output", 0.0)

        totals["events"] += session["event_count"]
        totals["user_messages"] += session["user_messages"]
        totals["assistant_messages"] += session["assistant_messages"]
        totals["tool_calls"] += session["tool_call_events"]
        totals["tool_results"] += session["tool_result_events"]
        totals["commands"] += session["command_events"]
        totals["file_reads"] += session["file_read_events"]
        totals["file_edits"] += session["file_edit_events"]
        totals["errors"] += session["failed_events"]
        totals["unknown_events"] += session["unknown_events"]
        totals["tokens"] += session["tokens"].get("total_tokens", 0)
        total_cost += session.get("estimated_cost_usd") or 0.0
        totals["rate_limit_hits"] += session.get("rate_limit_hits", 0)

        # Accumulate global totals
        totals["cost_input"] += cats.get("input", 0.0)
        totals["cost_cache_create"] += cats.get("cache_create", 0.0)
        totals["cost_cache_read"] += cats.get("cache_read", 0.0)
        totals["cost_output"] += cats.get("output", 0.0)

        if session.get("duration_seconds") is not None:
            durations.append(session["duration_seconds"])

    # Round daily metrics
    sorted_by_day = {}
    for day, metrics in sorted(by_day.items()):
        metrics["cost"] = round(metrics["cost"], 6)
        metrics["cost_input"] = round(metrics["cost_input"], 6)
        metrics["cost_cache_create"] = round(metrics["cost_cache_create"], 6)
        metrics["cost_cache_read"] = round(metrics["cost_cache_read"], 6)
        metrics["cost_output"] = round(metrics["cost_output"], 6)
        sorted_by_day[day] = metrics

    proj_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "sessions": 0,
            "tokens": 0,
            "cost": 0.0,
            "path": "",
            "hosts": set(),
        }
    )
    for session in sessions:
        p_name = session.get("project_name") or "unknown"
        p_path = session.get("project_path") or ""
        proj_map[p_name]["sessions"] += 1
        proj_map[p_name]["tokens"] += session["tokens"].get("total_tokens", 0)
        proj_map[p_name]["cost"] += session.get("estimated_cost_usd") or 0.0
        proj_map[p_name]["hosts"].add(session["host"])
        if p_path:
            proj_map[p_name]["path"] = p_path

    projects_list = []
    for name, p_data in proj_map.items():
        projects_list.append({
            "name": name,
            "path": p_data["path"],
            "sessions": p_data["sessions"],
            "tokens": p_data["tokens"],
            "cost": round(p_data["cost"], 6),
            "hosts": sorted(list(p_data["hosts"])),
        })
    projects_list.sort(key=lambda p: p["sessions"], reverse=True)

    return {
        "session_count": len(sessions),
        "sessions_used_for_stats": len(sessions),
        "sessions_with_token_usage": sum(1 for s in sessions if s["tokens"].get("total_tokens", 0) > 0),
        "sessions_with_cost_estimate": sum(1 for s in sessions if s.get("estimated_cost_usd") is not None),
        "active_days": len([d for d in by_day if d != "unknown"]),
        "by_host": dict(by_host),
        "by_day": sorted_by_day,
        "totals": dict(totals),
        "estimated_cost_usd": round(total_cost, 6),
        "cost_categories": {
            "input": round(totals["cost_input"], 6),
            "cache_create": round(totals["cost_cache_create"], 6),
            "cache_read": round(totals["cost_cache_read"], 6),
            "output": round(totals["cost_output"], 6),
        },
        "avg_duration_seconds": int(sum(durations) / len(durations)) if durations else None,
        "avg_events_per_session": round(totals["events"] / len(sessions), 1) if sessions else 0,
        "avg_tool_calls_per_session": round(totals["tool_calls"] / len(sessions), 1) if sessions else 0,
        "projects": projects_list,
        "projects_count": len([p for p in projects_list if p["name"] != "unknown"]),
    }


def summarize_memory(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up mined memory across every imported session.

    Surfaces:
      - sessions_with_memory: how many sessions have been mined at least once.
      - methods_used: which mining methods produced output, with run counts.
      - by_kind: candidate kind histogram (skill / procedure / failure_trigger / ...).
      - by_method: per-method candidate count.
      - candidate_count: total candidates across all sessions × methods.
      - top_candidates: highest-priority/confidence candidates for the
        portfolio-level "Memory" panel.
    """
    by_method: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_scope: Counter[str] = Counter()
    by_method_kind: dict[str, Counter[str]] = defaultdict(Counter)
    top: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []  # ALL candidates, for the "All Memories" tab
    candidate_count = 0
    sessions_with_memory = 0
    method_session_counts: Counter[str] = Counter()

    for session in sessions:
        mined = session.get("mined") or []
        seen_method_for_session: set[str] = set()
        if not mined:
            continue
        sessions_with_memory += 1
        for entry in mined:
            method = entry.get("method") or "unknown"
            seen_method_for_session.add(method)
            for c in entry.get("candidates") or []:
                kind = c.get("kind") or "unknown"
                scope = c.get("scope") or "repo"
                by_method[method] += 1
                by_kind[kind] += 1
                by_scope[scope] += 1
                by_method_kind[method][kind] += 1
                candidate_count += 1
                # Flat record for the "All Memories" browser — includes session
                # context plus the full candidate detail so the dashboard
                # doesn't need to cross-reference back.
                record = {
                    "session_id": session["session_id"],
                    "host": session["host"],
                    "title": session.get("title") or session["session_id"],
                    "date": session.get("date"),
                    "method": method,
                    "kind": kind,
                    "scope": scope,
                    "scope_reason": c.get("scope_reason") or "",
                    "origin_repo": c.get("origin_repo"),
                    "text": c.get("text") or "",
                    "when_to_use": c.get("when_to_use") or "",
                    "confidence": c.get("confidence"),
                    "priority": c.get("priority"),
                    "risk": c.get("risk"),
                    "evidence_refs": c.get("evidence_refs") or [],
                    "structured": c.get("structured"),
                }
                all_candidates.append(record)
                top.append({k: record[k] for k in (
                    "session_id", "host", "title", "method", "kind", "scope",
                    "text", "when_to_use", "confidence", "priority", "risk",
                )})
        for method in seen_method_for_session:
            method_session_counts[method] += 1

    # Keep the prompt-ready preview small.
    top.sort(
        key=lambda c: (
            -(c.get("priority") or 0),
            -(c.get("confidence") or 0),
            c.get("kind") or "",
        )
    )

    return {
        "sessions_with_memory": sessions_with_memory,
        "candidate_count": candidate_count,
        "index": summarize_memory_index(),
        "by_method": dict(by_method.most_common()),
        "by_kind": dict(by_kind.most_common()),
        "by_scope": dict(by_scope.most_common()),
        "by_method_kind": {m: dict(c.most_common()) for m, c in by_method_kind.items()},
        "method_session_counts": dict(method_session_counts.most_common()),
        "top_candidates": top[:12],
        "all_candidates": all_candidates,  # full list for the "All Memories" tab
    }


def summarize_memory_index() -> dict[str, Any]:
    index_path = ARTIFACT_ROOT / "memories" / "index.sqlite"
    empty = {
        "available": False,
        "memory_count": 0,
        "by_kind": {},
        "by_scope": {},
        "by_status": {},
        "top_utility": [],
        "lifecycle": [],
    }
    if not index_path.exists():
        return empty
    con = sqlite3.connect(index_path)
    con.row_factory = sqlite3.Row
    try:
        return {
            "available": True,
            "memory_count": con.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
            "by_kind": _db_counts(con, "kind"),
            "by_scope": _db_counts(con, "scope"),
            "by_status": _db_counts(con, "status"),
            "top_utility": [
                dict(row)
                for row in con.execute(
                    """
                    SELECT id, kind, scope, status, q_value, hits, successes, failures, text
                    FROM memory
                    ORDER BY q_value DESC, hits DESC, priority DESC
                    LIMIT 10
                    """
                ).fetchall()
            ],
            "lifecycle": [
                dict(row)
                for row in con.execute(
                    """
                    SELECT event, COUNT(*) AS count
                    FROM memory_event
                    GROUP BY event
                    ORDER BY count DESC
                    """
                ).fetchall()
            ],
        }
    except sqlite3.DatabaseError:
        return empty
    finally:
        con.close()


def _db_counts(con: sqlite3.Connection, column: str) -> dict[str, int]:
    rows = con.execute(f"SELECT {column}, COUNT(*) AS count FROM memory GROUP BY {column}").fetchall()
    return {row[0]: row["count"] for row in rows}


_BUCKET_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _empty_bucket() -> dict[str, int]:
    return {k: 0 for k in _BUCKET_KEYS}


def _add_to_bucket(bucket: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in usage.items():
        if key in bucket and isinstance(value, (int, float)):
            bucket[key] += int(value)


def _bucket_only(stats: dict[str, Any]) -> dict[str, int]:
    return {k: int(stats.get(k, 0) or 0) for k in _BUCKET_KEYS}


def extract_token_stats(events: list[dict[str, Any]], host: str, session_id: str) -> dict[str, Any]:
    """Return totals + per-model breakdown for a session.

    Output shape:
        {
          # session totals (sum across models)
          "input_tokens", "output_tokens", "cache_creation_tokens",
          "cached_input_tokens", "reasoning_output_tokens", "total_tokens",
          # per-model breakdown
          "by_model": {"<model>": { ...bucket... }, ...},
          # informational
          "embedded_cost_usd": float | None,  # sum of any provider-embedded costUSD
        }
    """
    if host == "claude-code":
        return _extract_claude_token_stats(events, session_id)
    if host == "codex":
        return _extract_codex_token_stats(events, session_id)
    return {**_empty_bucket(), "by_model": {}, "embedded_cost_usd": None}


def _extract_claude_token_stats(events: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    """Walk the raw Claude transcript so we get per-message `usage` + `model`.

    Normalized events drop per-message `usage` to keep the event stream lean,
    so we read the raw transcript directly. The dedup key `(message.id,
    requestId)` matches ccusage's behavior — Claude can replay the same
    message across project files when sessions are continued.
    """
    raw_path = ARTIFACT_ROOT / "raw" / "claude-code" / session_id / "transcript.jsonl"
    totals = _empty_bucket()
    by_model: dict[str, dict[str, int]] = {}
    embedded_cost = 0.0
    embedded_count = 0
    if raw_path.exists():
        seen: set[tuple[str, str]] = set()
        for raw in read_jsonl(raw_path):
            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            if not usage:
                continue
            msg_id = message.get("id")
            req_id = raw.get("requestId") or raw.get("request_id")
            if msg_id and req_id:
                key = (msg_id, req_id)
                if key in seen:
                    continue
                seen.add(key)
            normalized = normalize_usage(usage)
            model = message.get("model") or "unknown"
            if model == "<synthetic>":
                continue
            _add_to_bucket(totals, normalized)
            bucket = by_model.setdefault(model, _empty_bucket())
            _add_to_bucket(bucket, normalized)
            # Claude future-proofing: pick up costUSD if Anthropic ever embeds it.
            for key in ("costUSD", "cost_usd"):
                v = raw.get(key)
                if v is None:
                    v = message.get(key)
                if isinstance(v, (int, float)):
                    embedded_cost += float(v)
                    embedded_count += 1
                    break
    _finalize_totals(totals, by_model)
    return {
        **totals,
        "by_model": by_model,
        "embedded_cost_usd": embedded_cost if embedded_count else None,
        "embedded_cost_events": embedded_count,
    }


def _extract_codex_token_stats(events: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    """Codex emits `token_count` events with cumulative `total_token_usage`.

    Recent Codex builds also include `last_token_usage` (per-turn delta) which
    we prefer. When absent we subtract previous cumulative ourselves. The
    active model comes from `turn_context.model`, which precedes each token
    event; we track it as we walk.
    """
    totals = _empty_bucket()
    by_model: dict[str, dict[str, int]] = {}
    current_model: str | None = None
    previous_cumulative: dict[str, int] | None = None

    for event in events:
        payload = event.get("payload") or {}
        # turn_context arrives as an attachment event in the normalized stream;
        # the payload still carries `model`.
        if isinstance(payload, dict) and "model" in payload and "turn_id" in payload:
            model = payload.get("model")
            if isinstance(model, str) and model:
                current_model = model
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        if not isinstance(info, dict) or not info:
            continue
        # Prefer the explicit per-turn delta if Codex provided it.
        last = info.get("last_token_usage")
        delta = normalize_usage(last) if isinstance(last, dict) and last else None
        cumulative = normalize_usage(info.get("total_token_usage") or {})
        if delta is None:
            if previous_cumulative is None:
                delta = dict(cumulative)
            else:
                delta = {
                    k: max(0, cumulative.get(k, 0) - previous_cumulative.get(k, 0))
                    for k in _BUCKET_KEYS
                }
        previous_cumulative = cumulative or previous_cumulative
        if not any(delta.get(k, 0) for k in _BUCKET_KEYS):
            continue
        model_key = current_model or "unknown"
        _add_to_bucket(totals, delta)
        bucket = by_model.setdefault(model_key, _empty_bucket())
        _add_to_bucket(bucket, delta)

    # Sanity: if we never saw a delta but the final cumulative is non-zero,
    # fall back to the cumulative (old Codex builds without last_token_usage).
    if (
        not any(totals.get(k, 0) for k in _BUCKET_KEYS)
        and previous_cumulative
        and any(previous_cumulative.get(k, 0) for k in _BUCKET_KEYS)
    ):
        _add_to_bucket(totals, previous_cumulative)
        model_key = current_model or "unknown"
        bucket = by_model.setdefault(model_key, _empty_bucket())
        _add_to_bucket(bucket, previous_cumulative)

    _finalize_totals(totals, by_model)
    return {**totals, "by_model": by_model, "embedded_cost_usd": None, "embedded_cost_events": 0}


def _finalize_totals(totals: dict[str, int], by_model: dict[str, dict[str, int]]) -> None:
    """Patch up `total_tokens` from the sum of buckets when the provider didn't set it."""

    def _bucket_total(b: dict[str, int]) -> int:
        return (
            b.get("input_tokens", 0)
            + b.get("output_tokens", 0)
            + b.get("cache_creation_tokens", 0)
            + b.get("cached_input_tokens", 0)
        )

    if totals.get("total_tokens", 0) <= 0:
        totals["total_tokens"] = _bucket_total(totals)
    for bucket in by_model.values():
        if bucket.get("total_tokens", 0) <= 0:
            bucket["total_tokens"] = _bucket_total(bucket)


def normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_read_input_tokens": "cached_input_tokens",
        "cache_creation_input_tokens": "cache_creation_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens",
    }
    out: dict[str, int] = {}
    for src, dest in mapping.items():
        value = usage.get(src)
        if isinstance(value, (int, float)):
            out[dest] = out.get(dest, 0) + int(value)
    cache_creation = usage.get("cache_creation")
    if "cache_creation_input_tokens" not in usage and isinstance(cache_creation, dict):
        for value in cache_creation.values():
            if isinstance(value, (int, float)):
                out["cache_creation_tokens"] = out.get("cache_creation_tokens", 0) + int(value)
    return out


def compute_cost(
    token_stats: dict[str, Any],
    provider: str,
    host: str,
    models: list[str],
    pricing: PricingMap,
    mode: str = COST_MODE_AUTO,
) -> dict[str, Any]:
    """Per-model cost calculation honoring auto / calculate / display modes.

    Returns:
        {
          "total": float | None,
          "by_model": {model: float},
          "categories": {"input": float, "cache_create": float, "cache_read": float, "output": float},
          "mode_used": "auto"|"calculate"|"display",
          "source": "embedded"|"calculated"|"empty"
        }
    """
    embedded = token_stats.get("embedded_cost_usd")
    by_model_tokens = token_stats.get("by_model") or {}

    # Helper to calculate calc-based category breakdown for scaling/display
    calc_categories = {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
    calc_total = 0.0
    if by_model_tokens:
        for model, bucket in by_model_tokens.items():
            rates = pricing.rates_for(model)
            details = _cost_one_model_detailed(bucket, rates, host)
            calc_total += details["total"]
            for k in calc_categories:
                calc_categories[k] += details[k]

    if mode == COST_MODE_DISPLAY:
        embedded_val = float(embedded) if isinstance(embedded, (int, float)) else None
        if embedded_val is not None:
            if calc_total > 0:
                ratio = embedded_val / calc_total
                categories = {k: round(v * ratio, 6) for k, v in calc_categories.items()}
            else:
                categories = {"input": round(embedded_val, 6), "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
        else:
            categories = {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
        return {
            "total": round(float(embedded), 6) if isinstance(embedded, (int, float)) else None,
            "by_model": {},
            "categories": categories,
            "mode_used": mode,
            "source": "embedded" if isinstance(embedded, (int, float)) else "empty",
        }

    if mode == COST_MODE_AUTO and isinstance(embedded, (int, float)):
        embedded_val = float(embedded)
        if calc_total > 0:
            ratio = embedded_val / calc_total
            categories = {k: round(v * ratio, 6) for k, v in calc_categories.items()}
        else:
            categories = {"input": round(embedded_val, 6), "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
        return {
            "total": round(embedded_val, 6),
            "by_model": {},
            "categories": categories,
            "mode_used": mode,
            "source": "embedded",
        }

    if not by_model_tokens:
        return {
            "total": None,
            "by_model": {},
            "categories": {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0},
            "mode_used": mode,
            "source": "empty",
        }

    cost_by_model: dict[str, float] = {}
    categories = {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 0.0}
    total = 0.0
    for model, bucket in by_model_tokens.items():
        rates = pricing.rates_for(model)
        details = _cost_one_model_detailed(bucket, rates, host)
        cost_by_model[model] = details["total"]
        total += details["total"]
        for k in categories:
            categories[k] += details[k]

    return {
        "total": round(total, 6),
        "by_model": cost_by_model,
        "categories": {k: round(v, 6) for k, v in categories.items()},
        "mode_used": mode,
        "source": "calculated",
    }


def _cost_one_model_detailed(bucket: dict[str, int], rates: dict[str, float], host: str) -> dict[str, float]:
    """Calculate detailed costs for single model by categories."""
    input_tokens = bucket.get("input_tokens", 0)
    cached_tokens = bucket.get("cached_input_tokens", 0)
    cache_create = bucket.get("cache_creation_tokens", 0)
    output_tokens = bucket.get("output_tokens", 0)
    if host == "codex":
        input_tokens = max(0, input_tokens - cached_tokens)

    input_cost = (input_tokens * rates.get("input", 0.0)) / 1_000_000
    cache_create_cost = (cache_create * rates.get("cache_create", 0.0)) / 1_000_000
    cache_read_cost = (cached_tokens * rates.get("cache_read", 0.0)) / 1_000_000
    output_cost = (output_tokens * rates.get("output", 0.0)) / 1_000_000
    total = input_cost + cache_create_cost + cache_read_cost + output_cost

    return {
        "input": input_cost,
        "cache_create": cache_create_cost,
        "cache_read": cache_read_cost,
        "output": output_cost,
        "total": total,
    }


def _cost_one_model(bucket: dict[str, int], rates: dict[str, float], host: str) -> float:
    """Apply rates to a single per-model token bucket.

    For Codex (OpenAI) `input_tokens` is the cumulative input INCLUDING cached
    reads, so we subtract `cached_input_tokens` before pricing — ccusage
    `adapter/codex.rs:475`. Anthropic's `input_tokens` is already fresh
    non-cached input (cache reads are a disjoint field), so we leave it alone.
    """
    input_tokens = bucket.get("input_tokens", 0)
    cached_tokens = bucket.get("cached_input_tokens", 0)
    cache_create = bucket.get("cache_creation_tokens", 0)
    output_tokens = bucket.get("output_tokens", 0)
    if host == "codex":
        input_tokens = max(0, input_tokens - cached_tokens)
    return (
        input_tokens * rates.get("input", 0.0)
        + cache_create * rates.get("cache_create", 0.0)
        + cached_tokens * rates.get("cache_read", 0.0)
        + output_tokens * rates.get("output", 0.0)
    ) / 1_000_000


def rates_for_model(model: str | None) -> dict[str, float]:
    """Compatibility shim: legacy callers still expect a direct lookup.

    New code should use `PricingMap.rates_for()` instead.
    """
    if not model:
        return DEFAULT_RATES["unknown"]
    if model in DEFAULT_RATES:
        return DEFAULT_RATES[model]
    for known in sorted(DEFAULT_RATES, key=len, reverse=True):
        if known != "unknown" and known in model:
            return DEFAULT_RATES[known]
    return DEFAULT_RATES["unknown"]


def choose_cost_model(provider: str, host: str, models: list[str]) -> str | None:
    if models:
        return models[0]
    if host == "codex" or provider == "openai":
        return "gpt-5"
    if host == "claude-code" or provider == "anthropic":
        return None
    return None


def infer_models(host: str, session_id: str, raw_meta: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    models = Counter()
    for key in ("model", "model_name"):
        value = raw_meta.get(key)
        if isinstance(value, str) and value:
            models[value] += 1

    raw_dir = ARTIFACT_ROOT / "raw" / host / session_id
    raw_path = raw_dir / ("transcript.jsonl" if host == "claude-code" else "rollout.jsonl")
    if host == "claude-code" and raw_path.exists():
        for raw in read_jsonl(raw_path):
            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            model = message.get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                models[model] += 1
    elif host == "codex":
        for event in events:
            model = codex_model_from_payload(event.get("payload") or {})
            if model:
                models[model] += 1
        if raw_path.exists():
            for raw in read_jsonl(raw_path):
                payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
                model = codex_model_from_payload(payload)
                if model:
                    models[model] += 1
    return [model for model, _ in models.most_common()]


def codex_model_from_payload(payload: dict[str, Any]) -> str | None:
    for candidate in (payload, payload.get("info"), payload.get("data"), payload.get("result"), payload.get("response")):
        if not isinstance(candidate, dict):
            continue
        for key in ("model", "model_name"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("model")
            if isinstance(value, str) and value:
                return value
    return None


def infer_provider(host: str, raw_meta: dict[str, Any], events: list[dict[str, Any]]) -> str:
    if raw_meta.get("model_provider"):
        provider = str(raw_meta["model_provider"]).lower()
        if "openai" in provider:
            return "openai"
        if "anthropic" in provider or "claude" in provider:
            return "anthropic"
    if host == "claude-code":
        return "anthropic"
    if host == "codex":
        return "openai"
    return "unknown"


def read_raw_meta(host: str, session_id: str) -> dict[str, Any]:
    raw_dir = ARTIFACT_ROOT / "raw" / host / session_id
    for name in ("thread.json", "import_meta.json"):
        path = raw_dir / name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def find_mined_artifacts(host: str, session_id: str) -> list[dict[str, Any]]:
    """Collect all mined artifacts (one per method) for this session.

    For each method, surface:
      - paths + the prompt-block text (for the Memory drill-down),
      - lightweight per-candidate previews (kind, confidence, priority, risk,
        evidence_refs, structured), so the dashboard can render structured
        cards and aggregate by kind/method across the portfolio.
    """
    mined_root = ARTIFACT_ROOT / "mined"
    out = []
    if not mined_root.exists():
        return out
    for json_path in sorted(mined_root.glob(f"*/{host}/{session_id}.json")):
        method = json_path.parents[1].name
        prompt_path = json_path.with_suffix(".prompt.md")
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        raw_candidates = data.get("candidates") or []
        candidates: list[dict[str, Any]] = []
        for c in raw_candidates:
            candidates.append(
                {
                    "id": c.get("id"),
                    "kind": c.get("kind"),
                    "text": c.get("text") or "",
                    "when_to_use": c.get("when_to_use") or "",
                    "confidence": c.get("confidence"),
                    "priority": c.get("priority"),
                    "risk": c.get("risk"),
                    "scope": c.get("scope") or "repo",
                    "scope_reason": c.get("scope_reason") or "",
                    "origin_repo": c.get("origin_repo"),
                    "evidence_refs": c.get("evidence_refs") or [],
                    "structured": c.get("structured"),
                }
            )
        out.append(
            {
                "method": method,
                "json_path": rel(json_path),
                "prompt_path": rel(prompt_path) if prompt_path.exists() else None,
                "candidate_count": len(candidates),
                "candidates": candidates,
                "filters_applied": data.get("filters_applied") or [],
                "notes": data.get("notes") or [],
                "prompt_text": read_text(prompt_path, limit=200_000) if prompt_path.exists() else "",
            }
        )
    return out


def tool_name(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") or {}
    for key in ("name", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    action = payload.get("action")
    if isinstance(action, dict) and isinstance(action.get("type"), str):
        return action["type"]
    return None


def extract_files(event: dict[str, Any]) -> set[str]:
    payload = event.get("payload") or {}
    files: set[str] = set()
    for candidate in walk_values(payload):
        if isinstance(candidate, str) and looks_like_path(candidate):
            files.add(candidate)
    changes = payload.get("changes")
    if isinstance(changes, dict):
        files.update(str(k) for k in changes.keys())
    return files


def walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)
    else:
        yield value


def looks_like_path(value: str) -> bool:
    return value.startswith("/") and len(value) < 300 and ("\n" not in value)


def is_failed_event(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error"}:
        return True
    output = payload.get("output")
    if isinstance(output, str) and '"exit_code":' in output:
        match = re.search(r'"exit_code"\s*:\s*(\d+)', output)
        if match and int(match.group(1)) != 0:
            return True
    return event.get("event_type") == "error"


def _is_rate_limit_hit(event: dict[str, Any]) -> bool:
    text = (event.get("summary") or "").lower()
    payload = event.get("payload") or {}
    for key in ("text", "message", "error", "output", "raw_content"):
        val = payload.get(key)
        if isinstance(val, str):
            text += " " + val.lower()
    phrases = [
        "rate limit",
        "reached the limit",
        "wait until",
        "quota exceeded",
        "exceeded quota",
        "exceeded your quota",
        "resource_exhausted",
        "resource exhausted",
        "throttled",
        "too many requests",
        "try again in",
    ]
    return any(p in text for p in phrases)


def infer_title(events: list[dict[str, Any]], raw_meta: dict[str, Any]) -> str:
    if raw_meta.get("title"):
        return str(raw_meta["title"]).strip().splitlines()[0][:180]
    for event in events:
        if event.get("actor") == "user" and event.get("event_type") == "message":
            return (event.get("summary") or "").strip().splitlines()[0][:180]
    return events[0].get("summary", "untitled")[:180] if events else "untitled"


def count_actor_type(events: list[dict[str, Any]], actor: str, event_type: str) -> int:
    return sum(1 for event in events if event.get("actor") == actor and event.get("event_type") == event_type)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_text(path: Path, *, limit: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n\n[dashboard truncated preview]"
    return text


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def render_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Rollout Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --line: #d8d7d0;
      --ink: #20211f;
      --muted: #686b63;
      --accent: #0f766e;
      --accent-soft: #d9efeb;
      /* Host colors: Claude = orange, Codex = blue. */
      --host-claude: #ea580c;
      --host-claude-soft: #ffedd5;
      --host-claude-line: #fdba74;
      --host-codex: #2563eb;
      --host-codex-soft: #dbeafe;
      --host-codex-line: #93c5fd;
      --warn: #b45309;
      --bad: #b91c1c;
      --cost-input: #2563eb;
      --cost-cache-create: #ea580c;
      --cost-cache-read: #eab308;
      --cost-output: #16a34a;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    header {{ padding: 24px 28px 16px; border-bottom: 1px solid var(--line); background: #fbfbf8; }}
    h1 {{ margin: 0 0 4px; font-size: 24px; font-weight: 720; letter-spacing: 0; }}
    .subtle {{ color: var(--muted); font-size: 13px; }}
    main {{ padding: 20px 28px 32px; display: grid; gap: 18px; }}
    .overview {{ display: grid; grid-template-columns: minmax(280px, 0.85fr) minmax(0, 1.15fr); gap: 14px; align-items: start; }}
    .insight-panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .insight-panel h2 {{ margin: 0; padding: 13px 14px; font-size: 15px; border-bottom: 1px solid var(--line); background: #fbfbf8; }}
    .insights {{ padding: 12px; display: grid; gap: 8px; }}
    .insight {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px 11px; background: #fff; }}
    .insight.attention {{ border-left: 3px solid var(--bad); }}
    .insight.good {{ border-left: 3px solid var(--accent); }}
    .insight.warn {{ border-left: 3px solid var(--warn); }}
    .insight .name {{ color: var(--muted); font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.03em; }}
    .insight .value {{ margin-top: 4px; font-size: 18px; font-weight: 760; }}
    .insight .meta {{ margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
    .kpi, .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .kpi {{ padding: 12px; min-height: 78px; }}
    .kpi .label {{ font-size: 12px; color: var(--muted); }}
    .kpi .value {{ margin-top: 6px; font-size: 23px; font-weight: 720; }}
    .kpi.sessions-total {{ border-top: 3px solid var(--accent); }}
    .session-split {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 9px; }}
    .session-split .item {{ border-radius: 6px; padding: 6px 7px; border: 1px solid var(--line); background: #fbfbf8; }}
    .session-split .item.claude-code {{ background: var(--host-claude-soft); border-color: var(--host-claude-line); }}
    .session-split .item.codex {{ background: var(--host-codex-soft); border-color: var(--host-codex-line); }}
    .session-split .name {{ display: block; color: var(--muted); font-size: 11px; }}
    .session-split .count {{ display: block; margin-top: 2px; font-weight: 760; }}
    .controls {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; background: #fbfbf8; border: 1px solid var(--line); border-radius: 8px; padding: 10px; position: sticky; top: 0; z-index: 20; }}
    input, select {{ border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; background: white; color: var(--ink); min-height: 38px; }}
    input {{ min-width: min(420px, 100%); flex: 1; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr); gap: 16px; align-items: start; }}
    .panel {{ overflow: hidden; }}
    .panel h2 {{ margin: 0; padding: 13px 14px; font-size: 15px; border-bottom: 1px solid var(--line); background: #fbfbf8; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 650; background: #fbfbf8; position: sticky; top: 0; }}
    td.num, th.num {{ text-align: right; }}
    td.title-cell {{ min-width: 220px; }}
    .title-main {{ font-weight: 650; line-height: 1.25; }}
    .title-sub {{ margin-top: 3px; color: var(--muted); font-size: 11px; }}
    tr {{ cursor: pointer; }}
    tr:hover, tr.selected {{ background: var(--accent-soft); }}
    .scroll {{ max-height: 560px; overflow: auto; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 600; background: #ecebe4; color: #353731; }}
    .badge.codex {{ background: var(--host-codex-soft); color: var(--host-codex); border: 1px solid var(--host-codex-line); }}
    .badge.claude-code {{ background: var(--host-claude-soft); color: var(--host-claude); border: 1px solid var(--host-claude-line); }}
    tr.host-codex td:first-child {{ box-shadow: inset 3px 0 0 var(--host-codex); }}
    tr.host-claude-code td:first-child {{ box-shadow: inset 3px 0 0 var(--host-claude); }}
    .kpi.host-codex {{ border-top: 3px solid var(--host-codex); }}
    .kpi.host-claude-code {{ border-top: 3px solid var(--host-claude); }}
    .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }}
    .dot.codex {{ background: var(--host-codex); }}
    .dot.claude-code {{ background: var(--host-claude); }}
    .stacked-bar {{ display: flex; height: 10px; border-radius: 999px; overflow: hidden; background: #e5e4dc; }}
    .stacked-bar .seg.codex {{ background: var(--host-codex); }}
    .stacked-bar .seg.claude-code {{ background: var(--host-claude); }}
    .legend {{ display: flex; gap: 14px; padding: 0 12px 8px; font-size: 12px; color: var(--muted); align-items: center; }}
    .metrics {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 9px; background: #fff; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ margin-top: 3px; font-weight: 720; }}
    .tabs {{ display: flex; gap: 6px; padding: 10px 12px 0; border-top: 1px solid var(--line); }}
    button {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; background: white; cursor: pointer; }}
    button.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    pre {{ margin: 0; padding: 12px; overflow: auto; max-height: 520px; white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.45; background: #111827; color: #f9fafb; }}
    .bars {{ padding: 12px; display: grid; gap: 8px; }}
    .barrow {{ display: grid; grid-template-columns: 92px 1fr 60px; gap: 8px; align-items: center; font-size: 12px; }}
    .bar {{ height: 8px; border-radius: 999px; background: #e5e4dc; overflow: hidden; }}
    .bar > span {{ display: block; height: 100%; background: var(--accent); }}
    .cost-stacked-bar {{ display: flex; height: 16px; border-radius: 4px; overflow: hidden; background: #e5e4dc; width: 100%; }}
    .cost-stacked-bar .seg {{ height: 100%; transition: opacity 0.15s ease; cursor: pointer; }}
    .cost-stacked-bar .seg:hover {{ opacity: 0.85; }}
    .heatmap-cell {{ transition: transform 0.15s ease, border-color 0.15s ease; }}
    .heatmap-cell:hover {{ transform: scale(1.15); border-color: #1e293b !important; z-index: 10; }}
    .signal-aggs {{ padding: 12px; display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }}
    .sigcard {{ border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; background: #fff; }}
    .sigcard .name {{ font-size: 12px; color: var(--muted); }}
    .sigcard .v {{ margin-top: 4px; font-weight: 720; }}
    .sigcard .sub {{ margin-top: 3px; color: var(--muted); font-size: 11px; }}
    .mem-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; padding: 12px; }}
    .mem-block {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fff; }}
    .mem-block h3 {{ margin: 0 0 8px; font-size: 13px; color: var(--muted); font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em; }}
    .mem-row {{ display: flex; justify-content: space-between; gap: 8px; font-size: 13px; padding: 3px 0; align-items: baseline; }}
    .mem-row .label {{ color: var(--muted); }}
    .mem-row .val {{ font-weight: 650; }}
    .mem-kind {{ display: inline-flex; align-items: center; border-radius: 4px; padding: 1px 6px; font-size: 11px; font-weight: 600; }}
    .mem-kind.skill {{ background: #e0e7ff; color: #3730a3; }}
    .mem-kind.procedure {{ background: #d1fae5; color: #065f46; }}
    .mem-kind.failure_trigger {{ background: #fee2e2; color: #991b1b; }}
    .mem-kind.tool_lesson {{ background: #fef3c7; color: #92400e; }}
    .mem-kind.risk_rule {{ background: #fde68a; color: #78350f; }}
    .mem-kind.user_preference {{ background: #ede9fe; color: #5b21b6; }}
    .mem-kind.repo_convention {{ background: #ccfbf1; color: #115e59; }}
    .mem-card {{ border: 1px solid var(--line); border-radius: 6px; padding: 11px 13px; margin: 8px 0; background: #fff; }}
    .mem-card .hdr {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 5px; }}
    .mem-card .when {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; font-style: italic; }}
    .mem-card .body {{ font-size: 13px; line-height: 1.4; }}
    .mem-card details {{ margin-top: 8px; font-size: 12px; }}
    .mem-card details summary {{ cursor: pointer; color: var(--muted); }}
    .mem-card details pre {{ margin: 6px 0 0; padding: 8px; background: #f7f7f4; color: var(--ink); border-radius: 4px; font-size: 11px; max-height: 240px; }}
    .mem-meta {{ font-size: 11px; color: var(--muted); }}
    .pill {{ display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; background: #eef0e9; color: #3f4339; }}
    .pill.risk-high {{ background: #fee2e2; color: #991b1b; }}
    .pill.risk-low {{ background: #dcfce7; color: #14532d; }}
    .pill.method-skill_pro {{ background: #e0e7ff; color: #3730a3; }}
    .pill.method-reme_refine_poc {{ background: #fef3c7; color: #92400e; }}
    .pill.method-memp_procedural {{ background: #d1fae5; color: #065f46; }}
    .pill.scope-user {{ background: #fef3c7; color: #92400e; }}
    .pill.scope-repo {{ background: #ddd6fe; color: #5b21b6; }}
    .pill.scope-task {{ background: #cffafe; color: #155e75; }}
    .pill.scope-global {{ background: #fce7f3; color: #9f1239; }}
    .pill.warn {{ background: #fef3c7; color: #92400e; }}
    .pill.bad {{ background: #fee2e2; color: #991b1b; }}
    .pill.good {{ background: #dcfce7; color: #14532d; }}
    .all-mem-controls {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 0 12px 10px; align-items: center; }}
    .all-mem-controls .count {{ margin-left: auto; color: var(--muted); font-size: 12px; }}
    .all-mem-list {{ padding: 0 12px 12px; max-height: 720px; overflow: auto; }}
    .scope-counts {{ display: flex; gap: 8px; padding: 0 12px 8px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }}
    .scope-counts span code {{ background: #f1f1eb; padding: 0 4px; border-radius: 3px; }}
    .group-activity {{ border-left: 3px solid #3b82f6; }}
    .group-outcome  {{ border-left: 3px solid #0f766e; }}
    .group-cost     {{ border-left: 3px solid #b45309; }}
    .group-risk     {{ border-left: 3px solid #b91c1c; }}
    .signal-table table {{ width: 100%; }}
    .signal-table td.value {{ font-weight: 720; }}
    @media (max-width: 960px) {{ .grid, .overview {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 680px) {{
      header {{ padding: 18px 16px 12px; }}
      main {{ padding: 14px 16px 24px; }}
      .controls {{ position: static; }}
      input {{ min-width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Rollout Dashboard</h1>
    <div class="subtle">Generated {escape(payload["generated_at"])}. cost_mode=<code>{escape(payload.get("cost_mode", "auto"))}</code>. {escape(payload["rate_note"])}.</div>
  </header>
  <main>
    <section class="overview">
      <section class="insight-panel">
        <h2>At A Glance</h2>
        <div class="insights" id="insights"></div>
      </section>
      <section class="kpis" id="kpis"></section>
    </section>

    <!-- Operator Quests & Streaks Section (Issue 8) -->
    <section class="panel" id="questsPanel" style="display:none; margin-bottom: 18px;">
      <h2>Operator Quests &amp; Progression Loop</h2>
      <div style="padding: 16px; display: grid; grid-template-columns: minmax(240px, 300px) 1fr; gap: 20px; flex-wrap: wrap; align-items: start;">
        <div style="background: linear-gradient(135deg, #0f766e 0%, #115e59 100%); color: white; border-radius: 8px; padding: 18px; display: flex; flex-direction: column; justify-content: space-between; min-height: 160px; box-shadow: 0 4px 10px rgba(15, 118, 110, 0.25); align-self: start;">
          <div>
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.85;">Current Rank</div>
            <div id="questUserLevel" style="font-size: 18px; font-weight: 800; margin: 4px 0 10px 0;">Novice Prompt Mechanic</div>
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.85;">Operator Progress</div>
            <div style="font-size: 26px; font-weight: 900; margin-top: 2px;" id="questXp">0 <span style="font-size: 14px; font-weight: 500; opacity: 0.85;">XP</span></div>
          </div>
          <div style="border-top: 1px solid rgba(255,255,255,0.25); padding-top: 10px; margin-top: 12px; display: flex; align-items: center; justify-content: space-between;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 24px;">🔥</span>
              <span id="questStreak" style="font-size: 15px; font-weight: 700;">0 Day Streak</span>
            </div>
            <span id="questStreakFreeze" style="font-size: 11px; background: rgba(255,255,255,0.2); padding: 2px 6px; border-radius: 4px;">Freezes: 0</span>
          </div>
        </div>
        <div>
          <h3 style="margin: 0 0 12px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;">Active Daily Quests</h3>
          <div id="questsList" style="display: grid; gap: 10px;"></div>
        </div>
      </div>
    </section>

    <!-- Operator Diagnostics & Mentorship Section (Issue 9) -->
    <section class="panel" id="operatorProfilePanel" style="margin-bottom: 18px;">
      <h2>Operator Diagnostics &amp; Mentorship</h2>
      <div style="padding: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 20px; flex-wrap: wrap;">
        <div>
          <h3 style="margin: 0 0 12px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;">Performance Indicators</h3>
          <div style="display: grid; gap: 6px;">
            <div class="mem-row"><span class="label">Average Turns per Session</span><span class="val" id="opAvgTurns">0</span></div>
            <div class="mem-row"><span class="label">Command Failure Rate</span><span class="val" id="opCmdFailure">0%</span></div>
            <div class="mem-row"><span class="label">Exploration vs Exploitation</span><span class="val" id="opExplorBalance">0.5 / 0.5</span></div>
            <div class="mem-row"><span class="label">Average Session Token Cost</span><span class="val" id="opAvgCost">$0.0000</span></div>
            <div class="mem-row"><span class="label">Operator Role Classification</span><span class="val" id="opRoleClass">General Software Engineer</span></div>
          </div>
        </div>
        <div>
          <h3 style="margin: 0 0 12px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;">Mentorship Recommendations</h3>
          <div id="opRecommendations" style="display: flex; flex-direction: column; gap: 8px;"></div>
        </div>
      </div>
    </section>

    <section class="panel" id="memoryPanel">
      <h2>Memory · Mined Across Portfolio</h2>
      <div class="legend">
        <span><span class="dot claude-code"></span> Claude Code</span>
        <span><span class="dot codex"></span> Codex</span>
      </div>
      <div class="mem-grid" id="memoryGrid"></div>
    </section>
    <section class="panel" id="allMemoriesPanel">
      <h2 id="allMemoriesTitle">All Memories · Full Detail</h2>
      <div class="scope-counts" id="scopeCounts"></div>
      <div class="all-mem-controls">
        <select id="amHost">
          <option value="all">All hosts</option>
          <option value="claude-code">Claude Code</option>
          <option value="codex">Codex</option>
        </select>
        <select id="amScope">
          <option value="all">All scopes</option>
          <option value="user">user</option>
          <option value="repo">repo</option>
          <option value="task">task</option>
          <option value="global">global</option>
        </select>
        <select id="amKind">
          <option value="all">All kinds</option>
        </select>
        <select id="amMethod">
          <option value="all">All methods</option>
        </select>
        <select id="amRisk">
          <option value="all">All risk</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select>
        <input id="amSearch" placeholder="Search memory text or activation…" />
        <span class="count" id="amCount"></span>
      </div>
      <div class="all-mem-list" id="allMemoriesList"></div>
    </section>
    <section class="panel" id="signalsPanel">
      <h2>Signals · Portfolio Aggregates</h2>
      <div class="signal-aggs" id="signalAggs"></div>
    </section>
    <section class="panel">
      <h2>Activity By Day · <span style="color:var(--host-claude)">Claude</span> + <span style="color:var(--host-codex)">Codex</span></h2>
      <div class="bars" id="dayBars"></div>
    </section>
    <section class="panel" id="accountingPanel">
      <h2>Accounting &amp; Costs</h2>
      <div class="legend">
        <span><span class="dot" style="background:var(--cost-input)"></span> Input</span>
        <span><span class="dot" style="background:var(--cost-cache-create)"></span> Cache Create</span>
        <span><span class="dot" style="background:var(--cost-cache-read)"></span> Cache Read</span>
        <span><span class="dot" style="background:var(--cost-output)"></span> Output</span>
      </div>
      <div class="metrics" style="grid-template-columns: repeat(4, 1fr); padding: 12px 12px 6px;">
        <div class="metric"><div class="label">Total Input Cost</div><div class="value" id="accInputCost">$0.0000</div></div>
        <div class="metric"><div class="label">Total Cache Create Cost</div><div class="value" id="accCacheCreateCost">$0.0000</div></div>
        <div class="metric"><div class="label">Total Cache Read Cost</div><div class="value" id="accCacheReadCost">$0.0000</div></div>
        <div class="metric"><div class="label">Total Output Cost</div><div class="value" id="accOutputCost">$0.0000</div></div>
      </div>
      <div class="scroll" style="max-height: 400px; padding: 0 12px 12px;">
        <table id="accountingTable">
          <thead>
            <tr>
              <th style="width: 120px;">Date</th>
              <th>Cost breakdown by category (Input / Cache Create / Cache Read / Output)</th>
              <th style="width: 100px; text-align: right;">Total Cost</th>
            </tr>
          </thead>
          <tbody id="accountingRows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel" id="projectsPanel">
      <h2>Projects Grouping</h2>
      <div class="scroll" style="max-height: 400px; padding: 0 12px 12px;">
        <table id="projectsTable">
          <thead>
            <tr>
              <th style="width: 200px;">Project Name</th>
              <th>Path / Source</th>
              <th style="width: 80px; text-align: right;">Sessions</th>
              <th style="width: 120px; text-align: right;">Hosts</th>
              <th style="width: 120px; text-align: right;">Total Tokens</th>
              <th style="width: 100px; text-align: right;">Total Cost</th>
            </tr>
          </thead>
          <tbody id="projectsRows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel" id="ceilingPanel">
      <h2>Ceiling Maximization &amp; Daily Quotas</h2>
      <div style="display: grid; grid-template-columns: 280px 1fr; gap: 20px; padding: 16px; flex-wrap: wrap;">
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; background: #fafaf9; border: 1px solid var(--line); border-radius: 8px; padding: 16px; position: relative;">
          <h3 style="margin: 0 0 12px; font-size: 13px; color: var(--muted); text-transform: uppercase;">Daily Utilization</h3>
          <svg width="180" height="180" viewBox="0 0 180 180" style="transform: rotate(-90deg);">
            <circle cx="90" cy="90" r="75" fill="transparent" stroke="#e5e7eb" stroke-width="12" />
            <circle cx="90" cy="90" r="55" fill="transparent" stroke="#e5e7eb" stroke-width="12" />
            <circle cx="90" cy="90" r="35" fill="transparent" stroke="#e5e7eb" stroke-width="12" />
            <circle id="ringTokens" cx="90" cy="90" r="75" fill="transparent" stroke="#f97316" stroke-width="12"
                    stroke-dasharray="471.2" stroke-dashoffset="471.2" stroke-linecap="round" style="transition: stroke-dashoffset 0.5s ease;" />
            <circle id="ringCost" cx="90" cy="90" r="55" fill="transparent" stroke="#10b981" stroke-width="12"
                    stroke-dasharray="345.6" stroke-dashoffset="345.6" stroke-linecap="round" style="transition: stroke-dashoffset 0.5s ease;" />
            <circle id="ringLimits" cx="90" cy="90" r="35" fill="transparent" stroke="#eab308" stroke-width="12"
                    stroke-dasharray="219.9" stroke-dashoffset="219.9" stroke-linecap="round" style="transition: stroke-dashoffset 0.5s ease;" />
          </svg>
          <div style="margin-top: 16px; font-size: 11px; color: var(--muted); display: grid; gap: 4px; width: 100%;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span><span class="dot" style="background:#f97316"></span> Tokens (1M target)</span>
              <b id="ringTokensText">0%</b>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span><span class="dot" style="background:#10b981"></span> Cost ($1.00 target)</span>
              <b id="ringCostText">0%</b>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span><span class="dot" style="background:#eab308"></span> Maximization (1 hit)</span>
              <b id="ringLimitsText">0%</b>
            </div>
          </div>
        </div>
        <div style="display: flex; flex-direction: column;">
          <h3 style="margin: 0 0 8px; font-size: 13px; color: var(--muted); text-transform: uppercase;">Quotas Heatmap (Last 35 Days)</h3>
          <div class="legend" style="padding: 0 0 8px;">
            <span><span style="display:inline-block; width:12px; height:12px; background:#f3f4f6; border:1px solid #d1d5db; border-radius:2px; vertical-align:middle; margin-right:4px;"></span> Unused</span>
            <span><span style="display:inline-block; width:12px; height:12px; background:#dcfce7; border:1px solid #86efac; border-radius:2px; vertical-align:middle; margin-right:4px;"></span> &lt; 50%</span>
            <span><span style="display:inline-block; width:12px; height:12px; background:#86efac; border:1px solid #4ade80; border-radius:2px; vertical-align:middle; margin-right:4px;"></span> 50-100%</span>
            <span><span style="display:inline-block; width:12px; height:12px; background:#22c55e; border:1px solid #16a34a; border-radius:2px; vertical-align:middle; margin-right:4px;"></span> Full Quota</span>
            <span><span style="display:inline-block; width:12px; height:12px; background:#fef08a; border:2px solid #eab308; box-shadow: 0 0 6px rgba(234, 179, 8, 0.4); border-radius:2px; vertical-align:middle; margin-right:4px;"></span> Maximized (Rate Limit Hit!)</span>
          </div>
          <div id="heatmapGrid" style="display: grid; grid-template-columns: repeat(7, 18px); gap: 6px; margin-top: 6px;"></div>
          <div id="heatmapDetail" style="margin-top: 16px; font-size: 12px; color: var(--muted); font-style: italic;">
            Click a day in the heatmap to view its quota rings.
          </div>
        </div>
      </div>
    </section>
    <section class="controls">
      <input id="search" placeholder="Search title, id, host, file..." />
      <select id="hostFilter">
        <option value="all">All hosts</option>
        <option value="codex">Codex</option>
        <option value="claude-code">Claude Code</option>
      </select>
      <select id="projectFilter">
        <option value="all">All projects</option>
      </select>
      <select id="riskFilter">
        <option value="all">All risk levels</option>
        <option value="secret">Secret risk</option>
        <option value="failed">Has failures</option>
        <option value="expensive">High cost</option>
      </select>
      <select id="memoryFilter">
        <option value="all">All memory states</option>
        <option value="with">Has mined memory</option>
        <option value="without">No mined memory</option>
      </select>
    </section>
    <section class="grid">
      <div class="panel">
        <h2 id="sessionsTitle">Sessions</h2>
        <div class="scroll">
          <table>
            <thead><tr><th>Host</th><th>Date</th><th>Project / Title</th><th class="num">Events</th><th class="num">Tools</th><th class="num">Edits</th><th class="num">Tokens</th><th class="num">Cost</th><th>Memory</th><th>Flags</th></tr></thead>
            <tbody id="sessionRows"></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2 id="detailTitle">Session Detail</h2>
        <div class="metrics" id="detailMetrics"></div>
        <div class="tabs">
          <button id="tabSummary" class="active">Summary</button>
          <button id="tabModels">Models</button>
          <button id="tabSignals">Signals</button>
          <button id="tabTranscript">Transcript</button>
          <button id="tabMemory">Memory</button>
        </div>
        <pre id="detailText">Select a session.</pre>
        <div id="detailHtml" style="display:none; padding: 12px; max-height: 620px; overflow:auto;"></div>
      </div>
    </section>
  </main>
  <script id="rollout-data" type="application/json">{data_json}</script>
  <script>
    const DATA = JSON.parse(document.getElementById('rollout-data').textContent);
    let selected = DATA.sessions[0] || null;
    let activeTab = 'summary';

    const fmt = new Intl.NumberFormat();
    const money = v => v == null ? 'n/a' : '$' + Number(v).toFixed(4);
    const dur = s => s == null ? 'n/a' : (s < 60 ? s + 's' : Math.floor(s/60) + 'm ' + (s%60) + 's');

    function kpi(label, value, klass='') {{
      return `<div class="kpi ${{klass}}"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`;
    }}

    function sessionKpi(total, claudeCount, codexCount) {{
      return `<div class="kpi sessions-total">
        <div class="label">Sessions</div>
        <div class="value">${{fmt.format(total)}}</div>
        <div class="session-split">
          <div class="item claude-code"><span class="name">Claude</span><span class="count">${{fmt.format(claudeCount)}}</span></div>
          <div class="item codex"><span class="name">Codex</span><span class="count">${{fmt.format(codexCount)}}</span></div>
        </div>
      </div>`;
    }}

    function hasMemory(s) {{
      return (s.mined || []).some(m => (m.candidate_count || 0) > 0);
    }}

    function highCostCutoff() {{
      const costs = DATA.sessions
        .map(s => s.estimated_cost_usd || 0)
        .filter(v => v > 0)
        .sort((a, b) => a - b);
      if (!costs.length) return 0;
      return costs[Math.max(0, Math.floor(costs.length * 0.8) - 1)];
    }}

    function renderInsights() {{
      const sessions = DATA.sessions || [];
      const cutoff = highCostCutoff();
      const risky = sessions.filter(s => s.signals_index?.secret_exposure_signal);
      const failed = sessions.filter(s => (s.failed_events || 0) > 0);
      const noMemory = sessions.filter(s => !hasMemory(s));
      const expensive = sessions
        .filter(s => (s.estimated_cost_usd || 0) >= cutoff && cutoff > 0)
        .sort((a, b) => (b.estimated_cost_usd || 0) - (a.estimated_cost_usd || 0));
      const recentDay = Object.entries(DATA.summary.by_day || {{}}).slice(-1)[0];
      const topProject = (DATA.summary.projects || [])[0];
      const insight = (name, value, meta, klass='') => `
        <div class="insight ${{klass}}">
          <div class="name">${{name}}</div>
          <div class="value">${{value}}</div>
          <div class="meta">${{meta}}</div>
        </div>`;

      document.getElementById('insights').innerHTML = [
        insight(
          'Needs review',
          `${{fmt.format(risky.length)}} secret-risk · ${{fmt.format(failed.length)}} failed`,
          risky.length || failed.length
            ? 'Use the risk filter below to inspect the sessions most likely to contain sensitive output, broken commands, or missing follow-up.'
            : 'No secret-risk or failed-event sessions in the current artifact set.',
          risky.length || failed.length ? 'attention' : 'good'
        ),
        insight(
          'Memory coverage',
          `${{fmt.format(sessions.length - noMemory.length)}} / ${{fmt.format(sessions.length)}} sessions`,
          noMemory.length
            ? `${{fmt.format(noMemory.length)}} sessions have no mined memory yet; filter for them when deciding what to mine next.`
            : 'Every imported session has mined memory candidates.',
          noMemory.length ? 'warn' : 'good'
        ),
        insight(
          'Cost focus',
          expensive.length ? `${{money(expensive[0].estimated_cost_usd)}} top session` : 'No cost data',
          expensive.length
            ? `${{escapeHtml(expensive[0].title || expensive[0].session_id).slice(0, 110)}}`
            : 'Rebuild after token usage is available to see the expensive tail.',
          expensive.length ? 'warn' : ''
        ),
        insight(
          'Latest activity',
          recentDay ? `${{recentDay[0]}} · ${{fmt.format(recentDay[1].sessions)}} sessions` : 'No activity',
          topProject
            ? `Most active project: ${{escapeHtml(topProject.name)}} (${{fmt.format(topProject.sessions)}} sessions).`
            : 'No project grouping detected yet.',
          ''
        ),
      ].join('');
    }}

    function renderKpis() {{
      const t = DATA.summary.totals;
      const byHost = DATA.summary.by_host || {{}};
      const claudeCount = byHost['claude-code'] || 0;
      const codexCount = byHost['codex'] || 0;
      const secretAgg = DATA.signals?.by_signal?.secret_exposure_signal || {{}};
      const mem = DATA.memory || {{}};
      document.getElementById('kpis').innerHTML = [
        sessionKpi(DATA.summary.sessions_used_for_stats || DATA.summary.session_count, claudeCount, codexCount),
        kpi('Memory candidates', fmt.format(mem.candidate_count || 0)),
        kpi('With token data', fmt.format(DATA.summary.sessions_with_token_usage || 0)),
        kpi('With cost estimate', fmt.format(DATA.summary.sessions_with_cost_estimate || 0)),
        kpi('Secret-risk sessions', fmt.format(secretAgg.true_count || 0)),
        kpi('Active days', fmt.format(DATA.summary.active_days)),
        kpi('Active projects', fmt.format(DATA.summary.projects?.length || 0)),
        kpi('File edits', fmt.format(t.file_edits || 0)),
        kpi('Tokens', fmt.format(t.tokens || 0)),
        kpi('Est. cost', money(DATA.summary.estimated_cost_usd)),
      ].join('');
    }}

    function renderDayBars() {{
      const days = Object.entries(DATA.summary.by_day || {{}});
      const max = Math.max(1, ...days.map(([,d]) => d.sessions));
      document.getElementById('dayBars').innerHTML = days.map(([day,d]) => {{
        const claude = d.sessions_claude || 0;
        const codex = d.sessions_codex || 0;
        const claudePct = 100 * claude / max;
        const codexPct = 100 * codex / max;
        return `<div class="barrow">
          <span>${{day}}</span>
          <div class="stacked-bar">
            <span class="seg claude-code" style="width:${{claudePct}}%"></span>
            <span class="seg codex" style="width:${{codexPct}}%"></span>
          </div>
          <span title="claude / codex">${{claude}}+${{codex}}</span>
        </div>`;
      }}).join('');
    }}

    function renderMemoryAggregates() {{
      const mem = DATA.memory || {{}};
      const index = mem.index || {{}};
      const grid = document.getElementById('memoryGrid');
      const panel = document.getElementById('memoryPanel');
      if (!mem.candidate_count && !index.memory_count) {{
        panel.style.display = 'none';
        return;
      }}
      const byKind = mem.by_kind || {{}};
      const byMethod = mem.by_method || {{}};
      const methodSessions = mem.method_session_counts || {{}};
      const top = mem.top_candidates || [];
      const blocks = [];

      // Block 1: overview counts.
      blocks.push(`<div class="mem-block">
        <h3>Overview</h3>
        <div class="mem-row"><span class="label">Sessions with mined memory</span><span class="val">${{fmt.format(mem.sessions_with_memory || 0)}}</span></div>
        <div class="mem-row"><span class="label">Total candidates</span><span class="val">${{fmt.format(mem.candidate_count || 0)}}</span></div>
        <div class="mem-row"><span class="label">Indexed memories</span><span class="val">${{fmt.format(index.memory_count || 0)}}</span></div>
      </div>`);

      if (index.available) {{
        const statusRows = Object.entries(index.by_status || {{}}).map(([status, n]) => `
          <div class="mem-row"><span class="label">${{status}}</span><span class="val">${{fmt.format(n)}}</span></div>
        `).join('');
        const scopeRows = Object.entries(index.by_scope || {{}}).map(([scope, n]) => `
          <div class="mem-row"><span class="label">${{scope}}</span><span class="val">${{fmt.format(n)}}</span></div>
        `).join('');
        blocks.push(`<div class="mem-block"><h3>SQLite index</h3>${{statusRows || '<div class="mem-meta">no statuses</div>'}}<div style="height:8px"></div>${{scopeRows}}</div>`);
      }}

      // Block 2: by method.
      const methodRows = Object.entries(byMethod).map(([name, n]) => `
        <div class="mem-row">
          <span class="label"><span class="pill method-${{name}}">${{name}}</span> across ${{methodSessions[name] || 0}} session(s)</span>
          <span class="val">${{fmt.format(n)}}</span>
        </div>
      `).join('');
      blocks.push(`<div class="mem-block"><h3>By method</h3>${{methodRows || '<div class="mem-meta">no data</div>'}}</div>`);

      // Block 3: by kind.
      const kindMax = Math.max(1, ...Object.values(byKind));
      const kindRows = Object.entries(byKind).map(([kind, n]) => `
        <div class="mem-row">
          <span class="label"><span class="mem-kind ${{kind}}">${{kind}}</span></span>
          <span class="val">${{fmt.format(n)}}</span>
        </div>
        <div class="bar"><span style="width:${{100*n/kindMax}}%; background: var(--accent);"></span></div>
      `).join('');
      blocks.push(`<div class="mem-block"><h3>By kind</h3>${{kindRows || '<div class="mem-meta">no data</div>'}}</div>`);

      // Block 4: top candidates.
      const topRows = top.slice(0, 6).map(c => `
        <div style="margin-top:6px; padding:6px 0; border-top: 1px solid var(--line);">
          <div class="mem-row">
            <span class="label">
              <span class="mem-kind ${{c.kind}}">${{c.kind}}</span>
              <span class="pill method-${{c.method}}" style="margin-left:4px">${{c.method}}</span>
            </span>
            <span class="mem-meta">p${{c.priority}} · ${{Number(c.confidence).toFixed(2)}}</span>
          </div>
          <div class="mem-meta" style="margin-top:3px">${{escapeHtml(c.text).slice(0, 140)}}</div>
          <div class="mem-meta"><span class="badge ${{c.host}}">${{c.host}}</span> ${{escapeHtml(c.title || '').slice(0, 80)}}</div>
        </div>
      `).join('');
      blocks.push(`<div class="mem-block" style="grid-column: span 2;"><h3>Top candidates</h3>${{topRows || '<div class="mem-meta">none</div>'}}</div>`);

      if (index.available) {{
        const utilityRows = (index.top_utility || []).slice(0, 6).map(c => `
          <div style="margin-top:6px; padding:6px 0; border-top: 1px solid var(--line);">
            <div class="mem-row">
              <span class="label"><span class="mem-kind ${{c.kind}}">${{c.kind}}</span> ${{escapeHtml(c.id)}}</span>
              <span class="mem-meta">q=${{Number(c.q_value || 0).toFixed(2)}} · hits=${{c.hits || 0}}</span>
            </div>
            <div class="mem-meta" style="margin-top:3px">${{escapeHtml(c.text).slice(0, 140)}}</div>
          </div>
        `).join('');
        const lifecycleRows = (index.lifecycle || []).map(e => `
          <div class="mem-row"><span class="label">${{e.event}}</span><span class="val">${{fmt.format(e.count || 0)}}</span></div>
        `).join('');
        blocks.push(`<div class="mem-block" style="grid-column: span 2;"><h3>Top utility</h3>${{utilityRows || '<div class="mem-meta">none</div>'}}</div>`);
        blocks.push(`<div class="mem-block"><h3>Lifecycle</h3>${{lifecycleRows || '<div class="mem-meta">no events</div>'}}</div>`);
      }}

      grid.innerHTML = blocks.join('');
    }}

    function escapeHtml(s) {{
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    function renderSignalAggregates() {{
      const wrap = document.getElementById('signalAggs');
      const agg = (DATA.signals && DATA.signals.by_signal) || {{}};
      const names = Object.keys(agg).sort();
      if (!names.length) {{
        document.getElementById('signalsPanel').style.display = 'none';
        return;
      }}
      wrap.innerHTML = names.map(name => {{
        const a = agg[name];
        const klass = 'sigcard group-' + (a.group || 'activity');
        let value = '—';
        let sub = `${{a.sessions_with_reading || 0}} session(s)`;
        if (a.kind === 'numeric' && a.mean !== undefined) {{
          value = 'mean ' + (Number.isInteger(a.mean) ? a.mean : a.mean.toFixed(2));
          sub = `median ${{a.median}} · max ${{a.max}} · n=${{a.sessions_with_reading}}`;
        }} else if (a.kind === 'boolean') {{
          value = `${{a.true_count}}/${{a.true_count + a.false_count}} true`;
          sub = `ratio ${{a.true_ratio == null ? 'n/a' : a.true_ratio}}`;
        }} else if (a.kind === 'categorical' && a.histogram) {{
          const top = Object.entries(a.histogram).slice(0,3).map(([k,v]) => `${{k}}=${{v}}`).join(', ');
          value = top || '—';
          sub = `n=${{a.sessions_with_reading}}`;
        }}
        return `<div class="${{klass}}">
          <div class="name">${{name}} <span class="badge" style="background:#ecebe4">${{a.group}}</span></div>
          <div class="v">${{value}}</div>
          <div class="sub">${{sub}} · ${{a.unit || a.kind}}</div>
        </div>`;
      }}).join('');
    }}

    function filteredSessions() {{
      const q = document.getElementById('search').value.toLowerCase();
      const host = document.getElementById('hostFilter').value;
      const proj = document.getElementById('projectFilter').value;
      const risk = document.getElementById('riskFilter').value;
      const memory = document.getElementById('memoryFilter').value;
      const cutoff = highCostCutoff();
      return DATA.sessions.filter(s => {{
        if (host !== 'all' && s.host !== host) return false;
        if (proj !== 'all' && (s.project_name || 'unknown') !== proj) return false;
        if (risk === 'secret' && !s.signals_index?.secret_exposure_signal) return false;
        if (risk === 'failed' && !(s.failed_events > 0)) return false;
        if (risk === 'expensive' && !((s.estimated_cost_usd || 0) >= cutoff && cutoff > 0)) return false;
        if (memory === 'with' && !hasMemory(s)) return false;
        if (memory === 'without' && hasMemory(s)) return false;
        const hay = [s.host, s.session_id, s.title, s.project_name || '', ...(s.files_touched || [])].join(' ').toLowerCase();
        return hay.includes(q);
      }});
    }}

    function memorySummary(s) {{
      const mined = s.mined || [];
      if (!mined.length) return '<span class="mem-meta">—</span>';
      const totalCands = mined.reduce((acc, m) => acc + (m.candidate_count || 0), 0);
      const methods = mined.length;
      return `<span class="mem-meta">${{fmt.format(totalCands)}}c / ${{methods}}m</span>`;
    }}

    function sessionFlags(s) {{
      const flags = [];
      if (s.signals_index?.secret_exposure_signal) flags.push('<span class="pill bad">secret</span>');
      if ((s.failed_events || 0) > 0) flags.push(`<span class="pill warn">${{fmt.format(s.failed_events)}} fail</span>`);
      if (!hasMemory(s)) flags.push('<span class="pill">no memory</span>');
      if ((s.rate_limit_hits || 0) > 0) flags.push('<span class="pill warn">limit</span>');
      return flags.join(' ') || '<span class="mem-meta">—</span>';
    }}

    function renderRows() {{
      const rows = filteredSessions();
      document.getElementById('sessionsTitle').textContent =
        `Sessions · showing ${{fmt.format(rows.length)}} of ${{fmt.format(DATA.summary.session_count)}}`;
      document.getElementById('sessionRows').innerHTML = rows.map(s => {{
        const hostClass = `host-${{s.host}}`;
        const isSelected = selected && selected.session_id === s.session_id && selected.host === s.host;
        return `
        <tr data-id="${{s.host}}/${{s.session_id}}" class="${{hostClass}} ${{isSelected ? 'selected' : ''}}">
          <td><span class="badge ${{s.host}}">${{s.host}}</span></td>
          <td>${{s.date}}</td>
          <td class="title-cell">
            <div class="title-main">${{escapeHtml(s.title || s.session_id)}}</div>
            <div class="title-sub">${{escapeHtml(s.project_name || 'unknown')}} · <code>${{escapeHtml(String(s.session_id).slice(0, 12))}}</code></div>
          </td>
          <td class="num">${{fmt.format(s.event_count)}}</td>
          <td class="num">${{fmt.format(s.tool_call_events + s.tool_result_events)}}</td>
          <td class="num">${{fmt.format(s.file_edit_events)}}</td>
          <td class="num">${{fmt.format(s.tokens.total_tokens || 0)}}</td>
          <td class="num">${{money(s.estimated_cost_usd)}}</td>
          <td>${{memorySummary(s)}}</td>
          <td>${{sessionFlags(s)}}</td>
        </tr>
      `;}}).join('');
      document.querySelectorAll('#sessionRows tr').forEach(tr => tr.addEventListener('click', () => {{
        const [host, id] = tr.dataset.id.split('/');
        selected = DATA.sessions.find(s => s.host === host && s.session_id === id);
        activeTab = 'summary';
        renderAll();
      }}));
    }}

    function renderDetail() {{
      if (!selected) return;
      document.getElementById('detailTitle').textContent = selected.title || selected.session_id;
      const modelCount = Object.keys(selected.tokens_by_model || {{}}).length;
      const costLabel = modelCount > 1
        ? `${{money(selected.estimated_cost_usd)}} · ${{modelCount}} models`
        : money(selected.estimated_cost_usd);
      const costSource = selected.cost_source === 'embedded'
        ? 'embedded'
        : selected.cost_source === 'calculated' ? 'calculated' : 'n/a';
      document.getElementById('detailMetrics').innerHTML = [
        metric('Host', selected.host),
        metric('Duration', dur(selected.duration_seconds)),
        metric('Events', fmt.format(selected.event_count)),
        metric('Commands', fmt.format(selected.command_events)),
        metric('File edits', fmt.format(selected.file_edit_events)),
        metric('Files touched', fmt.format(selected.unique_files_touched)),
        metric('Tokens', fmt.format(selected.tokens.total_tokens || 0)),
        metric('Est. cost', costLabel),
        metric('Cost source', costSource),
        metric('Secret risk', selected.signals_index?.secret_exposure_signal ? 'yes' : 'no'),
        metric('Cost model', selected.cost_model || 'unknown'),
      ].join('');
      document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
      document.getElementById('tab' + activeTab[0].toUpperCase() + activeTab.slice(1)).classList.add('active');
      // Memory tab uses structured HTML cards; everything else stays in the
      // monospaced <pre>.
      const textEl = document.getElementById('detailText');
      const htmlEl = document.getElementById('detailHtml');
      if (activeTab === 'memory') {{
        textEl.style.display = 'none';
        htmlEl.style.display = 'block';
        htmlEl.innerHTML = memoryHtml(selected);
      }} else {{
        textEl.style.display = '';
        htmlEl.style.display = 'none';
        textEl.textContent = detailText(selected);
      }}
    }}

    function memoryHtml(s) {{
      const mined = s.mined || [];
      if (!mined.length) {{
        return '<div class="mem-meta">No mined memory found for this session. Run `retro mine ' + s.host + ' ' + s.session_id + ' --method all`.</div>';
      }}
      const sections = mined.map(entry => {{
        const cards = (entry.candidates || []).map(c => renderMemCard(c, {{showMethod: false, showSession: false}})).join('');
        const filters = (entry.filters_applied || []).length ? `<span class="mem-meta">filters: ${{escapeHtml(entry.filters_applied.join(', '))}}</span>` : '';
        return `<div class="mem-block" style="margin-bottom:12px">
          <h3 style="display:flex; gap:8px; align-items:center;">
            <span class="pill method-${{entry.method}}">${{entry.method}}</span>
            <span class="mem-meta">${{entry.candidate_count}} candidate(s)</span>
            ${{filters}}
          </h3>
          ${{cards || '<div class="mem-meta">no candidates</div>'}}
          <details><summary>raw prompt block</summary><pre style="white-space:pre-wrap; font-size:11px;">${{escapeHtml(entry.prompt_text || '')}}</pre></details>
        </div>`;
      }}).join('');
      return sections;
    }}

    // Shared card renderer used by the per-session drill-down and the
    // global "All Memories" browser. `opts.showMethod` includes a method
    // pill in the header; `opts.showSession` adds session/host/title context.
    function renderMemCard(c, opts) {{
      opts = opts || {{}};
      const conf = c.confidence != null ? Number(c.confidence).toFixed(2) : 'n/a';
      const riskClass = c.risk === 'high' ? 'risk-high' : c.risk === 'low' ? 'risk-low' : '';
      const scope = c.scope || 'repo';
      const scopeReason = c.scope_reason || 'default';
      const structuredHtml = c.structured ? renderStructured(c.kind, c.structured) : '';
      const evidence = (c.evidence_refs || []).slice(0, 4).map(e => `<code>${{escapeHtml(e).slice(0,12)}}…</code>`).join(' ');
      const origin = c.origin_repo ? `<span class="mem-meta">origin: <code>${{escapeHtml(c.origin_repo)}}</code></span>` : '';
      const methodPill = opts.showMethod && c.method ? `<span class="pill method-${{c.method}}">${{c.method}}</span>` : '';
      const sessionLine = opts.showSession ? `<div class="mem-meta" style="margin-top:6px">
        From <span class="badge ${{c.host}}">${{c.host}}</span>
        <code>${{escapeHtml(String(c.session_id || '').slice(0,12))}}…</code>
        ${{c.title ? '· ' + escapeHtml(c.title).slice(0,80) : ''}}
      </div>` : '';
      return `<div class="mem-card">
        <div class="hdr">
          <span class="mem-kind ${{c.kind}}">${{c.kind}}</span>
          ${{methodPill}}
          <span class="pill scope-${{scope}}" title="${{escapeHtml(scopeReason)}}">scope: ${{scope}}</span>
          <span class="pill ${{riskClass}}">risk: ${{c.risk || 'medium'}}</span>
          <span class="pill">priority: ${{c.priority}}</span>
          <span class="pill">confidence: ${{conf}}</span>
        </div>
        <div class="body"><b>${{escapeHtml(c.text)}}</b></div>
        ${{c.when_to_use ? `<div class="when">When to use: ${{escapeHtml(c.when_to_use)}}</div>` : ''}}
        ${{structuredHtml}}
        <div class="mem-meta" style="margin-top:6px">
          ${{origin}}
          ${{scopeReason ? '<span class="mem-meta"> · scope reason: ' + escapeHtml(scopeReason) + '</span>' : ''}}
        </div>
        ${{evidence ? `<div class="mem-meta" style="margin-top:6px">Evidence: ${{evidence}}</div>` : ''}}
        ${{sessionLine}}
      </div>`;
    }}

    // ---- All Memories browser --------------------------------------------

    function initAllMemoriesFilters() {{
      const mem = DATA.memory || {{}};
      const all = mem.all_candidates || [];
      const kinds = [...new Set(all.map(c => c.kind))].sort();
      const methods = [...new Set(all.map(c => c.method))].sort();
      const kindSel = document.getElementById('amKind');
      const methodSel = document.getElementById('amMethod');
      kinds.forEach(k => {{
        const o = document.createElement('option');
        o.value = k; o.textContent = k;
        kindSel.appendChild(o);
      }});
      methods.forEach(m => {{
        const o = document.createElement('option');
        o.value = m; o.textContent = m;
        methodSel.appendChild(o);
      }});
      ['amHost', 'amScope', 'amKind', 'amMethod', 'amRisk'].forEach(id => {{
        document.getElementById(id).addEventListener('change', renderAllMemories);
      }});
      document.getElementById('amSearch').addEventListener('input', renderAllMemories);
    }}

    function renderAllMemories() {{
      const mem = DATA.memory || {{}};
      const all = mem.all_candidates || [];
      const panel = document.getElementById('allMemoriesPanel');
      if (!all.length) {{
        panel.style.display = 'none';
        return;
      }}

      // Scope counts summary line (always reflects the full corpus, not filters).
      const byScope = mem.by_scope || {{}};
      document.getElementById('scopeCounts').innerHTML =
        '<span><b>By scope:</b></span>' +
        ['user', 'repo', 'task', 'global'].map(s =>
          `<span><span class="pill scope-${{s}}">${{s}}</span> <code>${{fmt.format(byScope[s] || 0)}}</code></span>`
        ).join('');

      const host = document.getElementById('amHost').value;
      const scope = document.getElementById('amScope').value;
      const kind = document.getElementById('amKind').value;
      const method = document.getElementById('amMethod').value;
      const risk = document.getElementById('amRisk').value;
      const q = document.getElementById('amSearch').value.toLowerCase();

      const filtered = all.filter(c => {{
        if (host !== 'all' && c.host !== host) return false;
        if (scope !== 'all' && c.scope !== scope) return false;
        if (kind !== 'all' && c.kind !== kind) return false;
        if (method !== 'all' && c.method !== method) return false;
        if (risk !== 'all' && c.risk !== risk) return false;
        if (q) {{
          const hay = [c.text, c.when_to_use, c.title, c.origin_repo].filter(Boolean).join(' ').toLowerCase();
          if (!hay.includes(q)) return false;
        }}
        return true;
      }});

      // Stable sort: highest priority/confidence first, then by scope (user > global > task > repo).
      const scopeOrder = {{user: 0, global: 1, task: 2, repo: 3}};
      filtered.sort((a, b) =>
        (b.priority || 0) - (a.priority || 0)
        || (b.confidence || 0) - (a.confidence || 0)
        || (scopeOrder[a.scope] ?? 9) - (scopeOrder[b.scope] ?? 9)
      );

      document.getElementById('amCount').textContent =
        `${{fmt.format(filtered.length)}} of ${{fmt.format(all.length)}} memories`;
      document.getElementById('allMemoriesList').innerHTML =
        filtered.length
          ? filtered.map(c => renderMemCard(c, {{showMethod: true, showSession: true}})).join('')
          : '<div class="mem-meta">No memories match the current filters.</div>';
    }}

    function renderStructured(kind, s) {{
      if (!s) return '';
      const lines = [];
      if (kind === 'skill') {{
        if (s.activation) lines.push(`<div><b>Activation:</b> ${{escapeHtml(s.activation)}}</div>`);
        if (s.steps && s.steps.length) {{
          lines.push('<div><b>Steps:</b><ol style="margin:4px 0 4px 18px; padding:0">' +
            s.steps.map(x => `<li>${{escapeHtml(x)}}</li>`).join('') + '</ol></div>');
        }}
        if (s.termination) lines.push(`<div><b>Termination:</b> ${{escapeHtml(s.termination)}}</div>`);
        if (s.verification) lines.push(`<div><b>Verification:</b> ${{escapeHtml(s.verification)}}</div>`);
      }} else if (kind === 'procedure') {{
        if (s.goal) lines.push(`<div><b>Goal:</b> ${{escapeHtml(s.goal)}}</div>`);
        if (s.preconditions && s.preconditions.length) lines.push(`<div><b>Preconditions:</b> ${{escapeHtml(s.preconditions.join(', '))}}</div>`);
        if (s.steps && s.steps.length) {{
          lines.push('<div><b>Steps:</b><ol style="margin:4px 0 4px 18px; padding:0">' +
            s.steps.map(x => `<li>${{escapeHtml(x)}}</li>`).join('') + '</ol></div>');
        }}
        if (s.warnings && s.warnings.length) lines.push(`<div><b>Warnings:</b> ${{escapeHtml(s.warnings.join('; '))}}</div>`);
        if (s.outcome) lines.push(`<div><b>Outcome:</b> ${{escapeHtml(s.outcome)}}</div>`);
      }}
      return lines.length ? `<div class="body" style="margin-top:6px">${{lines.join('')}}</div>` : '';
    }}

    function metric(label, value) {{ return `<div class="metric"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`; }}
    function detailText(s) {{
      if (activeTab === 'transcript') return s.rendered_markdown || 'No rendered transcript found.';
      // activeTab === 'memory' is handled in renderDetail() via memoryHtml.
      if (activeTab === 'models') {{
        const tokens = s.tokens_by_model || {{}};
        const costs = s.cost_by_model || {{}};
        const names = Object.keys(tokens);
        if (!names.length) {{
          return 'No per-model token data for this session.\\n\\n(For embedded-cost sessions cost is taken as-is and not broken down by model.)';
        }}
        names.sort((a, b) => (tokens[b].total_tokens || 0) - (tokens[a].total_tokens || 0));
        const lines = [];
        const pad = (s, n) => String(s).padEnd(n);
        lines.push(pad('model', 28) + pad('input', 12) + pad('cache_create', 13) + pad('cache_read', 12) + pad('output', 10) + pad('total', 12) + 'cost');
        lines.push('-'.repeat(98));
        for (const m of names) {{
          const t = tokens[m] || {{}};
          const c = costs[m];
          lines.push(
            pad(m, 28)
            + pad(fmt.format(t.input_tokens || 0), 12)
            + pad(fmt.format(t.cache_creation_tokens || 0), 13)
            + pad(fmt.format(t.cached_input_tokens || 0), 12)
            + pad(fmt.format(t.output_tokens || 0), 10)
            + pad(fmt.format(t.total_tokens || 0), 12)
            + (c == null ? 'n/a' : money(c))
          );
        }}
        lines.push('');
        lines.push(`cost_mode: ${{DATA.cost_mode}}  ·  cost_source: ${{s.cost_source}}`);
        lines.push(`pricing: ${{DATA.rate_note}}`);
        return lines.join('\\n');
      }}
      if (activeTab === 'signals') {{
        const readings = s.signals || [];
        if (!readings.length) return 'No signals computed for this session. Run `retro signal run` then rebuild the dashboard.';
        const groupOrder = ['activity','outcome','cost','risk'];
        const byGroup = {{}};
        readings.forEach(r => {{ (byGroup[r.group] = byGroup[r.group] || []).push(r); }});
        const lines = [];
        groupOrder.forEach(g => {{
          if (!byGroup[g]) return;
          lines.push('# ' + g);
          byGroup[g].sort((a,b) => a.signal.localeCompare(b.signal)).forEach(r => {{
            const value = r.value === null ? '∅ (missing)' : r.value;
            const meta = r.metadata && Object.keys(r.metadata).length ? '  ' + JSON.stringify(r.metadata) : '';
            const unit = r.unit ? ' ' + r.unit : '';
            const conf = r.confidence !== undefined && r.confidence !== 1.0 ? ` conf=${{r.confidence}}` : '';
            lines.push(`  ${{r.signal.padEnd(28)}} ${{String(value)}}${{unit}}${{conf}}${{meta}}`);
          }});
          lines.push('');
        }});
        return lines.join('\\n');
      }}
      return JSON.stringify({{
        session_id: s.session_id,
        host: s.host,
        date: s.date,
        first_ts: s.first_ts,
        last_ts: s.last_ts,
        duration_seconds: s.duration_seconds,
        event_counts: s.event_counts,
        actor_counts: s.actor_counts,
        top_tools: s.top_tools,
        tokens: s.tokens,
        models: s.models,
        cost_model: s.cost_model,
        estimated_cost_usd: s.estimated_cost_usd,
        cost_note: s.cost_note,
        files_touched: s.files_touched,
        artifacts: {{
          normalized: s.normalized_path,
          rendered: s.rendered_path,
          raw_dir: s.raw_dir,
          mined: s.mined,
        }}
      }}, null, 2);
    }}

    function renderAccounting() {{
      const cats = DATA.summary.cost_categories || {{input: 0, cache_create: 0, cache_read: 0, output: 0}};
      document.getElementById('accInputCost').textContent = money(cats.input);
      document.getElementById('accCacheCreateCost').textContent = money(cats.cache_create);
      document.getElementById('accCacheReadCost').textContent = money(cats.cache_read);
      document.getElementById('accOutputCost').textContent = money(cats.output);

      const days = Object.entries(DATA.summary.by_day || {{}});
      document.getElementById('accountingRows').innerHTML = days.map(([day, d]) => {{
        const total = d.cost || 0;
        const ci = d.cost_input || 0;
        const ccc = d.cost_cache_create || 0;
        const ccr = d.cost_cache_read || 0;
        const co = d.cost_output || 0;
        const sum = ci + ccc + ccr + co;

        let ciPct = 0, cccPct = 0, ccrPct = 0, coPct = 0;
        if (sum > 0) {{
          ciPct = (ci / sum) * 100;
          cccPct = (ccc / sum) * 100;
          ccrPct = (ccr / sum) * 100;
          coPct = (co / sum) * 100;
        }}

        return `<tr>
          <td><b>${{day}}</b></td>
          <td>
            <div class="cost-stacked-bar">
              ${{ci > 0 ? `<div class="seg" style="width:${{ciPct}}%; background:var(--cost-input);" title="Input: ${{money(ci)}} (${{ciPct.toFixed(1)}}%)"></div>` : ''}}
              ${{ccc > 0 ? `<div class="seg" style="width:${{cccPct}}%; background:var(--cost-cache-create);" title="Cache Create: ${{money(ccc)}} (${{cccPct.toFixed(1)}}%)"></div>` : ''}}
              ${{ccr > 0 ? `<div class="seg" style="width:${{ccrPct}}%; background:var(--cost-cache-read);" title="Cache Read: ${{money(ccr)}} (${{ccrPct.toFixed(1)}}%)"></div>` : ''}}
              ${{co > 0 ? `<div class="seg" style="width:${{coPct}}%; background:var(--cost-output);" title="Output: ${{money(co)}} (${{coPct.toFixed(1)}}%)"></div>` : ''}}
            </div>
          </td>
          <td style="text-align: right;"><b>${{money(total)}}</b></td>
        </tr>`;
      }}).join('');
    }}

    function renderCeilingHeatmap() {{
      const grid = document.getElementById('heatmapGrid');
      const byDay = DATA.summary.by_day || {{}};
      const dates = [];
      const now = new Date();
      for (let i = 34; i >= 0; i--) {{
        const d = new Date(now.getTime() - i * 24 * 60 * 60 * 1000);
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        dates.push(`${{yyyy}}-${{mm}}-${{dd}}`);
      }}
      
      grid.innerHTML = dates.map(date => {{
        const dayData = byDay[date] || {{ tokens: 0, cost: 0.0, rate_limit_hits: 0 }};
        const tokens = dayData.tokens || 0;
        const cost = dayData.cost || 0.0;
        const rateLimitHits = dayData.rate_limit_hits || 0;
        const tokenRatio = Math.min(1.0, tokens / 1000000);
        const costRatio = Math.min(1.0, cost / 1.0);
        const util = Math.max(tokenRatio, costRatio);
        
        let bgColor = '#f3f4f6';
        let borderColor = '#d1d5db';
        let style = '';
        
        if (rateLimitHits > 0) {{
          bgColor = '#fef08a';
          borderColor = '#eab308';
          style = 'border: 2px solid #eab308; box-shadow: 0 0 6px rgba(234,179,8,0.5); background-color: #fef08a;';
        }} else if (util > 0.99) {{
          bgColor = '#22c55e';
          borderColor = '#16a34a';
        }} else if (util >= 0.5) {{
          bgColor = '#86efac';
          borderColor = '#4ade80';
        }} else if (util > 0) {{
          bgColor = '#dcfce7';
          borderColor = '#86efac';
        }}
        
        const tooltip = `${{date}}: ${{fmt.format(tokens)}} tokens, ${{money(cost)}}, ${{rateLimitHits}} rate limit hit(s)`;
        return `<div class="heatmap-cell" data-date="${{date}}" title="${{tooltip}}" style="width: 18px; height: 18px; border-radius: 4px; background: ${{bgColor}}; border: 1px solid ${{borderColor}}; cursor: pointer; ${{style}}"></div>`;
      }}).join('');
      
      document.querySelectorAll('.heatmap-cell').forEach(cell => {{
        cell.addEventListener('click', () => {{
          const date = cell.dataset.date;
          updateRingsForDate(date);
        }});
      }});
      
      const lastDate = dates[34];
      updateRingsForDate(lastDate);
    }}

    function updateRingsForDate(date) {{
      const byDay = DATA.summary.by_day || {{}};
      const dayData = byDay[date] || {{ tokens: 0, cost: 0.0, rate_limit_hits: 0 }};
      const tokens = dayData.tokens || 0;
      const cost = dayData.cost || 0.0;
      const rateLimitHits = dayData.rate_limit_hits || 0;
      
      const tokenRatio = Math.min(1.0, tokens / 1000000);
      const costRatio = Math.min(1.0, cost / 1.0);
      const limitRatio = Math.min(1.0, rateLimitHits / 1);
      
      const offsetTokens = 471.2 * (1 - tokenRatio);
      const offsetCost = 345.6 * (1 - costRatio);
      const offsetLimits = 219.9 * (1 - limitRatio);
      
      document.getElementById('ringTokens').style.strokeDashoffset = offsetTokens;
      document.getElementById('ringCost').style.strokeDashoffset = offsetCost;
      document.getElementById('ringLimits').style.strokeDashoffset = offsetLimits;
      
      document.getElementById('ringTokensText').textContent = `${{Math.round(tokenRatio * 100)}}%`;
      document.getElementById('ringCostText').textContent = `${{Math.round(costRatio * 100)}}%`;
      document.getElementById('ringLimitsText').textContent = `${{Math.round(limitRatio * 100)}}%`;
      
      document.getElementById('heatmapDetail').innerHTML = `
        <b>Showing Quotas for ${{date}}:</b><br/>
        • Tokens: ${{fmt.format(tokens)}} / 1,000,000 (${{Math.round(tokenRatio * 100)}}%)<br/>
        • Cost: ${{money(cost)}} / $1.00 (${{Math.round(costRatio * 100)}}%)<br/>
        • Rate limit warnings: ${{rateLimitHits}} / 1 target (${{Math.round(limitRatio * 100)}}%)
      `;
    }}

    function renderProjects() {{
      const projects = DATA.summary.projects || [];
      document.getElementById('projectsRows').innerHTML = projects.map(p => {{
        const pathSpan = p.path ? `<span class="path" title="${{escapeHtml(p.path)}}" style="font-family:monospace; font-size:11px;">${{escapeHtml(p.path)}}</span>` : '<span class="mem-meta">—</span>';
        const hostsBadges = p.hosts.map(h => `<span class="badge ${{h}}">${{h}}</span>`).join(' ');
        return `
          <tr data-name="${{escapeHtml(p.name)}}" style="cursor: pointer;">
            <td><b>${{escapeHtml(p.name)}}</b></td>
            <td>${{pathSpan}}</td>
            <td style="text-align: right;">${{fmt.format(p.sessions)}}</td>
            <td style="text-align: right;">${{hostsBadges}}</td>
            <td style="text-align: right;">${{fmt.format(p.tokens)}}</td>
            <td style="text-align: right;">${{money(p.cost)}}</td>
          </tr>
        `;
      }}).join('');
      
      document.querySelectorAll('#projectsRows tr').forEach(tr => tr.addEventListener('click', () => {{
        const name = tr.dataset.name;
        document.getElementById('projectFilter').value = name;
        renderRows();
      }}));
    }}

    function populateProjects() {{
      const select = document.getElementById('projectFilter');
      if (select.children.length > 1) return;
      const projects = new Set();
      DATA.sessions.forEach(s => {{
        if (s.project_name && s.project_name !== 'unknown') {{
          projects.add(s.project_name);
        }}
      }});
      const hasUnknown = DATA.sessions.some(s => !s.project_name || s.project_name === 'unknown');
      
      const sorted = Array.from(projects).sort();
      sorted.forEach(p => {{
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        select.appendChild(opt);
      }});
      if (hasUnknown) {{
        const opt = document.createElement('option');
        opt.value = 'unknown';
        opt.textContent = 'Unknown Project';
        select.appendChild(opt);
      }}
    }}

    function renderQuests() {{
      const qState = DATA.quests || {{}};
      const panel = document.getElementById('questsPanel');
      if (!qState.daily_quests || qState.daily_quests.length === 0) {{
        panel.style.display = 'none';
        return;
      }}
      panel.style.display = 'block';
      document.getElementById('questUserLevel').textContent = qState.user_level || 'Novice Prompt Mechanic';
      document.getElementById('questXp').innerHTML = `${{qState.experience_points || 0}} <span style="font-size: 14px; font-weight: 500; opacity: 0.85;">XP</span>`;
      document.getElementById('questStreak').textContent = `${{qState.streak_count || 0}} Day Streak`;
      document.getElementById('questStreakFreeze').textContent = `Freezes: ${{qState.streak_freezes || 0}}`;

      const listDiv = document.getElementById('questsList');
      listDiv.innerHTML = qState.daily_quests.map(q => {{
        const isCompleted = q.status === 'completed';
        const cardBg = isCompleted ? '#f0fdf4' : '#ffffff';
        const cardBorder = isCompleted ? '1px solid #bbf7d0' : '1px solid var(--line)';
        const badgeColor = isCompleted ? 'background:#dcfce7; color:#166534;' : 'background:#fef3c7; color:#92400e;';
        const badgeText = isCompleted ? 'Completed' : 'Active';
        
        return `<div style="background:${{cardBg}}; border:${{cardBorder}}; border-radius: 6px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; transition: transform 0.15s ease;">
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px;">
            <b style="font-size:14px; color:${{isCompleted ? '#166534' : 'var(--ink)'}};">${{escapeHtml(q.name)}}</b>
            <span style="font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; ${{badgeColor}}">${{badgeText}}</span>
          </div>
          <div style="font-size: 12px; color: ${{isCompleted ? '#15803d' : 'var(--ink)'}};">${{escapeHtml(q.objective)}}</div>
          <div style="font-size: 11px; color: var(--muted); font-style: italic;">${{escapeHtml(q.rationale)}}</div>
        </div>`;
      }}).join('');
    }}

    function renderOperatorProfile() {{
      const op = DATA.operator_profile || {{}};
      document.getElementById('opAvgTurns').textContent = op.avg_turns || '0.0';
      document.getElementById('opCmdFailure').textContent = `${{Math.round((op.cmd_failure_rate || 0) * 100)}}%`;
      document.getElementById('opExplorBalance').textContent = `${{op.explore_ratio || 0.5}} Explore / ${{op.exploit_ratio || 0.5}} Exploit`;
      document.getElementById('opAvgCost').textContent = money(op.avg_cost || 0.0);
      document.getElementById('opRoleClass').textContent = op.role || 'General Software Engineer';

      const recsDiv = document.getElementById('opRecommendations');
      const recs = op.recommendations || [];
      recsDiv.innerHTML = recs.map(rec => `
        <div style="border-left: 3px solid var(--accent); background: #fafaf9; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); border-radius: 0 4px 4px 0; padding: 10px; font-size: 12px; line-height: 1.4;">
          ${{escapeHtml(rec)}}
        </div>
      `).join('');
    }}

    function renderAll() {{ populateProjects(); renderInsights(); renderKpis(); renderQuests(); renderOperatorProfile(); renderProjects(); renderMemoryAggregates(); renderAllMemories(); renderSignalAggregates(); renderDayBars(); renderAccounting(); renderCeilingHeatmap(); renderRows(); renderDetail(); }}
    initAllMemoriesFilters();
    document.getElementById('search').addEventListener('input', renderRows);
    document.getElementById('hostFilter').addEventListener('change', renderRows);
    document.getElementById('projectFilter').addEventListener('change', renderRows);
    document.getElementById('riskFilter').addEventListener('change', renderRows);
    document.getElementById('memoryFilter').addEventListener('change', renderRows);
    document.getElementById('tabSummary').addEventListener('click', () => {{ activeTab='summary'; renderDetail(); }});
    document.getElementById('tabModels').addEventListener('click', () => {{ activeTab='models'; renderDetail(); }});
    document.getElementById('tabSignals').addEventListener('click', () => {{ activeTab='signals'; renderDetail(); }});
    document.getElementById('tabTranscript').addEventListener('click', () => {{ activeTab='transcript'; renderDetail(); }});
    document.getElementById('tabMemory').addEventListener('click', () => {{ activeTab='memory'; renderDetail(); }});
    renderAll();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
