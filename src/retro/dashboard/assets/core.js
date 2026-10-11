/* retro dashboard core: shard loading, fact-table store, aggregation, UI atoms. */
(function (global) {
  "use strict";

  var manifest = global.RETRO.data.manifest || {};
  var COLUMNS = manifest.columns || [];
  var COL = {};
  for (var i = 0; i < COLUMNS.length; i++) { COL[COLUMNS[i]] = i; }
  var FLAGS = manifest.flags || {};
  var DICT = manifest.dict || {agents: [], clients: [], hosts: [], projects: [], models: [], tags: [], outcomes: [], work: []};
  var COST_SCALE = 1000000;
  var RATIO_SCALE = 1000;
  var ROW_HEIGHT = 44;

  /* ---------- shard loading ---------------------------------------------- */

  var listeners = [];
  var pendingExcluded = [];

  function emit(event) {
    for (var i = 0; i < listeners.length; i++) { listeners[i](event); }
  }

  function onChange(callback) { listeners.push(callback); }

  function loadShard(name, src, callback) {
    var data = global.RETRO.data;
    if (data[name]) { callback(data[name]); return; }
    if (global.RETRO.failed[src]) { callback(null); return; }
    var waiters = global.RETRO.waiters;
    (waiters[name] = waiters[name] || []).push(callback);
    if (global.RETRO.loading[src]) { return; }
    global.RETRO.loading[src] = true;
    var script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onerror = function () {
      global.RETRO.failed[src] = true;
      var queue = waiters[name] || [];
      delete waiters[name];
      for (var i = 0; i < queue.length; i++) { queue[i](null); }
    };
    document.head.appendChild(script);
  }

  var store = {
    rows: [],
    byKey: Object.create(null),
    childCount: Object.create(null),
    shardsLoaded: 0,
    shardsTotal: (manifest.shards && manifest.shards.sessions ? manifest.shards.sessions.length : 0),
    included: (manifest.counts ? manifest.counts.included : 0),
    excludedTotal: (manifest.counts ? manifest.counts.excluded : 0),
    total: (manifest.counts ? manifest.counts.sessions : 0),
    ready: false,
    excludedReady: false,
    excludedLoading: false,
    signalsLoaded: false,
    signals: Object.create(null)
  };

  function ingestSessions(payload) {
    if (!payload || !payload.rows) { return; }
    var rows = payload.rows;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      store.byKey[row[COL.key]] = row;
      var parent = row[COL.parent];
      if (parent) { store.childCount[parent] = (store.childCount[parent] || 0) + 1; }
      store.rows.push(row);
    }
    store.shardsLoaded += 1;
  }

  function loadSessions(done) {
    var shards = (manifest.shards && manifest.shards.sessions) || [];
    if (!shards.length) { store.ready = true; emit({type: "sessions", done: true}); if (done) { done(); } return; }
    var index = 0;
    function next() {
      if (index >= shards.length) {
        store.ready = true;
        emit({type: "sessions", done: true});
        if (done) { done(); }
        return;
      }
      var shard = shards[index++];
      loadShard("sessions:" + shard.shard, shard.file, function (payload) {
        ingestSessions(payload);
        emit({type: "sessions", done: false});
        setTimeout(next, 0);
      });
    }
    next();
  }

  function loadExcluded(done) {
    if (store.excludedReady) { if (done) { done(); } return; }
    var shards = (manifest.shards && manifest.shards.excluded) || [];
    var tail = manifest.shards && manifest.shards.dict_tail;
    if (!shards.length) {
      store.excludedReady = true;
      emit({type: "excluded", done: true});
      if (done) { done(); }
      return;
    }
    if (store.excludedLoading) {
      if (done) { pendingExcluded.push(done); }
      return;
    }
    store.excludedLoading = true;
    if (done) { pendingExcluded.push(done); }

    function finish() {
      store.excludedReady = true;
      store.excludedLoading = false;
      emit({type: "excluded", done: true});
      var queue = pendingExcluded.slice();
      pendingExcluded.length = 0;
      for (var i = 0; i < queue.length; i++) { queue[i](); }
    }

    function loadRows() {
      var index = 0;
      function next() {
        if (index >= shards.length) { finish(); return; }
        var shard = shards[index++];
        loadShard("excluded:" + shard.shard, shard.file, function (payload) {
          ingestSessions(payload);
          emit({type: "excluded", done: false});
          setTimeout(next, 0);
        });
      }
      next();
    }

    if (tail && tail.file) {
      loadShard("dict-tail", tail.file, function (payload) {
        if (payload) { mergeDictionaryTail(payload); }
        loadRows();
      });
    } else {
      loadRows();
    }
  }

  function mergeDictionaryTail(tail) {
    for (var name in tail) {
      if (!Object.prototype.hasOwnProperty.call(tail, name)) { continue; }
      var entry = tail[name];
      var target = DICT[name];
      if (!target || !entry || !entry.items) { continue; }
      for (var i = 0; i < entry.items.length; i++) {
        var item = entry.items[i];
        if (item && typeof item === "object" && item.id && !item.name) { item.name = item.id; }
        target[entry.offset + i] = item;
      }
    }
  }

  function loadSignalMatrix(done) {
    if (store.signalsLoaded) { done(store.signals); return; }
    var shards = (manifest.shards && manifest.shards.signals) || [];
    if (!shards.length) { store.signalsLoaded = true; done(store.signals); return; }
    var index = 0;
    function next() {
      if (index >= shards.length) {
        store.signalsLoaded = true;
        emit({type: "signals", done: true});
        done(store.signals);
        return;
      }
      var shard = shards[index++];
      loadShard("signals:" + shard.shard, shard.file, function (payload) {
        if (payload && payload.signals) {
          var offset = payload.offset || 0;
          for (var signalId in payload.signals) {
            if (!Object.prototype.hasOwnProperty.call(payload.signals, signalId)) { continue; }
            var flat = payload.signals[signalId];
            var column = store.signals[signalId] || (store.signals[signalId] = Object.create(null));
            for (var i = 0; i < flat.length; i += 2) { column[offset + flat[i]] = flat[i + 1]; }
          }
        }
        emit({type: "signals", done: false});
        setTimeout(next, 0);
      });
    }
    next();
  }

  function loadDetail(key, done) {
    var row = store.byKey[key];
    if (!row) { done(null); return; }
    var bucket = row[COL.bucket] || 0;
    var padded = String(bucket);
    while (padded.length < 4) { padded = "0" + padded; }
    var excluded = flag(row, "excluded");
    var channel = (excluded ? "detailx:" : "detail:") + bucket;
    var file = (excluded ? "data/detail-x/x-" : "data/detail/d-") + padded + ".js";
    loadShard(channel, file, function (payload) {
      if (!payload || !payload.sessions) { done(null); return; }
      var detail = payload.sessions[key];
      done(detail ? (detail.minimal ? mergeRowDetail(key, row, detail) : detail) : null);
    });
  }

  function mergeRowDetail(key, row, detail) {
    var project = projectMeta(row);
    var parts = key.split("/");
    var host = parts[0];
    var sessionId = parts.slice(1).join("/");
    var merged = {
      key: key,
      host: host,
      session_id: sessionId,
      agent: agentName(row),
      client: clientName(row),
      interactive: flag(row, "interactive") ? true : (flag(row, "automated") ? false : null),
      is_subagent: flag(row, "subagent"),
      parent: row[COL.parent] || null,
      children: [],
      title: row[COL.title] || null,
      prompt: row[COL.prompt] || null,
      started_at: row[COL.started] ? new Date(row[COL.started] * 1000).toISOString() : null,
      ended_at: detail.ended_at || null,
      duration_s: row[COL.duration],
      active_s: row[COL.active],
      cwd: detail.cwd || (project.id.indexOf("path:") === 0 ? project.id.slice(5) : null),
      git: {},
      project: {
        id: project.id, name: project.name, source: project.source,
        evidence: detail.project_evidence || null
      },
      models: modelNames(row),
      counts: detail.counts || {},
      markers: detail.markers || [],
      tags: tagNames(row),
      tag_sources: detail.tag_sources || {},
      excluded: flag(row, "excluded"),
      minimal: true,
      paths: {
        transcript: "rendered/" + host + "/" + sessionId + ".md",
        normalized: "normalized/" + host + "/" + sessionId + ".events.jsonl",
        raw: "raw/" + host + "/" + sessionId
      }
    };
    return merged;
  }

  /* ---------- row helpers ------------------------------------------------- */

  function flag(row, name) { return (row[COL.flags] & (FLAGS[name] || 0)) !== 0; }
  function cost(row) {
    var raw = row[COL.cost];
    return (raw === null || raw === undefined) ? null : raw / COST_SCALE;
  }
  function costBasis(row) {
    var index = row[COL.basis];
    var names = manifest.cost_basis || [];
    if (index === null || index === undefined || index < 0 || index >= names.length) {
      return flag(row, "signals") ? "none" : "unscored";
    }
    return names[index];
  }
  function errorRate(row) { var raw = row[COL.errors]; return raw === null || raw === undefined ? null : raw / RATIO_SCALE; }
  function name(list, index) { return (index !== null && index !== undefined && index >= 0 && index < list.length) ? list[index] : "unknown"; }
  function agentName(row) { return name(DICT.agents, row[COL.agent]); }
  function clientName(row) { return name(DICT.clients, row[COL.client]); }
  function outcomeName(row) { return name(DICT.outcomes, row[COL.outcome]); }
  function workName(row) { return name(DICT.work, row[COL.work]); }
  function projectMeta(row) {
    var index = row[COL.project];
    return (index >= 0 && index < DICT.projects.length) ? DICT.projects[index] : {id: "unknown", name: "unknown"};
  }
  function tagNames(row) {
    var out = [];
    var tags = row[COL.tags] || [];
    for (var i = 0; i < tags.length; i++) { out.push(name(DICT.tags, tags[i])); }
    return out;
  }
  function modelNames(row) {
    var out = [];
    var models = row[COL.models] || [];
    for (var i = 0; i < models.length; i++) { out.push(name(DICT.models, models[i])); }
    return out;
  }

  /* ---------- filtering and aggregation ----------------------------------- */

  function indexSet(values, list) {
    if (!values || !values.length) { return null; }
    var lookup = Object.create(null);
    for (var i = 0; i < list.length; i++) { lookup[list[i]] = i; }
    var out = Object.create(null);
    var any = false;
    for (var j = 0; j < values.length; j++) {
      if (lookup[values[j]] !== undefined) { out[lookup[values[j]]] = true; any = true; }
    }
    return any ? out : {"-1": true};
  }

  function filterRows(options) {
    var opts = options || {};
    var rows = opts.rows || store.rows;
    var agents = indexSet(opts.agents, DICT.agents);
    var clients = indexSet(opts.clients, DICT.clients);
    var projectIds = [];
    for (var p = 0; p < DICT.projects.length; p++) { projectIds.push(DICT.projects[p].id); }
    var projects = indexSet(opts.projects, projectIds);
    var tags = indexSet(opts.tags, DICT.tags);
    var outcomes = indexSet(opts.outcomes, DICT.outcomes);
    var work = indexSet(opts.work, DICT.work);
    var models = indexSet(opts.models, DICT.models);
    var query = (opts.query || "").trim().toLowerCase();
    var terms = query ? query.split(/\s+/) : [];
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (!opts.includeExcluded && flag(row, "excluded")) { continue; }
      if (opts.rollupSubagents && row[COL.parent]) { continue; }
      var started = row[COL.started];
      if (opts.since !== undefined && opts.since !== null && (started === null || started < opts.since)) { continue; }
      if (opts.until !== undefined && opts.until !== null && (started === null || started > opts.until)) { continue; }
      if (agents && !agents[row[COL.agent]]) { continue; }
      if (clients && !clients[row[COL.client]]) { continue; }
      if (projects && !projects[row[COL.project]]) { continue; }
      if (outcomes && !outcomes[row[COL.outcome]]) { continue; }
      if (work && !work[row[COL.work]]) { continue; }
      if (tags && !anyIndex(row[COL.tags], tags)) { continue; }
      if (models && !anyIndex(row[COL.models], models)) { continue; }
      if (opts.interactive === true && !flag(row, "interactive")) { continue; }
      if (opts.interactive === false && !flag(row, "automated")) { continue; }
      if (terms.length) {
        var haystack = (row[COL.key] + " " + (row[COL.title] || "") + " " + (row[COL.prompt] || "")).toLowerCase();
        var matched = true;
        for (var t = 0; t < terms.length; t++) {
          if (haystack.indexOf(terms[t]) === -1) { matched = false; break; }
        }
        if (!matched) { continue; }
      }
      out.push(row);
    }
    return out;
  }

  function anyIndex(values, lookup) {
    if (!values) { return false; }
    for (var i = 0; i < values.length; i++) {
      if (lookup[values[i]]) { return true; }
    }
    return false;
  }

  function bump(map, key) { map[key] = (map[key] || 0) + 1; }

  function aggregate(rows) {
    var stats = {
      sessions: 0, activeS: 0, durationS: 0, cost: 0, tokens: 0, turns: 0, tools: 0, edits: 0,
      corrections: 0, withCost: 0, unpriced: 0, errorSum: 0, errorN: 0,
      basis: Object.create(null),
      outcomes: Object.create(null), work: Object.create(null), agents: Object.create(null),
      clients: Object.create(null), projects: Object.create(null), days: Object.create(null),
      first: null, last: null, excluded: 0, subagents: 0
    };
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      stats.sessions += 1;
      stats.activeS += row[COL.active] || 0;
      stats.durationS += row[COL.duration] || 0;
      if (row[COL.cost] !== null && row[COL.cost] !== undefined) {
        stats.cost += row[COL.cost] / COST_SCALE;
        stats.withCost += 1;
      } else {
        stats.unpriced += 1;
        bump(stats.basis, costBasis(row));
      }
      stats.tokens += row[COL.tokens] || 0;
      stats.turns += row[COL.turns] || 0;
      stats.tools += row[COL.tools] || 0;
      stats.edits += row[COL.edits] || 0;
      stats.corrections += row[COL.corrections] || 0;
      var rate = errorRate(row);
      if (rate !== null) { stats.errorSum += rate; stats.errorN += 1; }
      bump(stats.outcomes, outcomeName(row));
      bump(stats.work, workName(row));
      bump(stats.agents, agentName(row));
      bump(stats.clients, clientName(row));
      bump(stats.projects, projectMeta(row).id);
      if (flag(row, "excluded")) { stats.excluded += 1; }
      if (flag(row, "subagent")) { stats.subagents += 1; }
      var started = row[COL.started];
      if (started) {
        bump(stats.days, dayKey(started));
        if (stats.first === null || started < stats.first) { stats.first = started; }
        if (stats.last === null || started > stats.last) { stats.last = started; }
      }
    }
    stats.activeDays = Object.keys(stats.days).length;
    stats.projectCount = Object.keys(stats.projects).length;
    stats.successRate = successRate(stats.outcomes);
    stats.errorRate = stats.errorN ? stats.errorSum / stats.errorN : null;
    stats.costPerSession = stats.withCost ? stats.cost / stats.withCost : null;
    stats.costComplete = stats.unpriced === 0;
    return stats;
  }

  function successRate(outcomes) {
    var judged = 0;
    var good = 0;
    for (var key in outcomes) {
      if (!Object.prototype.hasOwnProperty.call(outcomes, key) || key === "unknown") { continue; }
      judged += outcomes[key];
      if (key === "success") { good += outcomes[key]; }
    }
    return judged ? good / judged : null;
  }

  function groupBy(rows, classify) {
    var groups = Object.create(null);
    for (var i = 0; i < rows.length; i++) {
      var key = classify(rows[i]);
      if (key === null || key === undefined) { continue; }
      (groups[key] = groups[key] || []).push(rows[i]);
    }
    return groups;
  }

  function facetCounts(rows, accessor) {
    var counts = Object.create(null);
    for (var i = 0; i < rows.length; i++) {
      var value = accessor(rows[i]);
      if (value === null || value === undefined || value === "") { continue; }
      if (Array.isArray(value)) {
        for (var j = 0; j < value.length; j++) { bump(counts, value[j]); }
      } else {
        bump(counts, value);
      }
    }
    var entries = [];
    for (var key in counts) {
      if (Object.prototype.hasOwnProperty.call(counts, key)) { entries.push({value: key, count: counts[key]}); }
    }
    entries.sort(function (a, b) { return b.count - a.count || (a.value < b.value ? -1 : 1); });
    return entries;
  }

  function dailySeries(rows, days, until) {
    var end = until || Math.floor(Date.now() / 1000);
    var buckets = Object.create(null);
    for (var i = 0; i < rows.length; i++) {
      var started = rows[i][COL.started];
      if (!started) { continue; }
      var key = dayKey(started);
      var bucket = buckets[key] || (buckets[key] = {sessions: 0, cost: 0, activeS: 0, edits: 0, tools: 0});
      bucket.sessions += 1;
      bucket.cost += cost(rows[i]);
      bucket.activeS += rows[i][COL.active] || 0;
      bucket.edits += rows[i][COL.edits] || 0;
      bucket.tools += rows[i][COL.tools] || 0;
    }
    var series = [];
    var cursor = new Date(end * 1000);
    cursor.setHours(0, 0, 0, 0);
    var span = days || 30;
    for (var d = span - 1; d >= 0; d--) {
      var date = new Date(cursor.getTime() - d * 86400000);
      var label = localDayKey(date);
      var entry = buckets[label] || {sessions: 0, cost: 0, activeS: 0, edits: 0, tools: 0};
      series.push({day: label, date: date, sessions: entry.sessions, cost: entry.cost, activeS: entry.activeS, edits: entry.edits, tools: entry.tools});
    }
    return series;
  }

  function streak(rows) {
    var days = Object.create(null);
    for (var i = 0; i < rows.length; i++) {
      var started = rows[i][COL.started];
      if (started) { days[dayKey(started)] = true; }
    }
    var count = 0;
    var cursor = new Date();
    cursor.setHours(0, 0, 0, 0);
    if (!days[localDayKey(cursor)]) { cursor = new Date(cursor.getTime() - 86400000); }
    while (days[localDayKey(cursor)]) {
      count += 1;
      cursor = new Date(cursor.getTime() - 86400000);
    }
    return count;
  }

  function dayKey(epochSeconds) { return localDayKey(new Date(epochSeconds * 1000)); }

  function localDayKey(date) {
    var month = date.getMonth() + 1;
    var day = date.getDate();
    return date.getFullYear() + "-" + (month < 10 ? "0" : "") + month + "-" + (day < 10 ? "0" : "") + day;
  }

  function rangeStart(rangeKey) {
    var days = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}[rangeKey];
    if (!days) { return null; }
    return Math.floor(Date.now() / 1000) - days * 86400;
  }

  function rangeDays(rangeKey) { return {"7d": 7, "30d": 30, "90d": 90, "365d": 365}[rangeKey] || null; }

  /* ---------- formatting --------------------------------------------------- */

  var fmt = {
    int: function (value) {
      if (value === null || value === undefined || isNaN(value)) { return "—"; }
      return Math.round(value).toLocaleString();
    },
    compact: function (value) {
      if (value === null || value === undefined || isNaN(value)) { return "—"; }
      var abs = Math.abs(value);
      if (abs >= 1e9) { return (value / 1e9).toFixed(1) + "B"; }
      if (abs >= 1e6) { return (value / 1e6).toFixed(1) + "M"; }
      if (abs >= 1e3) { return (value / 1e3).toFixed(1) + "k"; }
      return String(Math.round(value));
    },
    money: function (value) {
      if (value === null || value === undefined || isNaN(value)) { return "—"; }
      if (value !== 0 && Math.abs(value) < 0.01) { return "<$0.01"; }
      return "$" + value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    },
    duration: function (seconds) {
      if (seconds === null || seconds === undefined) { return "—"; }
      if (!seconds || seconds <= 0) { return "–"; }
      var total = Math.round(seconds);
      var hours = Math.floor(total / 3600);
      var minutes = Math.floor((total % 3600) / 60);
      if (hours >= 24) { return Math.floor(hours / 24) + "d " + (hours % 24) + "h"; }
      if (hours) { return hours + "h " + minutes + "m"; }
      if (minutes) { return minutes + "m " + (total % 60) + "s"; }
      return total + "s";
    },
    pct: function (fraction, digits) {
      if (fraction === null || fraction === undefined || isNaN(fraction)) { return "—"; }
      return (fraction * 100).toFixed(digits === undefined ? 0 : digits) + "%";
    },
    date: function (epochSeconds) {
      if (!epochSeconds) { return "–"; }
      return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {year: "numeric", month: "short", day: "2-digit"});
    },
    dateTime: function (epochSeconds) {
      if (!epochSeconds) { return "–"; }
      return new Date(epochSeconds * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
    },
    ago: function (epochSeconds) {
      if (!epochSeconds) { return "never"; }
      var delta = Date.now() / 1000 - epochSeconds;
      if (delta < 3600) { return Math.max(1, Math.round(delta / 60)) + "m ago"; }
      if (delta < 86400) { return Math.round(delta / 3600) + "h ago"; }
      if (delta < 86400 * 30) { return Math.round(delta / 86400) + "d ago"; }
      return fmt.date(epochSeconds);
    },
    signal: function (value, definition) {
      if (value === null || value === undefined) { return "—"; }
      if (typeof value === "boolean") { return value ? "yes" : "no"; }
      var kind = definition ? definition.kind : null;
      if (kind === "money") { return fmt.money(value); }
      if (kind === "duration") { return fmt.duration(value); }
      if (kind === "ratio") { return fmt.pct(value, 1); }
      if (typeof value === "number") {
        return Math.abs(value) >= 1000 ? fmt.compact(value) : (Math.round(value * 1000) / 1000).toLocaleString();
      }
      return String(value);
    }
  };

  /* ---------- DOM atoms ---------------------------------------------------- */

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      for (var key in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, key) || attrs[key] === null || attrs[key] === undefined) { continue; }
        if (key === "class") { node.className = attrs[key]; }
        else if (key === "text") { node.textContent = attrs[key]; }
        else if (key.indexOf("on") === 0 && typeof attrs[key] === "function") { node.addEventListener(key.slice(2), attrs[key]); }
        else { node.setAttribute(key, attrs[key]); }
      }
    }
    appendAll(node, children);
    return node;
  }

  function appendAll(node, children) {
    if (children === null || children === undefined) { return; }
    if (!Array.isArray(children)) { children = [children]; }
    for (var i = 0; i < children.length; i++) {
      var child = children[i];
      if (child === null || child === undefined || child === false) { continue; }
      node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
    }
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  function panel(title, children, extra) {
    return el("section", {class: "panel" + (extra ? " " + extra : "")}, [title ? el("h2", {text: title}) : null].concat(children || []));
  }

  var dismissedNotices = null;
  var noticeStorageAvailable = true;
  var NOTICE_STORAGE_KEY = "retro-dismissed-warnings-v1";

  function noticeState() {
    if (dismissedNotices !== null) { return dismissedNotices; }
    dismissedNotices = [];
    try {
      if (!global.localStorage) { throw new Error("Browser storage is unavailable"); }
      var saved = global.localStorage.getItem(NOTICE_STORAGE_KEY);
      var values = saved ? JSON.parse(saved) : [];
      if (!Array.isArray(values) || values.some(function (value) { return typeof value !== "string"; })) {
        throw new Error("Saved warning dismissals are invalid");
      }
      dismissedNotices = values.slice(-64);
    } catch (error) {
      noticeStorageAvailable = false;
      if (global.console) { global.console.warn("Warning dismissals will last for this tab only:", error); }
    }
    return dismissedNotices;
  }

  function dismissibleNotice(content, identity, label) {
    var key = JSON.stringify([(manifest.archive || {}).root || "", identity]);
    var values = noticeState();
    var status = el("span", {class: "muted notice-storage-status", role: "status"});
    var reopen = el("button", {type: "button", class: "ghost notice-reopen",
      text: "Show dismissed warning", "aria-label": "Show dismissed " + label});
    var dismiss = el("button", {type: "button", class: "ghost notice-dismiss",
      text: "Dismiss", "aria-label": "Dismiss " + label});
    var wrapper = el("div", {class: "dismissible-notice"}, [content, reopen, status]);
    function update(hidden) {
      content.hidden = hidden;
      reopen.hidden = !hidden;
    }
    function save() {
      if (noticeStorageAvailable) {
        try { global.localStorage.setItem(NOTICE_STORAGE_KEY, JSON.stringify(dismissedNotices)); }
        catch (error) {
          noticeStorageAvailable = false;
          if (global.console) { global.console.warn("Could not save warning dismissal:", error); }
        }
      }
      status.textContent = noticeStorageAvailable ? "" : "Dismissal lasts for this tab only; browser storage is unavailable.";
    }
    dismiss.addEventListener("click", function () {
      if (dismissedNotices.indexOf(key) === -1) { dismissedNotices.push(key); }
      dismissedNotices = dismissedNotices.slice(-64);
      save(); update(true); reopen.focus();
    });
    reopen.addEventListener("click", function () {
      dismissedNotices = dismissedNotices.filter(function (value) { return value !== key; });
      save(); update(false); dismiss.focus();
    });
    content.appendChild(dismiss);
    update(values.indexOf(key) !== -1);
    return wrapper;
  }

  function kpi(label, value, deltaFraction, hint) {
    var children = [el("div", {class: "label", text: label}), el("div", {class: "value", text: value})];
    if (deltaFraction !== null && deltaFraction !== undefined && !isNaN(deltaFraction)) {
      var direction = deltaFraction > 0.001 ? "up" : (deltaFraction < -0.001 ? "down" : "flat");
      var arrow = direction === "up" ? "▲" : (direction === "down" ? "▼" : "=");
      children.push(el("div", {class: "delta " + direction, text: arrow + " " + fmt.pct(Math.abs(deltaFraction)) + " vs prev"}));
    } else if (hint) {
      children.push(el("div", {class: "delta flat", text: hint}));
    }
    return el("div", {class: "panel kpi"}, children);
  }

  function pill(outcome) { return el("span", {class: "pill " + outcome, text: outcome}); }

  var COST_BASIS_REASON = {
    exact: "priced with an exact model rate",
    family: "priced with a model-family rate",
    mixed: "priced from several model rates",
    embedded: "cost reported by the provider",
    unpriced: "no rate for the models used",
    none: "no token or cost usage was captured",
    unscored: "this session has no signal record"
  };

  function costTotal(stats) {
    if (!stats.withCost) { return "—"; }
    return fmt.money(stats.cost);
  }

  function costTotalTitle(stats) {
    if (!stats.withCost) {
      return "No session in this group has a cost estimate" +
        (stats.sessions ? " (" + stats.sessions + " sessions)" : "");
    }
    if (stats.unpriced) {
      return stats.unpriced + " of " + stats.sessions + " sessions have no cost estimate";
    }
    return null;
  }

  function costCell(row) {
    var value = cost(row);
    if (value !== null) { return el("span", {text: fmt.money(value)}); }
    var basis = costBasis(row);
    return el("span", {
      class: "muted",
      text: "—",
      title: "No cost estimate: " + (COST_BASIS_REASON[basis] || basis)
    });
  }

  function tableOf(headers, rows, options) {
    var opts = options || {};
    var labels = headers.map(function (header) {
      return typeof header === "string" ? header : header.label;
    });
    var head = el("tr", null, headers.map(function (header, index) {
      var attrs = {text: labels[index]};
      if (typeof header !== "string" && header.num) { attrs.class = "num"; }
      if (opts.onSort) {
        attrs.class = (attrs.class || "") + " sortable";
        attrs.tabindex = "0";
        attrs.role = "columnheader";
        attrs["aria-sort"] = opts.sortIndex === index ? (opts.sortDescending ? "descending" : "ascending") : "none";
        attrs.onclick = function () { opts.onSort(index); };
        attrs.onkeydown = function (event) {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); opts.onSort(index); }
        };
      }
      return el("th", attrs);
    }));
    var body = el("tbody", null, rows.map(function (cells) {
      var attrs = cells.attrs || null;
      var list = cells.cells || cells;
      return el("tr", attrs, list.map(function (cell, index) {
        // data-label drives the stacked card layout on narrow screens.
        var cellAttrs = {"data-label": labels[index] || ""};
        if (cell && cell.nodeType) { return el("td", cellAttrs, cell); }
        if (cell && typeof cell === "object") {
          if (cell.num) { cellAttrs.class = "num"; }
          if (cell.cls) { cellAttrs.class = (cellAttrs.class ? cellAttrs.class + " " : "") + cell.cls; }
          if (cell.title) { cellAttrs.title = cell.title; }
          return el("td", cellAttrs, cell.node !== undefined ? cell.node : String(cell.text));
        }
        cellAttrs.text = (cell === null || cell === undefined) ? "—" : String(cell);
        return el("td", cellAttrs);
      }));
    }));
    var table = el("table", {class: opts.stack === false ? null : "stack"}, [el("thead", null, head), body]);
    return opts.wrap === false ? table : el("div", {class: "table-wrap"}, [table]);
  }

  /* ---------- charts ------------------------------------------------------- */

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key) && attrs[key] !== null && attrs[key] !== undefined) {
        node.setAttribute(key, String(attrs[key]));
      }
    }
    return node;
  }

  function barChart(series, options) {
    var opts = options || {};
    var width = 720;
    var height = opts.height || 140;
    var padBottom = 18;
    var max = 0;
    for (var i = 0; i < series.length; i++) { max = Math.max(max, series[i].value || 0); }
    var svg = svgEl("svg", {class: "chart", viewBox: "0 0 " + width + " " + height, role: "img",
      "aria-label": opts.label || "bar chart", preserveAspectRatio: "none"});
    var count = Math.max(1, series.length);
    var slot = width / count;
    var barWidth = Math.max(1, slot - Math.min(3, slot * 0.25));
    for (var j = 0; j < series.length; j++) {
      var value = series[j].value || 0;
      var barHeight = max > 0 ? (height - padBottom) * (value / max) : 0;
      var rect = svgEl("rect", {
        x: (j * slot + (slot - barWidth) / 2).toFixed(2),
        y: (height - padBottom - barHeight).toFixed(2),
        width: barWidth.toFixed(2),
        height: Math.max(0, barHeight).toFixed(2),
        class: "barfill",
        rx: Math.min(2, barWidth / 2)
      });
      rect.appendChild(svgEl("title", {}));
      rect.lastChild.textContent = (series[j].label || "") + ": " + (opts.format ? opts.format(value) : value);
      svg.appendChild(rect);
    }
    svg.appendChild(svgEl("line", {x1: 0, y1: height - padBottom, x2: width, y2: height - padBottom, class: "grid-line"}));
    if (series.length) {
      var firstLabel = svgEl("text", {x: 0, y: height - 4, class: "axis"});
      firstLabel.textContent = series[0].label || "";
      var lastLabel = svgEl("text", {x: width, y: height - 4, class: "axis", "text-anchor": "end"});
      lastLabel.textContent = series[series.length - 1].label || "";
      svg.appendChild(firstLabel);
      svg.appendChild(lastLabel);
    }
    var maxLabel = svgEl("text", {x: 0, y: 10, class: "axis"});
    maxLabel.textContent = "max " + (opts.format ? opts.format(max) : max);
    svg.appendChild(maxLabel);
    return svg;
  }

  var MIX_COLORS = {
    success: "var(--good)", partial: "var(--warn)", failed: "var(--bad)",
    abandoned: "var(--neutral)", unknown: "var(--line)"
  };
  var PALETTE = ["#2f6feb", "#51cf86", "#e3b341", "#ff7b72", "#a371f7", "#39c5cf", "#f78166", "#8b949e", "#6ea0ff", "#d2a8ff"];

  function colorFor(label, index) {
    if (MIX_COLORS[label]) { return MIX_COLORS[label]; }
    return PALETTE[index % PALETTE.length];
  }

  function mixBar(counts, options) {
    var opts = options || {};
    var entries = [];
    var total = 0;
    for (var key in counts) {
      if (Object.prototype.hasOwnProperty.call(counts, key) && counts[key]) {
        entries.push({label: key, value: counts[key]});
        total += counts[key];
      }
    }
    entries.sort(function (a, b) { return b.value - a.value; });
    if (opts.order) {
      entries.sort(function (a, b) {
        var ai = opts.order.indexOf(a.label), bi = opts.order.indexOf(b.label);
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
      });
    }
    var bar = el("div", {class: "bar", role: "img", "aria-label": opts.label || "distribution"});
    var legend = el("div", {class: "legend"});
    entries.forEach(function (entry, index) {
      var color = colorFor(entry.label, index);
      var segment = el("span", {style: "width:" + (total ? (entry.value / total) * 100 : 0) + "%;background:" + color});
      segment.title = entry.label + ": " + entry.value + " (" + fmt.pct(total ? entry.value / total : 0) + ")";
      bar.appendChild(segment);
      legend.appendChild(el("span", null, [
        el("i", {style: "background:" + color}),
        entry.label + " " + fmt.int(entry.value)
      ]));
    });
    if (!entries.length) { return el("div", {class: "muted", text: "no data"}); }
    return el("div", null, [bar, legend]);
  }

  function histogram(values, options) {
    var opts = options || {};
    if (!values.length) { return el("div", {class: "muted", text: "no data"}); }
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var min = sorted[0];
    var max = sorted[sorted.length - 1];
    var bins = opts.bins || 24;
    if (min === max) {
      return el("div", {class: "muted", text: "all values = " + (opts.format ? opts.format(min) : min)});
    }
    var width = (max - min) / bins;
    var counts = [];
    for (var b = 0; b < bins; b++) { counts.push(0); }
    for (var i = 0; i < sorted.length; i++) {
      var index = Math.min(bins - 1, Math.floor((sorted[i] - min) / width));
      counts[index] += 1;
    }
    var series = counts.map(function (count, index) {
      return {label: (opts.format ? opts.format(min + index * width) : (min + index * width).toFixed(2)), value: count};
    });
    return barChart(series, {height: 120, label: opts.label, format: function (v) { return v + " sessions"; }});
  }

  function percentile(sortedValues, fraction) {
    if (!sortedValues.length) { return null; }
    if (sortedValues.length === 1) { return sortedValues[0]; }
    var position = fraction * (sortedValues.length - 1);
    var lower = Math.floor(position);
    var upper = Math.ceil(position);
    if (lower === upper) { return sortedValues[lower]; }
    return sortedValues[lower] * (upper - position) + sortedValues[upper] * (position - lower);
  }

  /* ---------- virtualized list -------------------------------------------- */

  function virtualList(viewport, rows, renderRow, options) {
    var opts = options || {};
    clear(viewport);
    var spacer = el("div", {class: "spacer"});
    spacer.style.height = (rows.length * ROW_HEIGHT) + "px";
    viewport.appendChild(spacer);
    var overscan = 6;
    var lastStart = -1;
    var lastEnd = -1;

    function render() {
      var scrollTop = viewport.scrollTop;
      var height = viewport.clientHeight || 400;
      var start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - overscan);
      var end = Math.min(rows.length, Math.ceil((scrollTop + height) / ROW_HEIGHT) + overscan);
      if (start === lastStart && end === lastEnd) { return; }
      lastStart = start;
      lastEnd = end;
      clear(spacer);
      var fragment = document.createDocumentFragment();
      for (var i = start; i < end; i++) {
        var node = renderRow(rows[i], i);
        node.style.top = (i * ROW_HEIGHT) + "px";
        fragment.appendChild(node);
      }
      spacer.appendChild(fragment);
      if (opts.onRender) { opts.onRender(start, end); }
    }

    viewport.onscroll = render;
    render();
    return {render: render, refresh: function (next, keepScroll) {
      rows = next;
      spacer.style.height = (rows.length * ROW_HEIGHT) + "px";
      lastStart = -1;
      var maximum = Math.max(0, rows.length * ROW_HEIGHT - (viewport.clientHeight || 0));
      viewport.scrollTop = keepScroll ? Math.min(viewport.scrollTop, maximum) : 0;
      render();
    }};
  }

  /* ---------- hash routing ------------------------------------------------- */

  function parseHash() {
    var hash = global.location.hash.replace(/^#\/?/, "");
    var parts = hash.split("?");
    var path = parts[0] || "overview";
    var params = Object.create(null);
    if (parts[1]) {
      var pairs = parts[1].split("&");
      for (var i = 0; i < pairs.length; i++) {
        if (!pairs[i]) { continue; }
        var pair = pairs[i].split("=");
        params[decodeURIComponent(pair[0])] = decodeURIComponent((pair[1] || "").replace(/\+/g, " "));
      }
    }
    var segments = path.split("/");
    return {route: segments[0] || "overview", id: segments.slice(1).join("/"), params: params};
  }

  function buildHash(route, id, params) {
    var out = "#/" + route + (id ? "/" + id : "");
    var pairs = [];
    for (var key in params) {
      if (Object.prototype.hasOwnProperty.call(params, key) && params[key] !== "" && params[key] !== null && params[key] !== undefined) {
        pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
      }
    }
    return pairs.length ? out + "?" + pairs.join("&") : out;
  }

  function go(route, id, params) { global.location.hash = buildHash(route, id, params); }

  global.RETRO.core = {
    manifest: manifest, COL: COL, FLAGS: FLAGS, DICT: DICT, ROW_HEIGHT: ROW_HEIGHT,
    store: store, onChange: onChange, emit: emit,
    loadSessions: loadSessions, loadSignalMatrix: loadSignalMatrix, loadDetail: loadDetail, loadShard: loadShard,
    flag: flag, cost: cost, costBasis: costBasis, costCell: costCell, errorRate: errorRate, name: name,
    costTotal: costTotal, costTotalTitle: costTotalTitle,
    COST_BASIS_REASON: COST_BASIS_REASON, facetCounts: facetCounts,
    loadExcluded: loadExcluded,
    agentName: agentName, clientName: clientName, outcomeName: outcomeName, workName: workName,
    projectMeta: projectMeta, tagNames: tagNames, modelNames: modelNames,
    filterRows: filterRows, aggregate: aggregate, groupBy: groupBy, successRate: successRate,
    dailySeries: dailySeries, dayKey: dayKey, localDayKey: localDayKey, streak: streak,
    rangeStart: rangeStart, rangeDays: rangeDays, percentile: percentile,
    fmt: fmt, el: el, clear: clear, panel: panel, kpi: kpi, pill: pill, tableOf: tableOf,
    dismissibleNotice: dismissibleNotice,
    barChart: barChart, mixBar: mixBar, histogram: histogram, colorFor: colorFor,
    virtualList: virtualList, parseHash: parseHash, buildHash: buildHash, go: go
  };
})(window);
