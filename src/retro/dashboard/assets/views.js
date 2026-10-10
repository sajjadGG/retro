/* retro dashboard views: overview, projects, sessions, session detail, signals, quality. */
(function (global) {
  "use strict";

  var C = global.RETRO.core;
  var el = C.el;
  var fmt = C.fmt;
  var views = {};
  var MIN_SAMPLE = 5;
  var OUTCOME_ORDER = ["success", "partial", "failed", "abandoned", "unknown"];

  function params(ctx) { return ctx.params || {}; }
  function rangeKey(ctx) { return params(ctx).range || "30d"; }
  function includeExcluded(ctx) { return params(ctx).inc === "1"; }
  function rollup(ctx) { return params(ctx).roll !== "0"; }

  function navigate(ctx, changes, route, id) {
    var next = {};
    var current = params(ctx);
    for (var key in current) {
      if (Object.prototype.hasOwnProperty.call(current, key)) { next[key] = current[key]; }
    }
    for (var change in changes) {
      if (Object.prototype.hasOwnProperty.call(changes, change)) { next[change] = changes[change]; }
    }
    C.go(route || ctx.route, id === undefined ? ctx.id : id, next);
  }

  function filterOptions(ctx, extra) {
    var options = {
      includeExcluded: includeExcluded(ctx),
      since: C.rangeStart(rangeKey(ctx)),
      query: params(ctx).q || "",
      agents: params(ctx).agent ? [params(ctx).agent] : null,
      clients: params(ctx).client ? [params(ctx).client] : null,
      projects: params(ctx).project ? [params(ctx).project] : null,
      tags: params(ctx).tag ? [params(ctx).tag] : null,
      outcomes: params(ctx).outcome ? [params(ctx).outcome] : null,
      work: params(ctx).work ? [params(ctx).work] : null,
      models: params(ctx).model ? [params(ctx).model] : null,
      interactive: params(ctx).mode === "interactive" ? true : (params(ctx).mode === "automated" ? false : null)
    };
    if (params(ctx).from) { options.since = Date.parse(params(ctx).from) / 1000; }
    if (params(ctx).to) { options.until = Date.parse(params(ctx).to) / 1000 + 86399; }
    for (var key in extra || {}) {
      if (Object.prototype.hasOwnProperty.call(extra, key)) { options[key] = extra[key]; }
    }
    return options;
  }

  function rangeChips(ctx) {
    var keys = ["7d", "30d", "90d", "365d", "all"];
    var active = rangeKey(ctx);
    return el("div", {class: "chips", role: "group", "aria-label": "Time range"}, keys.map(function (key) {
      return el("button", {
        type: "button", class: "chip", text: key, "aria-pressed": key === active ? "true" : "false",
        onclick: function () { navigate(ctx, {range: key}); }
      });
    }).concat([
      el("button", {
        type: "button", class: "chip", text: "include excluded",
        "aria-pressed": includeExcluded(ctx) ? "true" : "false",
        title: "Show sessions the catalog excluded (eval runs, scratch dirs, empty sessions)",
        onclick: function () { navigate(ctx, {inc: includeExcluded(ctx) ? "0" : "1"}); }
      })
    ]));
  }

  function sessionLink(row) {
    return el("a", {href: C.buildHash("session", row[C.COL.key], {}), text: row[C.COL.title] || row[C.COL.key]});
  }

  function loadingNote() {
    if (C.store.ready) { return null; }
    return el("p", {class: "muted", text: "loading sessions… " + fmt.int(C.store.rows.length) + " of " + fmt.int(C.store.total)});
  }

  /* ---------------- overview ---------------------------------------------- */

  views.overview = function (container, ctx) {
    if (includeExcluded(ctx)) { C.loadExcluded(); }
    var rows = C.filterRows(filterOptions(ctx, {rollupSubagents: false}));
    var stats = C.aggregate(rows);
    var span = C.rangeDays(rangeKey(ctx));
    var previous = [];
    if (span) {
      var since = C.rangeStart(rangeKey(ctx));
      previous = C.filterRows(filterOptions(ctx, {since: since - span * 86400, until: since, rollupSubagents: false}));
    }
    var before = C.aggregate(previous);

    function delta(now, then) {
      if (!span) { return null; }
      if (!then) { return now ? 1 : null; }
      return (now - then) / then;
    }

    container.appendChild(el("h1", {text: "Overview"}));
    container.appendChild(el("p", {class: "sub"}, [
      "All agents and projects · " + fmt.int(C.store.total) + " sessions captured · ",
      el("span", {class: "mono", text: C.manifest.archive ? C.manifest.archive.root : ""})
    ]));
    container.appendChild(rangeChips(ctx));
    var note = loadingNote();
    if (note) { container.appendChild(note); }

    container.appendChild(el("div", {class: "grid cards", style: "margin-top:14px"}, [
      C.kpi("sessions", fmt.int(stats.sessions), delta(stats.sessions, before.sessions)),
      C.kpi("active time", fmt.duration(stats.activeS), delta(stats.activeS, before.activeS)),
      C.kpi("est. cost", C.costTotal(stats), delta(stats.cost, before.cost),
        stats.unpriced ? fmt.int(stats.unpriced) + " sessions unpriced" : "all sessions priced"),
      C.kpi("tokens", fmt.compact(stats.tokens), delta(stats.tokens, before.tokens)),
      C.kpi("user turns", fmt.int(stats.turns), delta(stats.turns, before.turns)),
      C.kpi("tool calls", fmt.int(stats.tools), delta(stats.tools, before.tools)),
      C.kpi("file edits", fmt.int(stats.edits), delta(stats.edits, before.edits)),
      C.kpi("success rate", fmt.pct(stats.successRate), null,
        stats.successRate === null ? "no outcome signal" : fmt.int(stats.outcomes.success || 0) + " successful")
    ]));
    if (stats.unpriced) {
      container.appendChild(el("p", {class: "muted", style: "margin:8px 0 0"}, [
        C.costTotal(stats) + " covers " + fmt.int(stats.withCost) + " of " + fmt.int(stats.sessions) +
        " sessions; " + fmt.int(stats.unpriced) + " have no cost estimate (",
        unpricedReasons(stats.basis),
        stats.withCost
          ? "). Averages use priced sessions only — mean " + fmt.money(stats.costPerSession) + " per priced session."
          : ")."
      ]));
    }

    var days = span || 90;
    var series = C.dailySeries(rows, Math.min(days, 180));
    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("sessions per day", [C.barChart(series.map(function (point) {
        return {label: point.day, value: point.sessions};
      }), {label: "sessions per day", format: fmt.int})]),
      C.panel("estimated cost per day", [C.barChart(series.map(function (point) {
        return {label: point.day, value: point.cost};
      }), {label: "cost per day", format: fmt.money})])
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("outcome mix", [C.mixBar(stats.outcomes, {label: "outcome mix", order: OUTCOME_ORDER})]),
      C.panel("work kind mix", [C.mixBar(stats.work, {label: "work kind mix"})])
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("agents", [agentTable(rows, ctx, "agent")]),
      C.panel("clients", [agentTable(rows, ctx, "client")])
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("notable changes", [notableChanges(rows, previous, span)]),
      C.panel("momentum", momentum(rows, stats))
    ]));

    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "recent sessions"}),
      recentTable(topRecent(rows, 12)),
      el("p", null, [el("a", {href: C.buildHash("sessions", "", params(ctx)), text: "open sessions explorer →"})])
    ]));
  };

  function unpricedReasons(basis) {
    var parts = [];
    for (var name in basis) {
      if (Object.prototype.hasOwnProperty.call(basis, name)) {
        parts.push(el("span", {
          class: "tag",
          text: name + " " + fmt.int(basis[name]),
          title: C.COST_BASIS_REASON[name] || name
        }));
      }
    }
    return el("span", null, parts);
  }

  function agentTable(rows, ctx, dimension) {
    var groups = C.groupBy(rows, dimension === "agent" ? C.agentName : C.clientName);
    var entries = Object.keys(groups).map(function (key) {
      var stats = C.aggregate(groups[key]);
      return {key: key, stats: stats};
    }).sort(function (a, b) { return b.stats.sessions - a.stats.sessions; });
    return C.tableOf(
      [dimension, {label: "sessions", num: true}, {label: "active", num: true}, {label: "cost", num: true}, {label: "success", num: true}],
      entries.map(function (entry) {
        return [
          el("a", {
            href: C.buildHash("sessions", "", merge(params(ctx), dimension === "agent" ? {agent: entry.key} : {client: entry.key})),
            text: entry.key
          }),
          {num: true, text: fmt.int(entry.stats.sessions)},
          {num: true, text: fmt.duration(entry.stats.activeS)},
          {num: true, text: C.costTotal(entry.stats), title: C.costTotalTitle(entry.stats)},
          {num: true, text: fmt.pct(entry.stats.successRate)}
        ];
      })
    );
  }

  function notableChanges(currentRows, previousRows, span) {
    if (!span) { return el("p", {class: "muted", text: "pick a bounded range to compare periods"}); }
    var current = C.groupBy(currentRows, function (row) { return C.projectMeta(row).id; });
    var previous = C.groupBy(previousRows, function (row) { return C.projectMeta(row).id; });
    var seen = Object.create(null);
    var movers = [];
    [current, previous].forEach(function (groups) {
      for (var id in groups) {
        if (!Object.prototype.hasOwnProperty.call(groups, id) || seen[id]) { continue; }
        seen[id] = true;
        var size = (current[id] ? current[id].length : 0) + (previous[id] ? previous[id].length : 0);
        if (size < MIN_SAMPLE) { continue; }
        var now = C.aggregate(current[id] || []);
        var before = C.aggregate(previous[id] || []);
        if (now.sessions + before.sessions < MIN_SAMPLE) { continue; }
        movers.push({
          id: id,
          name: (current[id] || previous[id])[0] ? C.projectMeta((current[id] || previous[id])[0]).name : id,
          sessions: now.sessions - before.sessions,
          cost: now.cost - before.cost,
          activeS: now.activeS - before.activeS
        });
      }
    });
    if (!movers.length) { return el("p", {class: "muted", text: "not enough sessions to compare (min " + MIN_SAMPLE + " per project)"}); }
    movers.sort(function (a, b) { return Math.abs(b.sessions) - Math.abs(a.sessions) || Math.abs(b.cost) - Math.abs(a.cost); });
    return C.tableOf(
      ["project", {label: "Δ sessions", num: true}, {label: "Δ active", num: true}, {label: "Δ cost", num: true}],
      movers.slice(0, 8).map(function (mover) {
        return [
          el("a", {href: C.buildHash("project", mover.id, {}), text: mover.name}),
          {num: true, node: signed(mover.sessions, fmt.int)},
          {num: true, node: signed(mover.activeS, fmt.duration)},
          {num: true, node: signed(mover.cost, fmt.money)}
        ];
      })
    );
  }

  function signed(value, format) {
    var direction = value > 0 ? "up" : (value < 0 ? "down" : "flat");
    return el("span", {class: direction, text: (value > 0 ? "+" : value < 0 ? "−" : "") + format(Math.abs(value))});
  }

  function momentum(rows, stats) {
    var streak = C.streak(rows);
    var perDay = stats.activeDays ? stats.sessions / stats.activeDays : 0;
    return [
      el("dl", {class: "kv"}, [
        el("dt", {text: "current streak"}), el("dd", {text: streak + (streak === 1 ? " day" : " days")}),
        el("dt", {text: "active days"}), el("dd", {text: fmt.int(stats.activeDays)}),
        el("dt", {text: "sessions / active day"}), el("dd", {text: perDay.toFixed(1)}),
        el("dt", {text: "projects touched"}), el("dd", {text: fmt.int(stats.projectCount)}),
        el("dt", {text: "corrections"}), el("dd", {text: fmt.int(stats.corrections)}),
        el("dt", {text: "tool error rate"}), el("dd", {text: fmt.pct(stats.errorRate, 1)})
      ])
    ];
  }

  function recentTable(rows) {
    var ordered = rows.slice().sort(function (a, b) {
      return (b[C.COL.started] || 0) - (a[C.COL.started] || 0);
    });
    return C.tableOf(
      ["when", "agent", "project", "title", {label: "cost", num: true}, "outcome"],
      ordered.map(function (row) {
        return [
          fmt.ago(row[C.COL.started]),
          C.agentName(row),
          {text: C.projectMeta(row).name, title: C.projectMeta(row).id},
          sessionLink(row),
          {num: true, node: C.costCell(row)},
          {node: C.pill(C.outcomeName(row))}
        ];
      })
    );
  }

  function topRecent(rows, count) {
    return rows.slice().sort(function (a, b) {
      return (b[C.COL.started] || 0) - (a[C.COL.started] || 0);
    }).slice(0, count);
  }

  function merge(base, changes) {
    var out = {};
    for (var key in base) { if (Object.prototype.hasOwnProperty.call(base, key)) { out[key] = base[key]; } }
    for (var change in changes) { if (Object.prototype.hasOwnProperty.call(changes, change)) { out[change] = changes[change]; } }
    return out;
  }

  /* ---------------- projects ----------------------------------------------- */

  views.projects = function (container, ctx) {
    if (includeExcluded(ctx)) { C.loadExcluded(); }
    var rows = C.filterRows(filterOptions(ctx, {rollupSubagents: false}));
    var groups = C.groupBy(rows, function (row) { return C.projectMeta(row).id; });
    var entries = Object.keys(groups).map(function (id) {
      var groupRows = groups[id];
      var stats = C.aggregate(groupRows);
      var meta = C.projectMeta(groupRows[0]);
      return {id: id, meta: meta, stats: stats, rows: groupRows};
    });
    var sortIndex = parseInt(params(ctx).psort || "2", 10);
    var descending = params(ctx).pdir !== "asc";
    var accessors = [
      function (entry) { return entry.meta.name.toLowerCase(); },
      function (entry) { return entry.stats.sessions; },
      function (entry) { return entry.stats.activeS; },
      function (entry) { return entry.stats.cost; },
      function (entry) { return entry.stats.successRate === null ? -1 : entry.stats.successRate; },
      function (entry) { return entry.stats.errorRate === null ? -1 : entry.stats.errorRate; },
      function (entry) { return entry.stats.last || 0; }
    ];
    entries.sort(function (a, b) {
      var accessor = accessors[Math.min(sortIndex, accessors.length - 1)];
      var left = accessor(a), right = accessor(b);
      if (left < right) { return descending ? 1 : -1; }
      if (left > right) { return descending ? -1 : 1; }
      return 0;
    });

    container.appendChild(el("h1", {text: "Projects"}));
    container.appendChild(el("p", {class: "sub", text:
      fmt.int(entries.length) + " consolidated projects · identical repos reached through worktrees, clones or symlinks share one id"}));
    container.appendChild(rangeChips(ctx));
    var note = loadingNote();
    if (note) { container.appendChild(note); }

    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      C.tableOf(
        ["project", {label: "sessions", num: true}, {label: "active", num: true}, {label: "cost", num: true},
          {label: "success", num: true}, {label: "tool errors", num: true}, "last activity", "agents"],
        entries.map(function (entry) {
          var agents = Object.keys(entry.stats.agents).sort().map(function (agent) {
            return el("span", {class: "tag", text: agent + " " + entry.stats.agents[agent]});
          });
          return [
            el("a", {href: C.buildHash("project", entry.id, params(ctx)), text: entry.meta.name}),
            {num: true, text: fmt.int(entry.stats.sessions)},
            {num: true, text: fmt.duration(entry.stats.activeS)},
            {num: true, text: C.costTotal(entry.stats), title: C.costTotalTitle(entry.stats)},
            {num: true, text: fmt.pct(entry.stats.successRate)},
            {num: true, text: fmt.pct(entry.stats.errorRate, 1)},
            fmt.ago(entry.stats.last),
            {node: el("span", null, agents)}
          ];
        }),
        {
          sortIndex: sortIndex, sortDescending: descending,
          onSort: function (index) {
            navigate(ctx, {psort: String(index), pdir: (index === sortIndex && descending) ? "asc" : "desc"});
          }
        }
      )
    ]));
  };

  views.project = function (container, ctx) {
    if (includeExcluded(ctx)) { C.loadExcluded(); }
    var projectId = ctx.id;
    var rows = C.filterRows(filterOptions(ctx, {projects: [projectId], rollupSubagents: false}));
    var stats = C.aggregate(rows);
    var meta = rows.length ? C.projectMeta(rows[0]) : {id: projectId, name: projectId, source: "unknown"};

    container.appendChild(el("h1", {text: meta.name}));
    container.appendChild(el("p", {class: "sub"}, [
      el("span", {class: "mono", text: meta.id}), " · resolved by " + (meta.source || "unknown"),
      meta.remote ? " · " + meta.remote : ""
    ]));
    container.appendChild(rangeChips(ctx));

    container.appendChild(el("div", {class: "grid cards", style: "margin-top:14px"}, [
      C.kpi("sessions", fmt.int(stats.sessions)),
      C.kpi("active time", fmt.duration(stats.activeS)),
      C.kpi("est. cost", C.costTotal(stats), null,
        stats.unpriced ? fmt.int(stats.unpriced) + " unpriced" : null),
      C.kpi("tool calls", fmt.int(stats.tools)),
      C.kpi("file edits", fmt.int(stats.edits)),
      C.kpi("success rate", fmt.pct(stats.successRate)),
      C.kpi("corrections", fmt.int(stats.corrections)),
      C.kpi("last activity", fmt.ago(stats.last))
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("by agent / client", [breakdownTable(rows)]),
      C.panel("outcome mix", [C.mixBar(stats.outcomes, {order: OUTCOME_ORDER}), el("h3", {style: "margin-top:12px", text: "work kinds"}), C.mixBar(stats.work)])
    ]));

    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "activity"}),
      C.barChart(C.dailySeries(rows, Math.min(C.rangeDays(rangeKey(ctx)) || 90, 180)).map(function (point) {
        return {label: point.day, value: point.sessions};
      }), {format: fmt.int})
    ]));

    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "sessions"}),
      recentTable(topRecent(rows, 25)),
      el("p", null, [el("a", {
        href: C.buildHash("sessions", "", merge(params(ctx), {project: projectId})),
        text: "open all " + fmt.int(rows.length) + " in the sessions explorer →"
      })])
    ]));
  };

  function breakdownTable(rows) {
    var groups = C.groupBy(rows, function (row) { return C.agentName(row) + " / " + C.clientName(row); });
    var entries = Object.keys(groups).sort();
    return C.tableOf(
      ["agent / client", {label: "sessions", num: true}, {label: "active", num: true}, {label: "cost", num: true}, {label: "success", num: true}],
      entries.map(function (key) {
        var stats = C.aggregate(groups[key]);
        return [
          key,
          {num: true, text: fmt.int(stats.sessions)},
          {num: true, text: fmt.duration(stats.activeS)},
          {num: true, text: C.costTotal(stats), title: C.costTotalTitle(stats)},
          {num: true, text: fmt.pct(stats.successRate)}
        ];
      })
    );
  }

  /* ---------------- sessions explorer -------------------------------------- */

  var SESSION_COLUMNS = [
    {label: "when", field: "started", cls: "c-when"},
    {label: "agent", field: "agent", cls: "c-agent"},
    {label: "project", field: "project", cls: "c-project"},
    {label: "title", field: "title", cls: "c-title"},
    {label: "dur", field: "duration", cls: "c-duration c-num"},
    {label: "cost", field: "cost", cls: "c-cost c-num"},
    {label: "outcome", field: "outcome", cls: "c-outcome"},
    {label: "turns", field: "turns", cls: "c-turns c-num"}
  ];

  views.sessions = function (container, ctx) {
    container.appendChild(el("h1", {text: "Sessions"}));
    var countNode = el("p", {class: "sub", text: "…"});
    var filterHost = el("div");
    container.appendChild(filterHost);
    container.appendChild(countNode);

    var shell = el("div", {class: "sessionlist"});
    var head = el("div", {class: "listhead", role: "row"}, SESSION_COLUMNS.map(function (column) {
      return sortHeader(ctx, column);
    }));
    var viewport = el("div", {class: "viewport", tabindex: "0", role: "list", "aria-label": "sessions"});
    shell.appendChild(head);
    shell.appendChild(viewport);
    container.appendChild(shell);

    var selected = -1;
    var rows = [];
    var list = null;

    function renderRow(row, index) {
      var title = row[C.COL.title] || row[C.COL.key];
      var project = C.projectMeta(row);
      var node = el("div", {
        class: "listrow" + (C.flag(row, "excluded") ? " excluded" : ""),
        role: "listitem", tabindex: "-1",
        onclick: function () { C.go("session", row[C.COL.key], {}); }
      }, [
        el("span", {class: "c-when sub", text: fmt.dateTime(row[C.COL.started])}),
        el("span", {class: "c-agent", text: C.agentName(row), title: C.agentName(row) + " · " + C.clientName(row)}),
        el("span", {class: "c-project sub", text: project.name, title: project.id}),
        el("span", {class: "c-title", title: title}, [
          C.flag(row, "subagent") ? el("span", {class: "muted", text: "↳ "}) : null,
          title,
          C.store.childCount[row[C.COL.key]]
            ? el("span", {class: "tag", text: "+" + C.store.childCount[row[C.COL.key]] + " sub"})
            : null
        ]),
        el("span", {class: "c-duration c-num", text: fmt.duration(row[C.COL.duration])}),
        el("span", {class: "c-cost c-num"}, [C.costCell(row)]),
        el("span", {class: "c-outcome"}, [C.pill(C.outcomeName(row))]),
        el("span", {class: "c-turns c-num", text: fmt.int(row[C.COL.turns])})
      ]);
      if (index === selected) { node.setAttribute("aria-selected", "true"); }
      return node;
    }

    function refresh() {
      C.clear(filterHost);
      filterHost.appendChild(filterBar(ctx, scopeRows(ctx)));
      rows = C.filterRows(filterOptions(ctx, {rollupSubagents: rollup(ctx)}));
      sortRows(rows, params(ctx).sort || "started", params(ctx).dir !== "asc");
      var loading = includeExcluded(ctx) && !C.store.excludedReady;
      countNode.textContent = fmt.int(rows.length) + " sessions" +
        (!C.store.ready || loading
          ? " (indexing " + fmt.int(C.store.rows.length) + " of " +
            fmt.int(includeExcluded(ctx) ? C.store.total : C.store.included) + "…)"
          : "") +
        (includeExcluded(ctx) ? " · excluded included" : " · " + fmt.int(C.store.excludedTotal) + " excluded hidden") +
        (rollup(ctx) ? " · subagents rolled up" : "");
      if (list) { list.refresh(rows, true); } else { list = C.virtualList(viewport, rows, renderRow); }
      syncScrollbarGutter();
    }

    function syncScrollbarGutter() {
      var gutter = Math.max(0, viewport.offsetWidth - viewport.clientWidth);
      shell.style.setProperty("--sbw", gutter + "px");
    }

    global.addEventListener("resize", function () {
      syncScrollbarGutter();
      if (list) { list.render(); }
    });

    viewport.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        selected = Math.max(0, Math.min(rows.length - 1, selected + (event.key === "ArrowDown" ? 1 : -1)));
        viewport.scrollTop = Math.max(0, selected * C.ROW_HEIGHT - viewport.clientHeight / 2);
        list.render();
      } else if (event.key === "Enter" && selected >= 0 && rows[selected]) {
        C.go("session", rows[selected][C.COL.key], {});
      }
    });

    if (includeExcluded(ctx)) { C.loadExcluded(); }
    refresh();
    ctx.onStoreChange = refresh;
  };

  function sortHeader(ctx, column) {
    var active = (params(ctx).sort || "started") === column.field;
    function apply() {
      navigate(ctx, {
        sort: column.field,
        dir: active && params(ctx).dir !== "asc" ? "asc" : "desc"
      });
    }
    return el("span", {
      class: column.cls,
      text: column.label + (active ? (params(ctx).dir === "asc" ? " ▲" : " ▼") : ""),
      role: "columnheader",
      "aria-sort": active ? (params(ctx).dir === "asc" ? "ascending" : "descending") : "none",
      tabindex: "0",
      onclick: apply,
      onkeydown: function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); apply(); }
      }
    });
  }

  function sortRows(rows, field, descending) {
    var column = C.COL[field];
    var accessor;
    if (field === "project") { accessor = function (row) { return C.projectMeta(row).name.toLowerCase(); }; }
    else if (field === "agent") { accessor = function (row) { return C.agentName(row); }; }
    else if (field === "outcome") { accessor = function (row) { return C.outcomeName(row); }; }
    else if (field === "title") { accessor = function (row) { return (row[C.COL.title] || "").toLowerCase(); }; }
    else { accessor = function (row) { var value = row[column]; return value === null || value === undefined ? -Infinity : value; }; }
    rows.sort(function (a, b) {
      var left = accessor(a), right = accessor(b);
      if (left < right) { return descending ? 1 : -1; }
      if (left > right) { return descending ? -1 : 1; }
      return 0;
    });
  }

  var FACET_OPTION_CAP = 40;

  function scopeRows(ctx) {
    return C.filterRows({includeExcluded: includeExcluded(ctx), since: C.rangeStart(rangeKey(ctx))});
  }

  function filterBar(ctx, scope) {
    var current = params(ctx);
    function pair(key, value) { var out = {}; out[key] = value; return out; }

    // Facets list only values present in the current scope, most common first,
    // so a 30k-option project dropdown can never happen.
    function facetSelect(label, key, accessor, labeller) {
      var entries = C.facetCounts(scope, accessor);
      var options = [el("option", {value: "", text: "all " + label + " (" + fmt.int(entries.length) + ")"})];
      var shown = entries.slice(0, FACET_OPTION_CAP);
      var selectedPresent = !current[key];
      shown.forEach(function (entry) {
        if (entry.value === current[key]) { selectedPresent = true; }
        options.push(el("option", {
          value: entry.value,
          text: (labeller ? labeller(entry.value) : entry.value) + " (" + fmt.int(entry.count) + ")",
          selected: entry.value === current[key] ? "selected" : null
        }));
      });
      if (!selectedPresent) {
        options.push(el("option", {
          value: current[key],
          text: (labeller ? labeller(current[key]) : current[key]) + " (0 here)",
          selected: "selected"
        }));
      }
      if (entries.length > shown.length) {
        options.push(el("option", {value: "", disabled: "disabled", text: "… " + fmt.int(entries.length - shown.length) + " more — use search"}));
      }
      var node = el("select", {
        "aria-label": label,
        onchange: function (event) { navigate(ctx, pair(key, event.target.value)); }
      }, options);
      return el("label", null, [label, node]);
    }

    // Projects and models can be unbounded: type-ahead over a capped datalist.
    function facetTypeAhead(label, key, accessor, resolve) {
      var entries = C.facetCounts(scope, accessor);
      var listId = "facet-" + key;
      var datalist = el("datalist", {id: listId});
      function fill(query) {
        C.clear(datalist);
        var needle = (query || "").toLowerCase();
        var shown = 0;
        for (var i = 0; i < entries.length && shown < 200; i++) {
          var entry = entries[i];
          var text = resolve ? resolve(entry.value) : entry.value;
          if (needle && (text + " " + entry.value).toLowerCase().indexOf(needle) === -1) { continue; }
          datalist.appendChild(el("option", {value: entry.value, label: text + " (" + entry.count + ")"}));
          shown += 1;
        }
      }
      fill("");
      var input = el("input", {
        type: "search", class: "facet", list: listId, value: current[key] || "",
        placeholder: "any " + label + " (" + fmt.int(entries.length) + ")",
        "aria-label": label
      });
      var timer = null;
      input.addEventListener("input", function () {
        fill(input.value);
        if (timer) { clearTimeout(timer); }
        var value = input.value;
        var known = false;
        for (var i = 0; i < entries.length; i++) { if (entries[i].value === value) { known = true; break; } }
        if (!value || known) {
          timer = setTimeout(function () { navigate(ctx, pair(key, value)); }, 160);
        }
      });
      input.addEventListener("change", function () { navigate(ctx, pair(key, input.value)); });
      return el("label", null, [label, input, datalist]);
    }

    var search = el("input", {
      type: "search", id: "session-search", placeholder: "search title and first prompt…",
      value: current.q || "", "aria-label": "search sessions"
    });
    var searchTimer = null;
    search.addEventListener("input", function () {
      if (searchTimer) { clearTimeout(searchTimer); }
      searchTimer = setTimeout(function () { navigate(ctx, {q: search.value}); }, 220);
    });

    return el("div", {class: "filters"}, [
      el("label", null, ["search", search]),
      facetSelect("agent", "agent", C.agentName),
      facetSelect("client", "client", C.clientName),
      facetTypeAhead("project", "project", function (row) { return C.projectMeta(row).id; }, projectLabel),
      facetSelect("tag", "tag", function (row) { return C.tagNames(row); }),
      facetSelect("outcome", "outcome", C.outcomeName),
      facetSelect("work", "work", C.workName),
      facetTypeAhead("model", "model", function (row) { return C.modelNames(row); }),
      el("label", null, ["mode", el("select", {
        "aria-label": "interaction mode",
        onchange: function (event) { navigate(ctx, {mode: event.target.value}); }
      }, [
        el("option", {value: "", text: "all"}),
        el("option", {value: "interactive", text: "interactive", selected: current.mode === "interactive" ? "selected" : null}),
        el("option", {value: "automated", text: "automated", selected: current.mode === "automated" ? "selected" : null})
      ])]),
      el("label", null, ["from", el("input", {
        type: "date", value: current.from || "",
        onchange: function (event) { navigate(ctx, {from: event.target.value, range: "all"}); }
      })]),
      el("label", null, ["to", el("input", {
        type: "date", value: current.to || "",
        onchange: function (event) { navigate(ctx, {to: event.target.value, range: "all"}); }
      })]),
      el("div", {class: "chips"}, [
        el("button", {
          type: "button", class: "chip", text: "include excluded", "aria-pressed": includeExcluded(ctx) ? "true" : "false",
          onclick: function () { navigate(ctx, {inc: includeExcluded(ctx) ? "0" : "1"}); }
        }),
        el("button", {
          type: "button", class: "chip", text: "roll up subagents", "aria-pressed": rollup(ctx) ? "true" : "false",
          onclick: function () { navigate(ctx, {roll: rollup(ctx) ? "0" : "1"}); }
        }),
        el("button", {type: "button", class: "chip", text: "reset", onclick: function () { C.go("sessions", "", {}); }})
      ])
    ]);
  }

  function projectLabel(id) {
    var projects = C.DICT.projects;
    for (var i = 0; i < projects.length; i++) {
      if (projects[i] && projects[i].id === id) { return projects[i].name; }
    }
    return id;
  }

  /* ---------------- session detail ------------------------------------------ */

  views.session = function (container, ctx) {
    var key = ctx.id;
    container.appendChild(el("p", null, [el("a", {href: C.buildHash("sessions", "", params(ctx)), text: "← sessions"})]));
    var host = el("div", {class: "empty", text: "loading session…"});
    container.appendChild(host);

    function attempt() {
      var row = C.store.byKey[key];
      if (!row && (!C.store.ready || !C.store.excludedReady)) {
        host.textContent = "loading the session index… " + fmt.int(C.store.rows.length) + " sessions so far";
        ctx.onStoreChange = attempt;
        if (C.store.ready) { C.loadExcluded(); }
        return;
      }
      ctx.onStoreChange = null;
      C.loadDetail(key, function (detail) {
        C.clear(host);
        host.className = "";
        if (!detail) {
          host.className = "empty";
          host.textContent = "session " + key + " is not in this build";
          return;
        }
        renderSessionDetail(host, detail, row, ctx);
      });
    }

    attempt();
  };

  function renderSessionDetail(host, detail, row, ctx) {
    var definitions = {};
    (C.manifest.signals.definitions || []).forEach(function (definition) { definitions[definition.id] = definition; });

    host.appendChild(el("h1", {text: detail.title || detail.key}));
    host.appendChild(el("p", {class: "sub"}, [
      el("a", {href: C.buildHash("project", detail.project.id, {}), text: detail.project.name}),
      " · " + detail.agent + " / " + detail.client,
      detail.excluded ? el("span", {class: "tag", text: "excluded"}) : null
    ]));
    if (detail.minimal) {
      host.appendChild(el("p", {class: "muted"},
        "This session is excluded, so only its catalog record is published. " +
        "Signals and token usage are not computed for excluded sessions."));
    }

    var git = detail.git || {};
    host.appendChild(el("div", {class: "grid two"}, [
      C.panel("session", [el("dl", {class: "kv"}, [
        el("dt", {text: "key"}), el("dd", {class: "mono", text: detail.key}),
        el("dt", {text: "started"}), el("dd", {text: detail.started_at || "–"}),
        el("dt", {text: "ended"}), el("dd", {text: detail.ended_at || "–"}),
        el("dt", {text: "duration"}), el("dd", {text: fmt.duration(detail.duration_s)}),
        el("dt", {text: "active"}), el("dd", {text: fmt.duration(detail.active_s)}),
        el("dt", {text: "cost"}), el("dd", null, [row ? C.costCell(row) : "—"]),
        el("dt", {text: "models"}), el("dd", {text: (detail.models || []).join(", ") || "—"}),
        el("dt", {text: "branch"}), el("dd", {text: git.branch || "—"}),
        el("dt", {text: "remote"}), el("dd", {class: "mono", text: git.remote || "—"}),
        el("dt", {text: "cwd"}), el("dd", {class: "mono", text: detail.cwd || "–"}),
        el("dt", {text: "interactive"}), el("dd", {text: detail.interactive === null ? "unknown" : (detail.interactive ? "yes" : "no")})
      ])]),
      C.panel("tags and provenance", [
        tagList(detail),
        detail.markers && detail.markers.length ? el("h3", {style: "margin-top:12px", text: "inline markers"}) : null,
        detail.markers && detail.markers.length ? C.tableOf(["directive", "value", "event"], detail.markers.map(function (marker) {
          return [marker.directive, marker.value || "–", {node: el("span", {class: "mono", text: marker.event_id || "–"})}];
        })) : null,
        el("h3", {style: "margin-top:12px", text: "artifacts"}),
        artifactLinks(detail)
      ])
    ]));

    if (detail.parent || (detail.children && detail.children.length)) {
      host.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
        el("h2", {text: "session tree"}),
        detail.parent ? el("p", null, ["parent: ", el("a", {href: C.buildHash("session", detail.parent, {}), text: detail.parent})]) : null,
        detail.children && detail.children.length ? el("ul", null, detail.children.map(function (child) {
          var childRow = C.store.byKey[child];
          return el("li", null, [el("a", {href: C.buildHash("session", child, {}), text: (childRow && childRow[C.COL.title]) || child})]);
        })) : null
      ]));
    }

    if (detail.prompt) {
      host.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
        el("h2", {text: "first prompt"}),
        el("p", {class: "mono", style: "white-space:pre-wrap", text: detail.prompt})
      ]));
    }

    var signals = detail.signals;
    if (!signals) {
      host.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
        el("h2", {text: "signals"}),
        el("p", {class: "muted", text: detail.excluded ? "excluded sessions are not scored" : "no signal record for this session"})
      ]));
    } else {
      var grouped = {};
      Object.keys(signals.values).sort().forEach(function (signalId) {
        var definition = definitions[signalId];
        var category = definition ? definition.category : "other";
        (grouped[category] = grouped[category] || []).push(signalId);
      });
      var panels = Object.keys(grouped).sort().map(function (category) {
        return C.panel(category, [C.tableOf(
          ["signal", {label: "value", num: true}, "evidence"],
          grouped[category].map(function (signalId) {
            var definition = definitions[signalId];
            var value = signals.values[signalId];
            var evidence = (signals.evidence || {})[signalId] || [];
            var confidence = (signals.confidence || {})[signalId];
            return [
              {node: el("span", {title: definition ? definition.description : signalId}, [
                definition ? definition.title : signalId,
                confidence !== undefined ? el("span", {class: "evidence", text: " · confidence " + fmt.pct(confidence)}) : null
              ])},
              {num: true, node: el("span", {class: polarityClass(definition, value), text: fmt.signal(value, definition)})},
              {node: el("span", {class: "mono evidence", text: evidence.slice(0, 5).join(" ") || "–"})}
            ];
          })
        )]);
      });
      host.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, panels));
    }

    host.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "counts"}),
      C.tableOf(["metric", {label: "value", num: true}], Object.keys(detail.counts || {}).map(function (metric) {
        return [metric, {num: true, text: fmt.int(detail.counts[metric])}];
      }))
    ]));
    var _ = ctx;
  }

  function polarityClass(definition, value) {
    if (!definition || typeof value !== "number" || definition.polarity === "neutral") { return null; }
    if (definition.kind === "ratio" || definition.kind === "count") {
      if (definition.polarity === "lower_better") { return value > 0 ? "polarity-bad" : "polarity-good"; }
      return value > 0 ? "polarity-good" : null;
    }
    return null;
  }

  function tagList(detail) {
    var tags = detail.tags || [];
    if (!tags.length) { return el("p", {class: "muted", text: "no tags"}); }
    return C.tableOf(["tag", "why"], tags.map(function (tag) {
      var sources = (detail.tag_sources || {})[tag] || [];
      return [{node: el("span", {class: "tag", text: tag})}, sources.join(", ") || "unknown"];
    }));
  }

  function artifactLinks(detail) {
    var base = (C.manifest.archive && C.manifest.archive.url) || "";
    var paths = detail.paths || {};
    function link(label, relative) {
      if (!relative) { return null; }
      var href = base ? base.replace(/\/$/, "") + "/" + relative : null;
      return el("li", null, [
        href ? el("a", {href: href, text: label}) : el("span", {text: label}),
        el("div", {class: "mono evidence", text: relative})
      ]);
    }
    return el("ul", null, [
      link("rendered transcript (markdown)", paths.transcript),
      link("normalized events", paths.normalized),
      link("raw capture", paths.raw)
    ]);
  }

  /* ---------------- signals -------------------------------------------------- */

  views.signals = function (container, ctx) {
    var definitions = C.manifest.signals.definitions || [];
    var summary = C.manifest.signals.summary || {};
    container.appendChild(el("h1", {text: "Signals"}));
    container.appendChild(el("p", {class: "sub", text:
      definitions.length + " signals · version " + (C.manifest.signals.version || "unknown") +
      " · computed over included sessions only"}));
    if (!definitions.length) {
      container.appendChild(el("div", {class: "empty", text: "no signal definitions found; run `retro signal update`"}));
      return;
    }
    var byCategory = {};
    definitions.forEach(function (definition) {
      (byCategory[definition.category] = byCategory[definition.category] || []).push(definition);
    });
    Object.keys(byCategory).sort().forEach(function (category) {
      container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
        el("h2", {text: category}),
        C.tableOf(
          ["signal", "kind", {label: "n", num: true}, "summary", "polarity"],
          byCategory[category].map(function (definition) {
            var stats = summary[definition.id] || {n: 0};
            return [
              {node: el("a", {href: C.buildHash("signal", definition.id, params(ctx)), text: definition.title,
                title: definition.description})},
              definition.kind + (definition.unit ? " (" + definition.unit + ")" : ""),
              {num: true, text: fmt.int(stats.n)},
              {node: el("span", {text: summaryText(definition, stats)})},
              definition.polarity
            ];
          })
        )
      ]));
    });
  };

  function summaryText(definition, stats) {
    if (!stats || !stats.n) { return "no values"; }
    if (stats.true_rate !== undefined) { return fmt.pct(stats.true_rate) + " true"; }
    if (stats.counts) {
      return Object.keys(stats.counts).sort(function (a, b) { return stats.counts[b] - stats.counts[a]; })
        .slice(0, 3).map(function (key) { return key + " " + stats.counts[key]; }).join(" · ");
    }
    return "median " + fmt.signal(stats.median, definition) + " · p90 " + fmt.signal(stats.p90, definition) +
      " · total " + fmt.signal(stats.sum, definition);
  }

  views.signal = function (container, ctx) {
    var signalId = ctx.id;
    var definitions = {};
    (C.manifest.signals.definitions || []).forEach(function (definition) { definitions[definition.id] = definition; });
    var definition = definitions[signalId];
    container.appendChild(el("p", null, [el("a", {href: C.buildHash("signals", "", params(ctx)), text: "← signals"})]));
    if (!definition) {
      container.appendChild(el("div", {class: "empty", text: "unknown signal " + signalId}));
      return;
    }
    container.appendChild(el("h1", {text: definition.title}));
    container.appendChild(el("p", {class: "sub", text: definition.description + " · " + definition.category +
      " · " + definition.kind + " · " + definition.method + " · polarity " + definition.polarity}));
    container.appendChild(rangeChips(ctx));
    var body = el("div", {class: "empty", text: "loading signal values…"});
    container.appendChild(body);

    C.loadSignalMatrix(function (matrix) {
      C.clear(body);
      body.className = "";
      var column = matrix[signalId];
      if (!column) {
        body.className = "empty";
        body.textContent = "no values recorded for this signal";
        return;
      }
      var rows = C.filterRows(filterOptions(ctx, {rollupSubagents: false}));
      var encoding = (C.manifest.signals.encodings || {})[signalId] || {encoding: "num", categories: []};
      var positions = Object.create(null);
      for (var i = 0; i < C.store.rows.length; i++) { positions[C.store.rows[i][C.COL.key]] = i; }

      var samples = [];
      rows.forEach(function (row) {
        var value = column[positions[row[C.COL.key]]];
        if (value === undefined) { return; }
        samples.push({row: row, value: value});
      });
      if (!samples.length) {
        body.className = "empty";
        body.textContent = "no sessions in this range have this signal";
        return;
      }

      body.appendChild(el("div", {class: "grid two"}, [
        C.panel("distribution", [distribution(definition, encoding, samples)]),
        C.panel("compare", [comparisonControls(ctx), comparison(definition, encoding, samples, params(ctx).by || "agent")])
      ]));

      var extremes = samples.slice().sort(function (a, b) { return b.value - a.value; });
      body.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
        el("h2", {text: "highest values"}),
        C.tableOf(["session", "project", "agent", {label: "value", num: true}], extremes.slice(0, 12).map(function (sample) {
          return [
            sessionLink(sample.row), C.projectMeta(sample.row).name, C.agentName(sample.row),
            {num: true, text: decodeValue(definition, encoding, sample.value)}
          ];
        }))
      ]));
    });
  };

  function decodeValue(definition, encoding, value) {
    if (encoding.encoding === "bool") { return value ? "yes" : "no"; }
    if (encoding.encoding === "cat") { return encoding.categories[value] !== undefined ? encoding.categories[value] : String(value); }
    return fmt.signal(value, definition);
  }

  function distribution(definition, encoding, samples) {
    if (encoding.encoding === "cat" || encoding.encoding === "bool") {
      var counts = Object.create(null);
      samples.forEach(function (sample) {
        var label = decodeValue(definition, encoding, sample.value);
        counts[label] = (counts[label] || 0) + 1;
      });
      return C.mixBar(counts, {label: definition.title, order: definition.categories || OUTCOME_ORDER});
    }
    var values = samples.map(function (sample) { return sample.value; }).sort(function (a, b) { return a - b; });
    return el("div", null, [
      C.histogram(values, {label: definition.title, format: function (value) { return fmt.signal(value, definition); }}),
      el("dl", {class: "kv", style: "margin-top:10px"}, [
        el("dt", {text: "n"}), el("dd", {text: fmt.int(values.length)}),
        el("dt", {text: "median"}), el("dd", {text: fmt.signal(C.percentile(values, 0.5), definition)}),
        el("dt", {text: "p90"}), el("dd", {text: fmt.signal(C.percentile(values, 0.9), definition)}),
        el("dt", {text: "max"}), el("dd", {text: fmt.signal(values[values.length - 1], definition)})
      ])
    ]);
  }

  function comparisonControls(ctx) {
    var dimensions = ["agent", "client", "project", "model", "work"];
    var current = params(ctx).by || "agent";
    return el("div", {class: "chips", style: "margin-bottom:10px"}, dimensions.map(function (dimension) {
      return el("button", {
        type: "button", class: "chip", text: dimension, "aria-pressed": dimension === current ? "true" : "false",
        onclick: function () { navigate(ctx, {by: dimension}); }
      });
    }));
  }

  function comparison(definition, encoding, samples, dimension) {
    var classify = {
      agent: function (row) { return C.agentName(row); },
      client: function (row) { return C.clientName(row); },
      project: function (row) { return C.projectMeta(row).name; },
      work: function (row) { return C.workName(row); },
      model: function (row) { var models = C.modelNames(row); return models.length ? models[0] : "unknown"; }
    }[dimension] || function (row) { return C.agentName(row); };

    var groups = Object.create(null);
    samples.forEach(function (sample) {
      var key = classify(sample.row);
      (groups[key] = groups[key] || []).push(sample.value);
    });
    var entries = Object.keys(groups).map(function (key) {
      var values = groups[key].slice().sort(function (a, b) { return a - b; });
      var total = 0;
      for (var i = 0; i < values.length; i++) { total += values[i]; }
      return {
        key: key, n: values.length,
        mean: total / values.length,
        median: C.percentile(values, 0.5),
        p90: C.percentile(values, 0.9),
        sum: total
      };
    }).filter(function (entry) { return entry.n >= MIN_SAMPLE; });
    var dropped = Object.keys(groups).length - entries.length;
    if (!entries.length) {
      return el("p", {class: "muted", text: "no " + dimension + " has at least " + MIN_SAMPLE + " scored sessions in this range"});
    }
    var better = definition.polarity === "lower_better" ? 1 : -1;
    entries.sort(function (a, b) { return better * (a.median - b.median); });
    var isCategorical = encoding.encoding !== "num";
    return el("div", null, [
      C.tableOf(
        [dimension, {label: "n", num: true}, {label: isCategorical ? "share" : "median", num: true},
          {label: isCategorical ? "" : "p90", num: true}, {label: isCategorical ? "" : "total", num: true}],
        entries.map(function (entry, index) {
          var highlight = index === 0 && definition.polarity !== "neutral" ? "polarity-good" : null;
          return [
            entry.key,
            {num: true, text: fmt.int(entry.n)},
            {num: true, node: el("span", {class: highlight, text: isCategorical
              ? fmt.pct(entry.mean) : fmt.signal(entry.median, definition)})},
            {num: true, text: isCategorical ? "" : fmt.signal(entry.p90, definition)},
            {num: true, text: isCategorical ? "" : fmt.signal(entry.sum, definition)}
          ];
        })
      ),
      dropped ? el("p", {class: "muted", text: dropped + " groups hidden (fewer than " + MIN_SAMPLE + " sessions)"}) : null
    ]);
  }

  /* ---------------- data quality --------------------------------------------- */

  views.quality = function (container, ctx) {
    var quality = C.manifest.quality || {};
    var sync = C.manifest.sync || {};
    var cost = quality.cost || {};
    container.appendChild(el("h1", {text: "Data quality and capture"}));
    container.appendChild(el("p", {class: "sub", text: "what the archive contains, what was filtered out, and whether capture is healthy"}));

    container.appendChild(el("div", {class: "grid cards"}, [
      C.kpi("sessions", fmt.int(quality.sessions)),
      C.kpi("included", fmt.int(quality.included)),
      C.kpi("excluded", fmt.int(quality.excluded), null, fmt.pct(quality.sessions ? quality.excluded / quality.sessions : 0) + " of archive"),
      C.kpi("scored", fmt.int(quality.with_signals), null, "sessions with signals"),
      C.kpi("priced", fmt.int(cost.priced), null, fmt.int(cost.unpriced) + " without a cost estimate"),
      C.kpi("unknown events", fmt.pct(quality.unknown_rate, 2), null, fmt.compact(quality.unknown_events) + " of " + fmt.compact(quality.events)),
      C.kpi("interactive", fmt.int(quality.interactive), null, fmt.int(quality.automated) + " automated"),
      C.kpi("stale signals", fmt.int(quality.stale_signals), null, fmt.int(quality.missing_signals) + " included but unscored")
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("why sessions are excluded", [
        (quality.excluded_by_tag || []).length ? C.tableOf(
          ["tag", {label: "sessions", num: true}, "sources", "examples"],
          (quality.excluded_by_tag || []).map(function (entry) {
            var sources = el("span", {class: "chips"}, Object.keys(entry.sources || {}).map(function (source) {
              return el("span", {class: "tag", text: source + " " + entry.sources[source]});
            }));
            var examples = el("span", null, (entry.examples || []).slice(0, 3).map(function (key) {
              return el("div", null, [el("a", {
                class: "mono", href: C.buildHash("session", key, {}), text: shortKey(key), title: key
              })]);
            }));
            return [
              {node: el("span", {class: "tag", text: entry.tag})},
              {num: true, text: fmt.int(entry.count)},
              {node: sources},
              {node: examples, cls: "keys"}
            ];
          })
        ) : el("p", {class: "muted", text: "nothing is excluded"})
      ]),
      C.panel("capture by host", [
        C.tableOf(
          ["host", {label: "sessions", num: true}, {label: "events", num: true},
            {label: "unknown", num: true}, {label: "no transcript", num: true}],
          (quality.hosts || []).map(function (entry) {
            return [
              entry.host,
              {num: true, text: fmt.int(entry.sessions)},
              {num: true, text: fmt.compact(entry.events)},
              {num: true, text: fmt.pct(entry.events ? entry.unknown_events / entry.events : 0, 2)},
              {num: true, text: fmt.int(entry.missing_transcript)}
            ];
          })
        )
      ])
    ]));

    container.appendChild(el("div", {class: "grid two", style: "margin-top:14px"}, [
      C.panel("cost coverage", [
        (cost.basis || []).length ? C.tableOf(
          ["basis", {label: "sessions", num: true}, "meaning", "examples"],
          (cost.basis || []).map(function (entry) {
            return [
              {node: el("span", {class: "tag", text: entry.basis})},
              {num: true, text: fmt.int(entry.count)},
              C.COST_BASIS_REASON[entry.basis] || "unknown basis",
              {cls: "keys", node: el("span", null, (entry.examples || []).slice(0, 2).map(function (key) {
                return el("div", null, [el("a", {class: "mono", href: C.buildHash("session", key, {}), text: shortKey(key), title: key})]);
              }))}
            ];
          })
        ) : el("p", {class: "muted", text: "every included session has a cost estimate"}),
        el("p", {class: "muted", text:
          "Sessions without an estimate show “—”, never $0.00, and are excluded from cost averages."})
      ]),
      C.panel("tags", [
        C.tableOf(["tag", {label: "sessions", num: true}], (quality.tags || []).slice(0, 20).map(function (entry) {
          return [
            {node: el("a", {href: C.buildHash("sessions", "", {tag: entry.tag, inc: "1", range: "all"}), text: entry.tag})},
            {num: true, text: fmt.int(entry.count)}
          ];
        }))
      ])
    ]));

    var latest = sync.latest;
    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "latest sync"}),
      latest ? el("div", null, [
        el("p", null, [
          el("span", {class: "pill " + healthPill(latest.health), text: syncHeadline(latest)}),
          latest.started_at ? " started " + latest.started_at : "",
          latest.completed_at ? " · finished " + latest.completed_at : ""
        ]),
        latest.built_during_sync ? el("p", {class: "muted", text:
          "This dashboard was built by the dashboard stage of that run, so the report was still open when it " +
          "was read. Stages that had not finished at that point are listed as pending; the generation stays " +
          "published until the next rebuild."}) : null,
        stageTable(latest.stages, latest.stages_unfinished || []),
        C.tableOf(["metric", {label: "value", num: true}], [
          ["imported", {num: true, text: fmt.int(latest.imported)}],
          ["unchanged", {num: true, text: fmt.int(latest.unchanged)}],
          ["rendered", {num: true, text: fmt.int(latest.rendered)}],
          ["pending", {num: true, text: fmt.int(latest.pending)}],
          ["failures", {num: true, text: fmt.int(latest.failure_count)}],
          ["free disk", {num: true, text: latest.free_bytes ? (latest.free_bytes / (1024 * 1024 * 1024)).toFixed(1) + " GiB" : "—"}],
          ["warning", latest.warning || "none"]
        ]),
        (latest.failures || []).length ? C.tableOf(["session", "error"], latest.failures.map(function (failure) {
          return [{node: el("span", {class: "mono", text: shortKey(failure.session), title: failure.session})}, failure.error];
        })) : null
      ]) : el("p", {class: "muted", text: "no sync reports in the archive"}),
      (sync.history || []).length > 1 ? el("div", null, [
        el("h3", {style: "margin-top:12px", text: "recent runs"}),
        C.tableOf(["started", "status", {label: "imported", num: true}, {label: "failures", num: true}],
          sync.history.map(function (entry) {
            return [entry.started_at, entry.status, {num: true, text: fmt.int(entry.imported)}, {num: true, text: fmt.int(entry.failure_count)}];
          }))
      ]) : null
    ]));

    container.appendChild(el("section", {class: "panel", style: "margin-top:14px"}, [
      el("h2", {text: "inputs"}),
      el("dl", {class: "kv"}, [
        el("dt", {text: "catalog"}), el("dd", {class: "mono", text: (quality.catalog || {}).path || "—"}),
        el("dt", {text: "catalog state"}), el("dd", {text: (quality.catalog || {}).missing ? "missing" : "ok" +
          ((quality.catalog || {}).malformed ? " · " + quality.catalog.malformed + " malformed lines" : "")}),
        el("dt", {text: "signal store"}), el("dd", {class: "mono", text: (quality.signals || {}).sessions_path || "—"}),
        el("dt", {text: "signal version"}), el("dd", {class: "mono", text: (quality.signals || {}).version || "—"}),
        el("dt", {text: "sync state"}), el("dd", {class: "mono", text: sync.state_path || "—"}),
        el("dt", {text: "markers"}), el("dd", {text: Object.keys(quality.markers || {}).map(function (directive) {
          return directive + " " + quality.markers[directive];
        }).join(", ") || "none"}),
        el("dt", {text: "built"}), el("dd", {text: C.manifest.generated_at || "—"})
      ])
    ]));
    var _ = ctx;
  };

  function shortKey(key) {
    var text = String(key || "");
    var slash = text.indexOf("/");
    if (slash === -1) { return text.length > 20 ? text.slice(0, 10) + "…" + text.slice(-6) : text; }
    var host = text.slice(0, slash);
    var id = text.slice(slash + 1);
    return host + "/" + (id.length > 14 ? id.slice(0, 8) + "…" + id.slice(-4) : id);
  }

  function healthPill(health) {
    if (health === "ok") { return "success"; }
    if (health === "degraded") { return "partial"; }
    return "failed";
  }

  function syncHeadline(latest) {
    if (latest.health === "ok") { return "sync ok"; }
    if (latest.health === "degraded") { return "sync degraded"; }
    return "sync failed";
  }

  function stageTable(stages, unfinished) {
    var names = Object.keys(stages || {});
    if (!names.length) { return el("p", {class: "muted", text: "this run did not record per-stage status"}); }
    return C.tableOf(
      ["stage", "status", {label: "duration", num: true}, "finished", "detail"],
      names.map(function (name) {
        var stage = stages[name];
        var detail = [];
        ["changed_keys", "total", "excluded", "rendered", "recomputed", "reused", "removed_keys"].forEach(function (key) {
          if (stage[key] !== undefined && stage[key] !== null) { detail.push(key + " " + fmt.int(stage[key])); }
        });
        if (stage.error) { detail.push(String(stage.error)); }
        var pending = (unfinished || []).indexOf(name) !== -1;
        return [
          name,
          {node: el("span", {
            class: "pill " + stageStyle(stage.status),
            text: pending ? "pending at build" : stage.status
          })},
          {num: true, text: stage.duration_seconds ? fmt.duration(stage.duration_seconds) : "—"},
          stage.completed_at || "—",
          detail.join(" · ") || "—"
        ];
      })
    );
  }

  function stageStyle(status) {
    if (status === "success" || status === "ok") { return "success"; }
    if (status === "running" || status === "in_progress" || status === "skipped") { return "unknown"; }
    if (status === "failed" || status === "error") { return "failed"; }
    return "unknown";
  }

  global.RETRO.views = views;
})(window);
