"""Run signals across captured sessions and aggregate the readings.

Inputs:  rollout-memory/normalized/<host>/<id>.events.jsonl
Outputs: rollout-memory/signals/readings.jsonl
         rollout-memory/signals/aggregates.json
         rollout-memory/signals/summary.md
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from ..schema import Host
from ..storage import Layout
from .base import REGISTRY, SessionContext, Signal, SignalReading, load_events


@dataclass
class SessionRef:
    host: Host
    session_id: str
    normalized_path: Path
    raw_dir: Path


def iter_sessions(layout: Layout, host: str | None = None) -> Iterator[SessionRef]:
    normalized_root = layout.root / "normalized"
    if not normalized_root.exists():
        return
    host_dirs = [normalized_root / host] if host else sorted(normalized_root.iterdir())
    for host_dir in host_dirs:
        if not host_dir.exists() or not host_dir.is_dir():
            continue
        host_name = host_dir.name  # type: ignore[assignment]
        for jsonl in sorted(host_dir.glob("*.events.jsonl")):
            sid = jsonl.name[: -len(".events.jsonl")]
            yield SessionRef(
                host=host_name,  # type: ignore[arg-type]
                session_id=sid,
                normalized_path=jsonl,
                raw_dir=layout.raw_dir(host_name, sid),  # type: ignore[arg-type]
            )


def _load_raw_meta(raw_dir: Path) -> dict:
    for name in ("thread.json", "import_meta.json"):
        path = raw_dir / name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def run_signals(
    layout: Layout,
    *,
    host: str | None = None,
    session_ids: Iterable[str] | None = None,
    signal_names: Iterable[str] | None = None,
) -> list[SignalReading]:
    """Compute selected signals for selected sessions.

    Returns the list of all produced readings. Does not write to disk; the
    caller decides whether to merge with existing readings or replace.
    """
    selected_signals = _select_signals(signal_names)
    target_ids = set(session_ids) if session_ids else None
    readings: list[SignalReading] = []

    for ref in iter_sessions(layout, host=host):
        if target_ids is not None and ref.session_id not in target_ids:
            continue
        events = load_events(ref.normalized_path)
        if not events:
            continue
        ctx = SessionContext(
            host=ref.host,
            session_id=ref.session_id,
            events=events,
            raw_dir=ref.raw_dir,
            raw_meta=_load_raw_meta(ref.raw_dir),
        )
        for signal in selected_signals:
            try:
                produced = signal(ctx)
            except Exception as exc:  # one bad signal must not kill the run
                produced = [
                    SignalReading(
                        signal=signal.name,
                        group=signal.group,
                        kind=signal.kind,
                        method=signal.method,
                        session_id=ctx.session_id,
                        host=ctx.host,
                        value=None,
                        unit=signal.unit,
                        confidence=0.0,
                        metadata={"error": f"{type(exc).__name__}: {exc}"},
                    )
                ]
            readings.extend(produced)

    return readings


def _select_signals(names: Iterable[str] | None) -> list[Signal]:
    if names is None:
        return list(REGISTRY.values())
    wanted = set(names)
    missing = wanted - set(REGISTRY)
    if missing:
        raise KeyError(f"unknown signal(s): {sorted(missing)}")
    return [REGISTRY[n] for n in REGISTRY if n in wanted]


# --- aggregation -------------------------------------------------------------


def aggregate_readings(readings: Iterable[SignalReading]) -> dict:
    by_signal: dict[str, list[SignalReading]] = {}
    for r in readings:
        by_signal.setdefault(r.signal, []).append(r)

    agg: dict[str, dict] = {}
    for name, items in by_signal.items():
        signal = REGISTRY.get(name)
        kind = signal.kind if signal else items[0].kind
        group = signal.group if signal else items[0].group
        method = signal.method if signal else items[0].method
        unit = signal.unit if signal else items[0].unit
        values = [r.value for r in items if r.value is not None]
        present_sessions = [r.session_id for r in items if r.value is not None]
        missing_sessions = [r.session_id for r in items if r.value is None]

        block: dict = {
            "group": group,
            "kind": kind,
            "method": method,
            "unit": unit,
            "sessions_with_reading": len(present_sessions),
            "sessions_missing": len(missing_sessions),
        }

        if kind == "numeric" and values:
            numeric = [float(v) for v in values if isinstance(v, (int, float))]
            if numeric:
                numeric.sort()
                block.update(
                    {
                        "min": numeric[0],
                        "max": numeric[-1],
                        "mean": round(statistics.fmean(numeric), 3),
                        "median": statistics.median(numeric),
                        "p90": _percentile(numeric, 0.90),
                        "sum": round(sum(numeric), 3),
                    }
                )
        elif kind == "boolean":
            true_count = sum(1 for v in values if v is True)
            false_count = sum(1 for v in values if v is False)
            denom = true_count + false_count
            block.update(
                {
                    "true_count": true_count,
                    "false_count": false_count,
                    "true_ratio": round(true_count / denom, 3) if denom else None,
                }
            )
        elif kind == "categorical":
            block["histogram"] = dict(Counter(str(v) for v in values).most_common())

        agg[name] = block

    return {"by_signal": agg}


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return round(sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f), 3)


# --- disk I/O ----------------------------------------------------------------


def write_signal_artifacts(layout: Layout, readings: list[SignalReading]) -> dict[str, Path]:
    signals_dir = layout.root / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)

    readings_path = signals_dir / "readings.jsonl"
    with readings_path.open("w", encoding="utf-8") as fh:
        for r in readings:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False))
            fh.write("\n")

    aggregates = aggregate_readings(readings)
    agg_path = signals_dir / "aggregates.json"
    agg_path.write_text(
        json.dumps(aggregates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summary_path = signals_dir / "summary.md"
    summary_path.write_text(_render_summary(readings, aggregates), encoding="utf-8")

    return {"readings": readings_path, "aggregates": agg_path, "summary": summary_path}


def _render_summary(readings: list[SignalReading], aggregates: dict) -> str:
    by_session: dict[tuple[str, str], int] = Counter()
    for r in readings:
        by_session[(r.host, r.session_id)] += 1

    lines = ["# Signal Summary", ""]
    lines.append(f"- **Sessions evaluated:** {len(by_session)}")
    lines.append(f"- **Signal readings:** {len(readings)}")
    lines.append("")
    lines.append("## By signal")
    lines.append("")
    lines.append("| signal | group | kind | sessions | summary |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, block in sorted(aggregates.get("by_signal", {}).items()):
        if block["kind"] == "numeric" and "mean" in block:
            summary = f"mean={block['mean']}, median={block['median']}, max={block['max']}"
        elif block["kind"] == "boolean":
            ratio = block.get("true_ratio")
            summary = f"true={block['true_count']}/{block['true_count'] + block['false_count']} ({ratio})"
        elif block["kind"] == "categorical" and block.get("histogram"):
            top = list(block["histogram"].items())[:3]
            summary = ", ".join(f"{k}={v}" for k, v in top)
        else:
            summary = "—"
        lines.append(
            f"| `{name}` | {block['group']} | {block['kind']} | "
            f"{block['sessions_with_reading']} | {summary} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"
