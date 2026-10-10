"""Shipped-asset contracts that the Python build depends on.

These guard the two failure modes that are invisible to Python unit tests: the
session list grid drifting out of sync between header and rows, and an asset
reaching for the network.
"""
from __future__ import annotations

import re
from pathlib import Path

from retro.dashboard.build import ASSETS_DIR
from retro.dashboard.facts import COLUMNS, FLAGS

CSS = (ASSETS_DIR / "app.css").read_text(encoding="utf-8")
VIEWS = (ASSETS_DIR / "views.js").read_text(encoding="utf-8")
CORE = (ASSETS_DIR / "core.js").read_text(encoding="utf-8")
APP = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")


def session_column_classes() -> list[str]:
    block = VIEWS[VIEWS.index("var SESSION_COLUMNS = ["): VIEWS.index("views.sessions = function")]
    return [match.split()[0] for match in re.findall(r'cls:\s*"([^"]+)"', block)]


def grid_templates() -> list[tuple[str, list[str], set[str]]]:
    """(label, columns, hidden cell classes) for the base rule and each breakpoint."""
    templates: list[tuple[str, list[str], set[str]]] = []
    base = re.search(r"\.sessionlist\s*\{\s*--cols:\s*([^;]+);", CSS)
    assert base, "the session list must declare a --cols template"
    templates.append(("base", split_template(base.group(1)), set()))
    for block in re.finditer(r"@media \(max-width: (\d+)px\) \{(.*?)\n\}", CSS, re.S):
        body = block.group(2)
        cols = re.search(r"--cols:\s*([^;]+);", body)
        if not cols:
            continue
        hidden = set()
        for rule in re.finditer(r"([^{}]+)\{\s*display:\s*none;\s*\}", body):
            for selector in rule.group(1).split(","):
                selector = selector.strip()
                if selector.startswith(".c-"):
                    hidden.add(selector[1:])
        templates.append((block.group(1) + "px", split_template(cols.group(1)), hidden))
    return templates


def split_template(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in value.strip():
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isspace() and depth == 0:
            if current:
                parts.append(current)
                current = ""
            continue
        current += char
    if current:
        parts.append(current)
    return parts


def test_every_breakpoint_has_one_track_per_visible_cell():
    classes = session_column_classes()
    assert len(classes) == 8
    for label, columns, hidden in grid_templates():
        visible = [cls for cls in classes if not any(part in hidden for part in cls.split())]
        assert len(columns) == len(visible), (
            f"{label}: {len(columns)} grid tracks for {len(visible)} visible cells"
        )


def test_the_title_is_the_only_flexible_track():
    classes = session_column_classes()
    for label, columns, hidden in grid_templates():
        flexible = [column for column in columns if "fr" in column]
        assert len(flexible) == 1, f"{label}: expected exactly one flexible column"
        assert flexible[0].startswith("minmax(0,"), f"{label}: flexible column must be able to shrink"
        visible = [cls for cls in classes if not any(part in hidden for part in cls.split())]
        assert visible[columns.index(flexible[0])].startswith("c-title")


def test_narrow_breakpoints_keep_the_title_and_drop_wide_columns():
    for label, _, hidden in grid_templates():
        assert "c-title" not in hidden, f"{label}: the title must never be hidden"
    hidden_sets = [hidden for _, _, hidden in grid_templates()]
    assert hidden_sets[0] == set()
    assert hidden_sets[-1] >= {"c-agent", "c-project", "c-duration", "c-turns"}


def test_rows_and_header_share_one_template():
    assert re.search(r"\.listhead,\s*\.listrow\s*\{[^}]*grid-template-columns:\s*var\(--cols\)", CSS, re.S)
    assert ".listrow > * {" in CSS
    assert "text-overflow: ellipsis" in CSS
    assert "min-width: 0" in CSS


def test_row_cells_carry_tooltips():
    block = VIEWS[VIEWS.index("function renderRow(row, index)"): VIEWS.index("function refresh()")]
    assert 'title: title' in block
    assert 'title: project.id' in block


def test_tables_are_responsive():
    assert ".table-wrap { overflow-x: auto" in CSS
    assert "table.stack thead { display: none; }" in CSS
    assert 'content: attr(data-label)' in CSS
    assert '"data-label": labels[index]' in CORE


def test_hidden_elements_cannot_swallow_clicks():
    # .banner and .modal set display, so [hidden] needs an explicit rule.
    assert "[hidden] { display: none !important; }" in CSS
    for selector in (".banner {", ".modal {"):
        assert selector in CSS
    assert CSS.index("[hidden] { display: none !important; }") < CSS.index(".banner {")


def test_assets_never_reach_the_network():
    for asset in (CSS, VIEWS, CORE, APP, INDEX):
        assert "fetch(" not in asset
        assert "innerHTML" not in asset
        for url in re.findall(r"https?://[^\s\"')]+", asset):
            assert url.startswith("http://www.w3.org/"), url
    assert 'type="module"' not in INDEX


def test_client_constants_match_the_python_fact_table():
    for name in COLUMNS:
        assert f"COL.{name}" in CORE or f'COL["{name}"]' in CORE or name in ("events", "unknown", "active")
    for flag in ("excluded", "interactive", "automated", "subagent", "signals"):
        assert flag in FLAGS
        assert f'"{flag}"' in CORE


def test_excluded_payload_is_lazy():
    assert "function loadExcluded(done)" in CORE
    assert "shards.excluded" in CORE
    assert "dict_tail" in CORE
    assert CORE.count("C.loadExcluded") == 0
    assert VIEWS.count("C.loadExcluded()") >= 3


def test_cost_is_never_rendered_as_zero_when_unknown():
    assert "function costCell(row)" in CORE
    assert "COST_BASIS_REASON" in CORE
    assert "No cost estimate:" in CORE
    assert "sessions unpriced" in VIEWS
    # the fallback must be a dash, not a zero-valued money format
    block = CORE[CORE.index("function cost(row)"): CORE.index("function costBasis(row)")]
    assert "null" in block and "0" not in block.replace("COST_SCALE", "")


def test_group_totals_never_render_as_zero_when_nothing_is_priced():
    assert "function costTotal(stats)" in CORE
    assert "if (!stats.withCost) { return \"—\"; }" in CORE
    assert "C.costTotal(" in VIEWS
    assert "fmt.money(stats.cost)" not in VIEWS
    assert "fmt.money(entry.stats.cost)" not in VIEWS


def test_sync_banner_reports_a_past_run_never_a_live_one():
    assert 'sync.health === "failed"' in APP
    assert 'sync.health === "degraded"' in APP
    assert "stageNote" in APP
    assert '"last sync "' in APP
    assert "this dashboard was built during that sync" in APP
    for forbidden in ("syncing now", "synced just now", "in progress"):
        assert forbidden not in APP
    # an unfinished stage must not be rendered as a failure
    failure_branch = APP[APP.index('if (sync.health === "failed"'): APP.index("} else {")]
    assert "running" not in failure_branch
    # the banner shows an absolute time and a relative age
    note = APP[APP.index("function stageNote(sync)"): APP.index("function epochOf(value)")]
    assert "C.fmt.dateTime(epoch)" in note and "C.fmt.ago(epoch)" in note
    assert "stages_unfinished" in note


def test_quality_page_explains_a_build_time_report():
    assert "built_during_sync" in VIEWS
    assert "pending at build" in VIEWS
    assert "stages_unfinished" in VIEWS


def test_facets_are_scoped_and_capped():
    assert "FACET_OPTION_CAP" in VIEWS
    assert "facetTypeAhead" in VIEWS
    assert "C.facetCounts(scope" in VIEWS
    assert "more — use search" in VIEWS


def test_assets_are_listed_in_the_shell():
    assert ASSETS_DIR.is_dir()
    referenced = set(re.findall(r'(?:src|href)="assets/([^"]+)"', INDEX))
    on_disk = {
        path.name
        for path in ASSETS_DIR.iterdir()
        if path.is_file() and path.suffix in {".js", ".css"} and path.name != "index.html"
    }
    assert referenced == on_disk


def test_asset_payload_stays_small():
    total = sum(path.stat().st_size for path in ASSETS_DIR.iterdir() if path.is_file())
    assert total < 200 * 1024
    assert (ASSETS_DIR / "index.html").stat().st_size < 8 * 1024


def test_paths_can_be_derived_without_the_detail_payload():
    assert 'transcript: "rendered/"' in CORE
    assert "function mergeRowDetail(key, row, detail)" in CORE


def _unused() -> Path:
    return ASSETS_DIR
