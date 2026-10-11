/* retro dashboard shell: routing, keyboard, theme, capture-health banner. */
(function (global) {
  "use strict";

  var C = global.RETRO.core;
  var el = C.el;
  var views = global.RETRO.views;
  var viewNode = document.getElementById("view");
  var bannerNode = document.getElementById("banner");
  var loadStateNode = document.getElementById("loadstate");
  var footerNode = document.getElementById("footer-build");
  var helpNode = document.getElementById("help");
  var NAV_PARENT = {project: "projects", session: "sessions", signal: "signals",
    benchmarks: "tasks", "benchmark-task": "tasks", task: "tasks",
    dataset: "evals", "benchmark-eval": "experiments", "agent-run": "experiments", experiment: "experiments"};
  var context = null;

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = document.getElementById("theme");
    button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    button.title = "Theme: " + theme + " (press t)";
    try { global.localStorage.setItem("retro-theme", theme); } catch (error) { /* file:// private mode */ }
  }

  function cycleTheme() {
    var order = ["auto", "light", "dark"];
    var current = document.documentElement.getAttribute("data-theme") || "auto";
    applyTheme(order[(order.indexOf(current) + 1) % order.length]);
  }

  function renderBanner() {
    var messages = (C.manifest.warnings || []).slice();
    var quality = C.manifest.quality || {};
    var sync = (C.manifest.sync || {}).latest;
    var tone = "warn";
    var note = null;

    if (sync) {
      // The dashboard stage runs inside the sync, so the report it was built
      // from is usually still open. Report the run in the past tense and only
      // raise a banner for stages that actually failed.
      if (sync.health === "failed" || sync.health === "degraded") {
        messages.push(
          "last sync " + sync.health +
          (sync.observed_at ? " at " + sync.observed_at : "") +
          ((sync.stages_failed || []).length ? " — failed stages: " + sync.stages_failed.join(", ") : "") +
          (sync.failure_count ? " — " + sync.failure_count + " session failures" : "") +
          " — see Data quality"
        );
      } else {
        note = stageNote(sync);
      }
      if (sync.warning) { messages.push(sync.warning); }
    }
    if (quality.unknown_rate > 0.25) {
      messages.push("unknown events are " + C.fmt.pct(quality.unknown_rate, 1) +
        " of all captured events — normalization coverage is incomplete");
    }

    C.clear(bannerNode);
    if (messages.length) {
      bannerNode.hidden = false;
      bannerNode.className = "banner-shell";
      var content = el("div", {class: "banner " + tone});
      var onlyBenchmarks = messages.every(function (message) { return /^\d+ benchmark artifact warnings/.test(message); });
      content.appendChild(el("strong", {text: onlyBenchmarks ? "benchmark verification" : "data status"}));
      content.appendChild(el("ul", null, messages.map(function (message) {
        if (/^\d+ benchmark artifact warnings/.test(message)) {
          return el("li", null, [
            "Some saved benchmark records need verification. This does not mean the latest verified tasks failed. ",
            el("a", {href: "#/experiments", text: "See affected versions and what to do"})
          ]);
        }
        return el("li", {text: message});
      })));
      if (note) { content.appendChild(el("div", {class: "muted", text: note})); }
      bannerNode.appendChild(C.dismissibleNotice(content, ["global", messages.slice().sort(),
        (C.manifest.benchmarks || {}).warning_sha256 || ""], "global warning"));
      return;
    }
    if (note) {
      bannerNode.hidden = false;
      bannerNode.className = "banner ok";
      bannerNode.appendChild(el("span", {text: note}));
      bannerNode.appendChild(el("a", {href: "#/quality", text: "details"}));
      return;
    }
    bannerNode.hidden = true;
  }

  function stageNote(sync) {
    // This generation was built during that run and stays published until the
    // next rebuild, so the banner always reports a past sync, never a live one.
    var when = sync.observed_at || sync.completed_at || sync.started_at;
    var epoch = epochOf(when);
    var label = epoch ? C.fmt.dateTime(epoch) + " (" + C.fmt.ago(epoch) + ")" : "unknown time";
    var stages = sync.stages || {};
    var order = ["capture", "catalog", "render", "signals", "memory", "dashboard"];
    var names = Object.keys(stages).sort(function (a, b) {
      var ai = order.indexOf(a), bi = order.indexOf(b);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
    var unfinished = sync.stages_unfinished || [];
    var parts = names.filter(function (name) {
      return unfinished.indexOf(name) === -1;
    }).map(function (name) {
      var status = String(stages[name].status);
      var mark = (status === "success" || status === "ok") ? "✓" : (status === "skipped" ? "–" : "✗");
      return name + " " + mark;
    });
    var note = "last sync " + label;
    if (parts.length) { note += ": " + parts.join("  "); }
    if (sync.built_during_sync) { note += " · this dashboard was built during that sync"; }
    return note;
  }

  function epochOf(value) {
    if (!value) { return null; }
    var parsed = Date.parse(value);
    return isNaN(parsed) ? null : parsed / 1000;
  }

  function renderLoadState() {
    if (!C.store.shardsTotal) { loadStateNode.textContent = ""; return; }
    if (C.store.ready) {
      loadStateNode.textContent = C.fmt.int(C.store.rows.length) + " sessions";
      return;
    }
    loadStateNode.textContent = "indexing " + C.fmt.int(C.store.rows.length) + " / " + C.fmt.int(C.store.total);
  }

  function highlightNav(route) {
    var target = NAV_PARENT[route] || route;
    var links = document.querySelectorAll(".nav a");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("data-route") === target) { links[i].setAttribute("aria-current", "page"); }
      else { links[i].removeAttribute("aria-current"); }
    }
  }

  function render() {
    var parsed = C.parseHash();
    var view = views[parsed.route] || views.overview;
    context = {route: parsed.route, id: parsed.id, params: parsed.params, onStoreChange: null};
    highlightNav(parsed.route);
    C.clear(viewNode);
    try {
      view(viewNode, context);
    } catch (error) {
      C.clear(viewNode);
      viewNode.appendChild(el("div", {class: "empty"}, [
        el("p", {text: "This view failed to render."}),
        el("p", {class: "mono", text: String(error && error.message ? error.message : error)})
      ]));
      if (global.console) { global.console.error(error); }
    }
    document.title = "retro · " + parsed.route;
  }

  function toggleHelp(force) {
    var open = force === undefined ? helpNode.hidden : force;
    if (!open) { helpNode.hidden = true; return; }
    C.clear(helpNode);
    helpNode.hidden = false;
    helpNode.appendChild(el("div", {class: "sheet"}, [
      el("h2", {text: "Keyboard"}),
      el("dl", {class: "kv"}, [
        el("dt", {text: "/"}), el("dd", {text: "search sessions"}),
        el("dt", {text: "g o / g p / g s / g i / g b / g e / g x / g q"}), el("dt", {text: ""}),
        el("dd", {text: "overview, projects, sessions, signals, tasks, evals, experiments, data quality"}), el("dd", {text: ""}),
        el("dt", {text: "↑ ↓ / Enter"}), el("dd", {text: "move and open rows in the sessions list"}),
        el("dt", {text: "t"}), el("dd", {text: "cycle theme"}),
        el("dt", {text: "Esc"}), el("dd", {text: "close this dialog"})
      ]),
      el("button", {type: "button", text: "close", onclick: function () { toggleHelp(false); }})
    ]));
    helpNode.querySelector("button").focus();
  }

  var pendingGo = false;

  function onKeyDown(event) {
    var tag = (event.target && event.target.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (event.key === "Escape") { toggleHelp(false); return; }
    if (typing) { return; }
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (pendingGo) {
      pendingGo = false;
      var destination = {o: "overview", p: "projects", s: "sessions", i: "signals",
        b: "tasks", e: "evals", x: "experiments", q: "quality"}[event.key];
      if (destination) { event.preventDefault(); C.go(destination, "", {}); }
      return;
    }
    if (event.key === "g") { pendingGo = true; setTimeout(function () { pendingGo = false; }, 1200); return; }
    if (event.key === "/") {
      event.preventDefault();
      var search = document.getElementById("session-search");
      if (search) { search.focus(); search.select(); }
      else { C.go("sessions", "", {}); setTimeout(function () {
        var next = document.getElementById("session-search");
        if (next) { next.focus(); }
      }, 60); }
      return;
    }
    if (event.key === "t") { cycleTheme(); return; }
    if (event.key === "?") { toggleHelp(true); }
  }

  function boot() {
    var stored = "auto";
    try { stored = global.localStorage.getItem("retro-theme") || "auto"; } catch (error) { stored = "auto"; }
    applyTheme(stored);
    document.getElementById("theme").addEventListener("click", cycleTheme);
    document.addEventListener("keydown", onKeyDown);
    global.addEventListener("hashchange", render);

    if (!C.manifest.schema) {
      viewNode.appendChild(el("div", {class: "empty"}, [
        el("p", {text: "data/manifest.js did not load."}),
        el("p", {text: "Rebuild with `retro dashboard build`."})
      ]));
      return;
    }

    footerNode.textContent = "built " + (C.manifest.generated_at || "?") +
      " · " + C.fmt.int((C.manifest.counts || {}).sessions) + " sessions" +
      " · signals " + ((C.manifest.signals || {}).version || "none") +
      " · archive " + ((C.manifest.archive || {}).root || "?");
    renderBanner();
    renderLoadState();
    render();

    C.onChange(function () {
      renderLoadState();
      if (C.store.excludedReady) { renderBanner(); }
      if (context && context.onStoreChange) { context.onStoreChange(); }
      else if (context && (context.route === "overview" || context.route === "projects" ||
        context.route === "project" || context.route === "signal")) {
        if (C.store.ready) { render(); }
      }
    });
    C.loadSessions();
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", boot); }
  else { boot(); }
})(window);
