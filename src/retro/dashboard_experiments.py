"""Build an experimental dashboard for trajectory signals only.

CLI:
    retro dashboard experiments [--root PATH] [--out PATH]
    python -m retro.dashboard_experiments
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

# Defaults are cwd-relative; build() rebinds these from its arguments.
ARTIFACT_ROOT = Path.cwd() / "rollout-memory"
OUT_DIR = Path.cwd() / "dashboard"
DATA_DIR = OUT_DIR / "data"
TRAJECTORY_PREFIX = "trajectory_"


def build(artifact_root: Path | None = None, out_dir: Path | None = None) -> Path:
    """Build the trajectory-experiments page; returns the generated HTML path."""
    global ARTIFACT_ROOT, OUT_DIR, DATA_DIR
    if artifact_root is not None:
        ARTIFACT_ROOT = Path(artifact_root).expanduser().resolve()
    if out_dir is not None:
        OUT_DIR = Path(out_dir).expanduser().resolve()
        DATA_DIR = OUT_DIR / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_payload()
    data_path = DATA_DIR / "trajectory_experiments.json"
    html_path = OUT_DIR / "trajectory_experiments.html"
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    print(f"wrote {data_path}")
    print(f"wrote {html_path}")
    return html_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("RETRO_ARTIFACT_ROOT"),
        help="rollout-memory artifact root (default: ./rollout-memory)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output directory (default: ./dashboard)",
    )
    args = parser.parse_args()
    build(
        artifact_root=Path(args.artifact_root) if args.artifact_root else None,
        out_dir=Path(args.out) if args.out else None,
    )


def build_payload() -> dict[str, Any]:
    readings_by_session = load_trajectory_readings()
    aggregates = load_trajectory_aggregates()
    session_meta = load_session_meta()
    sessions: list[dict[str, Any]] = []
    for key, readings in readings_by_session.items():
        host, session_id = key
        meta = session_meta.get(key, {})
        signals_index = {r["signal"]: r["value"] for r in readings}
        sessions.append(
            {
                "host": host,
                "session_id": session_id,
                "title": meta.get("title") or session_id,
                "date": meta.get("date") or "",
                "first_ts": meta.get("first_ts"),
                "signals": sorted(readings, key=lambda r: (r.get("group", ""), r["signal"])),
                "signals_index": signals_index,
                "risk_badges": risk_badges(signals_index),
            }
        )
    sessions.sort(key=lambda s: (s.get("first_ts") or "", s["host"], s["session_id"]), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(sessions, aggregates),
        "signals": {"by_signal": aggregates},
        "sessions": sessions,
    }


def load_trajectory_readings() -> dict[tuple[str, str], list[dict]]:
    path = ARTIFACT_ROOT / "signals" / "readings.jsonl"
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(row.get("signal", "")).startswith(TRAJECTORY_PREFIX):
            continue
        out[(row["host"], row["session_id"])].append(row)
    return out


def load_trajectory_aggregates() -> dict[str, dict]:
    path = ARTIFACT_ROOT / "signals" / "aggregates.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    by_signal = data.get("by_signal", {})
    return {k: v for k, v in by_signal.items() if k.startswith(TRAJECTORY_PREFIX)}


def load_session_meta() -> dict[tuple[str, str], dict]:
    path = DATA_DIR / "rollouts.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out = {}
    for session in data.get("sessions", []):
        out[(session["host"], session["session_id"])] = {
            "title": session.get("title"),
            "date": session.get("date"),
            "first_ts": session.get("first_ts"),
        }
    return out


def risk_badges(signals: dict[str, Any]) -> list[str]:
    badges = []
    if signals.get("trajectory_premature_finish_without_validation"):
        badges.append("unvalidated")
    if signals.get("trajectory_repeated_action_without_progress"):
        badges.append("repeat")
    fix_no_test = signals.get("trajectory_fix_without_validation_count") or 0
    if fix_no_test:
        badges.append(f"{fix_no_test} fix/no-test")
    ignored = signals.get("trajectory_failure_ignored_count") or 0
    if ignored:
        badges.append(f"{ignored} ignored-failure")
    return badges


def summarize(sessions: list[dict], aggregates: dict[str, dict]) -> dict[str, Any]:
    return {
        "session_count": len(sessions),
        "signal_count": len(aggregates),
        "avg_steps": aggregates.get("trajectory_step_count", {}).get("mean"),
        "unvalidated_sessions": aggregates.get(
            "trajectory_premature_finish_without_validation", {}
        ).get("true_count", 0),
        "validation_sessions": aggregates.get("trajectory_validation_presence", {}).get(
            "true_count", 0
        ),
        "mean_entropy": aggregates.get("trajectory_sequence_entropy", {}).get("mean"),
        "mean_redundancy": aggregates.get("trajectory_action_redundancy", {}).get("mean"),
    }


def render_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    generated = escape(payload["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trajectory Experiments</title>
  <style>
    :root {{
      --bg:#f8f8f5; --panel:#fff; --line:#d9d7ce; --ink:#1f211d; --muted:#696b63;
      --accent:#ea580c; --bad:#b91c1c; --warn:#b45309; --blue:#2563eb;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); }}
    header {{ padding:24px 28px 16px; border-bottom:1px solid var(--line); background:#fbfbf8; }}
    h1 {{ margin:0 0 4px; font-size:24px; }}
    .subtle {{ color:var(--muted); font-size:13px; }}
    main {{ padding:20px 28px 34px; display:grid; gap:16px; }}
    .kpis,.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
    .kpi,.panel,.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
    .kpi {{ padding:12px; min-height:80px; }}
    .kpi .label,.card .label {{ color:var(--muted); font-size:12px; }}
    .kpi .value {{ margin-top:6px; font-size:24px; font-weight:760; }}
    .panel {{ overflow:hidden; }}
    .panel h2 {{ margin:0; padding:13px 14px; border-bottom:1px solid var(--line); background:#fbfbf8; font-size:15px; }}
    .cards {{ padding:12px; }}
    .card {{ padding:10px; }}
    .card .value {{ margin-top:4px; font-weight:760; font-size:18px; }}
    .card .sub {{ margin-top:3px; color:var(--muted); font-size:11px; }}
    .controls {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
    input,select {{ border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:white; min-height:38px; }}
    input {{ flex:1; min-width:min(420px,100%); }}
    .grid {{ display:grid; grid-template-columns:minmax(0,1.1fr) minmax(360px,.9fr); gap:16px; align-items:start; }}
    .scroll {{ max-height:640px; overflow:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ position:sticky; top:0; background:#fbfbf8; color:var(--muted); }}
    tr {{ cursor:pointer; }}
    tr:hover,tr.selected {{ background:#ffedd5; }}
    .badge {{ display:inline-flex; align-items:center; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:650; background:#ecebe4; color:#363831; margin:1px 2px 1px 0; }}
    .badge.codex {{ background:#dbeafe; color:#1d4ed8; }}
    .badge.claude-code {{ background:#ffedd5; color:#c2410c; }}
    .badge.warn {{ background:#fef3c7; color:#92400e; }}
    .badge.bad {{ background:#fee2e2; color:#991b1b; }}
    pre {{ margin:0; padding:12px; overflow:auto; max-height:700px; white-space:pre-wrap; word-break:break-word; background:#111827; color:#f9fafb; font-size:12px; line-height:1.45; }}
    .metricrow {{ display:grid; grid-template-columns:190px 1fr; gap:8px; margin:4px 0; }}
    .metricrow b {{ color:#e5e7eb; }}
    @media (max-width: 960px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Trajectory Experiments</h1>
    <div class="subtle">Generated {generated}. Experimental signals only; the main dashboard stays unchanged.</div>
  </header>
  <main>
    <section class="kpis" id="kpis"></section>
    <section class="panel">
      <h2>Portfolio Shape</h2>
      <div class="cards" id="aggregateCards"></div>
    </section>
    <section class="controls">
      <input id="search" placeholder="Search title, id, host, badge..." />
      <select id="hostFilter">
        <option value="all">All hosts</option>
        <option value="codex">Codex</option>
        <option value="claude-code">Claude Code</option>
      </select>
      <select id="riskFilter">
        <option value="all">All risk states</option>
        <option value="unvalidated">unvalidated</option>
        <option value="repeat">repeat</option>
        <option value="fix/no-test">fix/no-test</option>
        <option value="ignored-failure">ignored-failure</option>
      </select>
    </section>
    <section class="grid">
      <div class="panel">
        <h2 id="sessionsTitle">Sessions</h2>
        <div class="scroll">
          <table>
            <thead><tr><th>Host</th><th>Date</th><th>Title</th><th>Steps</th><th>Entropy</th><th>Validation</th><th>Risk</th></tr></thead>
            <tbody id="rows"></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2 id="detailTitle">Session Detail</h2>
        <pre id="detail">Select a session.</pre>
      </div>
    </section>
  </main>
  <script id="trajectory-data" type="application/json">{data_json}</script>
  <script>
    const DATA = JSON.parse(document.getElementById('trajectory-data').textContent);
    let selected = DATA.sessions[0] || null;
    const fmt = new Intl.NumberFormat();
    const val = (s, name, fallback='') => s?.signals_index?.[name] ?? fallback;
    function esc(x) {{ return String(x ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}
    function kpi(label, value) {{ return `<div class="kpi"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`; }}
    function card(label, value, sub='') {{ return `<div class="card"><div class="label">${{label}}</div><div class="value">${{value}}</div><div class="sub">${{sub}}</div></div>`; }}
    function agg(name) {{ return DATA.signals.by_signal[name] || {{}}; }}
    function aggMean(name) {{ const a=agg(name); return a.mean == null ? 'n/a' : Number(a.mean).toFixed(2); }}
    function aggBool(name) {{ const a=agg(name); return `${{a.true_count || 0}} true`; }}
    function renderKpis() {{
      const s = DATA.summary;
      document.getElementById('kpis').innerHTML = [
        kpi('Sessions', fmt.format(s.session_count || 0)),
        kpi('Trajectory signals', fmt.format(s.signal_count || 0)),
        kpi('Avg steps', s.avg_steps == null ? 'n/a' : Number(s.avg_steps).toFixed(1)),
        kpi('Validation sessions', fmt.format(s.validation_sessions || 0)),
        kpi('Unvalidated sessions', fmt.format(s.unvalidated_sessions || 0)),
        kpi('Mean entropy', s.mean_entropy == null ? 'n/a' : Number(s.mean_entropy).toFixed(2)),
      ].join('');
    }}
    function renderAggregates() {{
      const topCat = ['search','explore','locate','generate_fix','run_tests','refactor','reproduce','explain']
        .map(c => [c, agg(`trajectory_action_ratio_${{c}}`).mean])
        .filter(([,v]) => v != null)
        .sort((a,b) => b[1] - a[1])[0];
      document.getElementById('aggregateCards').innerHTML = [
        card('Step count', aggMean('trajectory_step_count'), `p90 ${{agg('trajectory_step_count').p90 ?? 'n/a'}} · max ${{agg('trajectory_step_count').max ?? 'n/a'}}`),
        card('Sequence entropy', aggMean('trajectory_sequence_entropy'), 'higher means more varied action mix'),
        card('Action redundancy', aggMean('trajectory_action_redundancy'), 'repeated action fingerprints'),
        card('Validation present', aggBool('trajectory_validation_presence'), 'tests/checks after first edit'),
        card('Unvalidated finish', aggBool('trajectory_premature_finish_without_validation'), 'edits after last validation'),
        card('Top category', topCat ? topCat[0] : 'n/a', topCat ? `mean ratio ${{Number(topCat[1]).toFixed(2)}}` : ''),
        card('Result reuse', aggMean('trajectory_result_token_reuse_ratio'), 'proxy for reacting to output'),
        card('Balance score', aggMean('trajectory_balance_score'), 'exploration vs exploitation'),
      ].join('');
    }}
    function badge(text, cls='') {{ return `<span class="badge ${{cls}}">${{esc(text)}}</span>`; }}
    function riskHtml(s) {{ return (s.risk_badges || []).map(x => badge(x, x.includes('unvalidated') || x.includes('repeat') ? 'warn' : 'bad')).join(''); }}
    function filteredSessions() {{
      const q = document.getElementById('search').value.toLowerCase();
      const host = document.getElementById('hostFilter').value;
      const risk = document.getElementById('riskFilter').value;
      return DATA.sessions.filter(s => {{
        if (host !== 'all' && s.host !== host) return false;
        if (risk !== 'all' && !(s.risk_badges || []).some(b => b.includes(risk))) return false;
        const hay = [s.host, s.session_id, s.title, ...(s.risk_badges || [])].join(' ').toLowerCase();
        return hay.includes(q);
      }});
    }}
    function renderRows() {{
      const rows = filteredSessions();
      document.getElementById('sessionsTitle').textContent = `Sessions · showing ${{fmt.format(rows.length)}} of ${{fmt.format(DATA.sessions.length)}}`;
      document.getElementById('rows').innerHTML = rows.map(s => {{
        const sel = selected && selected.host === s.host && selected.session_id === s.session_id;
        return `<tr data-id="${{s.host}}/${{s.session_id}}" class="${{sel ? 'selected' : ''}}">
          <td>${{badge(s.host, s.host)}}</td>
          <td>${{esc(s.date || '')}}</td>
          <td>${{esc(s.title || s.session_id)}}</td>
          <td>${{esc(val(s, 'trajectory_step_count', ''))}}</td>
          <td>${{esc(val(s, 'trajectory_sequence_entropy', ''))}}</td>
          <td>${{val(s, 'trajectory_validation_presence') ? 'yes' : 'no'}}</td>
          <td>${{riskHtml(s)}}</td>
        </tr>`;
      }}).join('');
      document.querySelectorAll('#rows tr').forEach(tr => tr.addEventListener('click', () => {{
        const [host,id] = tr.dataset.id.split('/');
        selected = DATA.sessions.find(s => s.host === host && s.session_id === id);
        renderAll();
      }}));
    }}
    function renderDetail() {{
      if (!selected) return;
      document.getElementById('detailTitle').textContent = selected.title || selected.session_id;
      const lines = [];
      lines.push(`${{selected.host}}/${{selected.session_id}}`);
      lines.push('');
      lines.push('Key metrics');
      ['trajectory_step_count','trajectory_validation_presence','trajectory_premature_finish_without_validation','trajectory_action_redundancy','trajectory_sequence_entropy','trajectory_balance_score','trajectory_fix_without_validation_count','trajectory_failure_ignored_count','trajectory_result_token_reuse_ratio'].forEach(name => {{
        lines.push(`  ${{name.padEnd(52)}} ${{val(selected, name, 'n/a')}}`);
      }});
      lines.push('');
      lines.push('All trajectory readings');
      selected.signals.forEach(r => {{
        const meta = r.metadata && Object.keys(r.metadata).length ? '  ' + JSON.stringify(r.metadata) : '';
        lines.push(`  ${{r.signal.padEnd(52)}} ${{String(r.value)}}${{r.unit ? ' ' + r.unit : ''}}${{meta}}`);
      }});
      document.getElementById('detail').textContent = lines.join('\\n');
    }}
    function renderAll() {{ renderKpis(); renderAggregates(); renderRows(); renderDetail(); }}
    document.getElementById('search').addEventListener('input', renderRows);
    document.getElementById('hostFilter').addEventListener('change', renderRows);
    document.getElementById('riskFilter').addEventListener('change', renderRows);
    renderAll();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
