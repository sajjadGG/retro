/* Published benchmark metadata, integrated with the existing file:// dashboard. */
(function (global) {
  "use strict";

  var C = global.RETRO.core;
  var el = C.el;
  var fmt = C.fmt;
  var views = global.RETRO.views;
  var REQUIRED = ["base", "oracle", "no_op", "construct_changing", "construct_preserving", "regression"];
  var PAGE_SIZE = 50;
  var SCORE_NOTE = "Scores and pass rates use scored attempts only. Coverage is scored / requested attempts. " +
    "Infrastructure, agent and scorer failures are unavailable rewards, NOT zero reward. " +
    "Benchmark scores are source-normalized published aggregates; resources are reported separately. " +
    "Training sampling priority NEVER changes benchmark score.";

  function list(value) { return Array.isArray(value) ? value : []; }
  function numeric(value) { return typeof value === "number" && isFinite(value); }
  function count(value) { return numeric(value) ? fmt.int(value) : "—"; }
  function reward(value) { return numeric(value) ? value.toFixed(3) : "—"; }
  function exact(value) { return numeric(value) ? String(value) : "—"; }
  function rate(value) { return numeric(value) ? fmt.pct(value, 1) : "—"; }
  function decision(value) { return value === true ? "yes" : (value === false ? "no" : "unavailable"); }
  function maps(value) {
    return Object.keys(value || {}).map(function (key) { return key + ": " + count(value[key]); }).join(" · ") || "—";
  }
  function badge(value) {
    var tones = {passed: "success", accept: "success", verified: "success", curated: "success",
      rejected: "failed", failed: "failed", reject: "failed", incompatible: "failed", incomplete: "partial",
      revise: "partial", exact_replay: "success", anchored_reconstruction: "partial", adjacent_synthesis: "partial"};
    var tone = Object.prototype.hasOwnProperty.call(tones, value) ? tones[value] : "unknown";
    var labels = {exact_replay: "Exact replay", anchored_reconstruction: "Reconstructed task",
      adjacent_synthesis: "Generated variant"};
    return el("span", {class: "pill " + tone, text: labels[value] || value || "unavailable", title: value});
  }
  function section(title, children) { return C.panel(title, children, "benchmark-section"); }
  function kv(entries) {
    var children = [];
    entries.forEach(function (entry) {
      children.push(el("dt", {text: entry[0]}));
      children.push(el("dd", {class: "benchmark-value"}, [entry[1] === null || entry[1] === undefined ? "—" : entry[1]]));
    });
    return el("dl", {class: "kv"}, children);
  }
  function hash(value) { return el("span", {class: "mono benchmark-hash", text: value || "—"}); }
  function explainWarning(message) {
    if (/legacy\/unbound|cohort\.json: published artifact missing/.test(message)) {
      return {key: "unbound", title: "Some older runs have no recorded benchmark version",
        impact: "We cannot confirm which exact tasks and grader those runs used. Their scores remain visible for inspection, but comparisons are disabled.",
        action: "Use a verified evaluation, or rerun those agents against a frozen benchmark version."};
    }
    if (/different scorer package|scorer_package_sha256 differs/.test(message)) {
      return {key: "grader", title: "An older release has conflicting grader records",
        impact: "Its task and validation records name different grader versions. We cannot certify that release or compare its scores.",
        action: "Use the verified canonical release instead. The older files are kept as history, not silently repaired."};
    }
    if (/cohort does not match|comparison unavailable/.test(message)) {
      return {key: "cohort", title: "A saved run does not match a verified benchmark version",
        impact: "The task version, grader, requested trials or result evidence does not match. Comparison is disabled for this run only; this is not an agent failure.",
        action: "Open a verified evaluation for the current release. Review the technical evidence before trusting or rerunning this record."};
    }
    if (/Source-grouped splits share repository history/.test(message)) {
      return {key: "split", title: "Training and evaluation share repository history",
        impact: "This split tests new tasks in a familiar repository, not transfer to an unseen repository.",
        action: "Use repository-grouped splits when you want to measure new-repository generalization."};
    }
    return {key: "other", title: "Some saved benchmark details could not be verified",
      impact: "Affected details may be unavailable, and a run may not be eligible for comparison. This does not turn an unavailable result into a failed task.",
      action: "Review the technical evidence below; rebuild missing data or validate the affected release before using it."};
  }
  function warnings(messages, open) {
    if (!list(messages).length) { return null; }
    var groups = {};
    messages.forEach(function (message) {
      var info = explainWarning(message);
      groups[info.key] = info;
    });
    var content = el("section", {class: "benchmark-warnings", role: open ? "alert" : "status",
      "aria-label": "Benchmark verification notices"}, [
      el("h2", {text: "What this warning means"}),
      el("p", {class: "muted", text: "These notices apply to the affected saved records, not automatically to every task or to the latest verified results."}),
      el("div", null, Object.keys(groups).map(function (key) {
        var issue = groups[key];
        return el("div", {class: "benchmark-notice"}, [
          el("h3", {text: issue.title}), el("p", {text: issue.impact}),
          el("p", {class: "muted", text: "Next step: " + issue.action})
        ]);
      })),
      el("details", null, [
        el("summary", {text: "Technical evidence (" + messages.length + " records)"}),
        el("ul", null, messages.map(function (message) { return el("li", {text: message}); }))
      ])
    ]);
    return C.dismissibleNotice(content, ["verification", messages.slice().sort()], "verification warning");
  }
  function empty(host, message) { host.appendChild(el("p", {class: "empty", text: message})); }
  function load(ref, host, done) {
    if (!ref || !ref.file || !ref.name) {
      C.clear(host);
      empty(host, "Benchmark data is not declared in this dashboard generation.");
      return;
    }
    C.loadShard(ref.name, ref.file, function (payload) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        C.clear(host);
        host.appendChild(el("p", {class: "benchmark-warning", role: "alert",
          text: "Benchmark shard could not be loaded: " + ref.file + ". Rebuild this dashboard from the archive."}));
        return;
      }
      done(payload);
    });
  }
  function benchmarkHref(name, suite, extra) {
    var params = {name: name, suite: suite || ""};
    Object.keys(extra || {}).forEach(function (key) { params[key] = extra[key]; });
    return C.buildHash("tasks", "", params);
  }
  function taskHref(name, id, suite) {
    return C.buildHash("task", name + "/" + id, {suite: suite || ""});
  }
  function sourceLink(source, eventId) {
    source = source || {};
    var label = eventId || source.source_id || "unavailable";
    if (source.captured && source.session_key) {
      return el("a", {class: "mono", text: label, href: C.buildHash("session", source.session_key,
        {event: eventId || "", range: "all", inc: "1"}), title: "Open captured source session" +
          (eventId ? " and locate event " + eventId + " in its transcript" : "")});
    }
    return el("span", {class: "mono", text: label, title: "Source session is not in this dashboard archive"});
  }
  function eventLinks(ids, source) {
    if (!list(ids).length) { return "—"; }
    return el("span", {class: "benchmark-events"}, ids.map(function (id) { return sourceLink(source, id); }));
  }
  function select(label, values, current, change, labels) {
    return el("label", null, [label, el("select", {"aria-label": label, onchange: function (event) {
      change(event.target.value);
    }}, values.map(function (value) {
      return el("option", {value: value, selected: value === current ? "" : null,
        text: labels && labels[value] ? labels[value] : value || "all"});
    }))]);
  }
  function unique(values) {
    return values.filter(function (value, index) { return value && values.indexOf(value) === index; }).sort();
  }
  function paged(host, headers, records, renderRow, label, options) {
    var page = 0;
    function refresh() {
      C.clear(host);
      var start = page * PAGE_SIZE;
      var rows = records.slice(start, start + PAGE_SIZE).map(renderRow);
      host.appendChild(headers ? C.tableOf(headers, rows, options) :
        el("div", {class: "grid benchmark-task-cards"}, rows));
      if (records.length > PAGE_SIZE) {
        host.appendChild(el("div", {class: "benchmark-pagination", role: "group", "aria-label": label + " pages"}, [
          el("button", {type: "button", text: "previous", disabled: page === 0 ? "" : null,
            onclick: function () { page -= 1; refresh(); }}),
          el("span", {role: "status", "aria-live": "polite",
            text: (start + 1) + "–" + Math.min(start + PAGE_SIZE, records.length) + " of " + records.length}),
          el("button", {type: "button", text: "next", disabled: start + PAGE_SIZE >= records.length ? "" : null,
            onclick: function () { page += 1; refresh(); }})
        ]));
      }
    }
    refresh();
  }

  views.tasks = function (container, ctx) {
    container.appendChild(el("h1", {text: "Tasks"}));
    container.appendChild(el("p", {class: "sub", text: "Explore the tasks, the repository environments an agent starts in, " +
      "and how each hidden grader decides success. This view does not execute agents or graders."}));
    var host = el("div", {text: "loading benchmark index…"});
    container.appendChild(host);
    var manifest = C.manifest.benchmarks;
    if (!manifest) { C.clear(host); empty(host, "No benchmarks in this archive."); return; }
    load(manifest.index, host, function (index) {
      C.clear(host);
      var sets = list(index.tasksets);
      if (!sets.length) {
        empty(host, "No benchmarks in this archive. Publish a taskset or curated suite and rebuild the dashboard.");
        host.appendChild(warnings(index.warnings, true) || el("span"));
        return;
      }
      if (!(ctx.params || {}).name && index.catalog) {
        var catalogHost = el("div", {text: "loading tasks and environments…"});
        host.appendChild(catalogHost);
        load(index.catalog, catalogHost, function (catalog) {
          C.clear(catalogHost);
          catalogHost.appendChild(el("p", {class: "muted", text: "Showing the current verified release from each collection, " +
            "not duplicate historical task copies. An environment is a fresh repository copy in an isolated container; " +
            "its final state receives a 0–1 reward from the hidden grader."}));
          renderTasks(catalogHost, list(catalog.tasks), "", "", ctx);
          if (list(catalog.draft_tasks).length) {
            catalogHost.appendChild(el("details", {class: "benchmark-history"}, [
              el("summary", {text: list(catalog.draft_tasks).length + " unreleased task records"}),
              el("p", {text: "These published tasks have no verified frozen release. They are not included in the default benchmark view."}),
              el("ul", null, list(catalog.draft_tasks).map(function (task) {
                return el("li", null, [el("a", {href: taskHref(task.name, task.task_id, task.suite_id),
                  text: (task.overview || {}).title || task.task_id})]);
              }))
            ]));
          }
        });
        host.appendChild(warnings(index.warnings, false) || el("span"));
      }
      var history = el("details", {class: "benchmark-history", open: (ctx.params || {}).name ? "" : null}, [
        el("summary", {text: "Creation history & older versions (" + sets.length + " collections)"})
      ]);
      history.appendChild(section("Benchmark collections", [C.tableOf(
        ["collection", "selected sources", "bundled sources", "published tasks", "datasets", "warnings"],
        sets.map(function (item) {
          var counts = item.counts || {};
          return [el("a", {href: benchmarkHref(item.name), text: item.name}),
            count(counts.selected), count(counts.bundled), count(counts.tasks), count(counts.suites), count(item.warnings)];
        })
      )]));
      host.appendChild(history);
      if (!(ctx.params || {}).name && index.catalog) { return; }
      var wanted = (ctx.params || {}).name || sets[0].name;
      var selected = sets.filter(function (item) { return item.name === wanted; })[0];
      if (!selected) { empty(host, "Taskset " + wanted + " is not in this dashboard generation."); return; }
      host.appendChild(el("div", {class: "filters benchmark-section"}, [
        select("Taskset", sets.map(function (item) { return item.name; }), wanted, function (value) {
          C.go("tasks", "", {name: value});
        })
      ]));
      var body = el("div", {text: "loading taskset…"});
      host.appendChild(body);
      load(selected.detail, body, function (data) { C.clear(body); renderSet(body, data, ctx); });
    });
  };
  views.benchmarks = views.tasks;

  function renderSet(host, data, ctx) {
    var counts = data.counts || {};
    host.appendChild(el("h2", {text: data.name}));
    host.appendChild(warnings(data.warnings, false) || el("span"));
    host.appendChild(el("div", {class: "grid cards benchmark-section"}, [
      C.kpi("selected sources", count(counts.selected), null),
      C.kpi("selection rejections", count(counts.rejected), null),
      C.kpi("bundled sources", count(counts.bundled), null),
      C.kpi("published tasks", count(counts.tasks), null),
      C.kpi("active tasks", count(counts.active_tasks), null,
        counts.active_tasks === null ? "active-tasks.json unavailable" : "published active index"),
      C.kpi("frozen suites", count(counts.suites), null)
    ]));
    var builds = list(data.builds);
    host.appendChild(section("Selection and build yield", [
      el("p", {class: "muted", text: "Counts are observed artifacts, not estimates. Build rows are separate runs; " +
        "their task counts must not be summed as distinct tasks. Missing stages are unavailable, not zero yield."}),
      data.selection ? el("p", {text: "Selection: " + count(data.selection.counts.selected) + " selected, " +
        count(data.selection.counts.rejected) + " rejected" +
        (data.selection.counts.selected + data.selection.counts.rejected ?
          " · yield " + rate(data.selection.counts.selected / (data.selection.counts.selected + data.selection.counts.rejected)) :
          " · yield unavailable (no candidates)") + "."}) : el("p", {text: "Selection report unavailable."}),
      data.bundles ? el("p", {text: "Bundle outcomes: " + maps(data.bundles.counts)}) :
        el("p", {text: "Bundle report unavailable."}),
      builds.length ? C.tableOf(["build", "sources", "successful source yield", "published tasks", "rejections", "stages", "source outcomes"],
        builds.map(function (build) {
          var sources = list(build.sources);
          var successful = sources.filter(function (source) { return source.status === "ok" && list(source.published_task_ids).length; }).length;
          return [build.build_id, count(sources.length),
            count(successful) + " / " + count(sources.length) + " · " + (sources.length ? rate(successful / sources.length) : "unavailable"),
            count(list(build.published_task_ids).length),
            count(list(build.rejections).length), maps(build.stage_counts), maps(build.status_counts)];
        })) : el("p", {text: "No published build reports."})
    ]));
    if (data.curation) {
      var curation = data.curation;
      var curationPolicy = curation.policy || {};
      host.appendChild(section("Latest curation outcome", [
        badge(curation.status),
        kv([["created", curation.created_at], ["candidates", count(curation.candidate_tasks)],
          ["accepted tasks", count(list(curation.accepted_task_ids).length)],
          ["suite", curation.suite_id || "no release"], ["split unit", curationPolicy.split_unit],
          ["split policy", Object.keys(curationPolicy.split_ratios || {}).map(function (split) {
            return split + " " + rate(curationPolicy.split_ratios[split]);
          }).join(" · ")], ["anchored reconstructions allowed", decision(curationPolicy.allow_reconstructed)],
          ["error", curation.error]]),
        curation.status === "rejected" || curation.status === "incomplete" ? el("p", {class: "benchmark-warning", role: "alert",
          text: "Curation " + curation.status + ": no successful empty release is implied. Inspect the explicit reasons below. " +
            "Existing frozen suites, if any, are previous releases."}) : null
      ]));
    }
    var rejections = list((data.selection || {}).rejections).slice();
    list((data.bundles || {}).sources).forEach(function (item) {
      if (item.code || (item.status !== "bundled" && item.status !== "reused")) {
        rejections.push({stage: "bundle", source: item.source, code: item.code || item.status, detail: item.detail});
      }
    });
    builds.forEach(function (build) {
      rejections = rejections.concat(list(build.rejections).map(function (item) {
        return {stage: "build " + build.build_id + " / " + (item.stage || "build"), source: item.source,
          task_id: item.task_id, code: item.code, detail: item.detail};
      }));
      list(build.sources).forEach(function (item) {
        if (item.error) {
          rejections.push({stage: "build " + build.build_id + " / " + item.stage, source: item.source,
            code: item.status || "BUILD_ERROR", detail: item.error});
        }
      });
    });
    var suites = list(data.suites);
    suites.forEach(function (suite) { rejections = rejections.concat(list(suite.rejections)); });
    rejections = rejections.concat(list((data.curation || {}).rejections));
    var rejectionHost = el("div");
    if (rejections.length) {
      paged(rejectionHost, ["stage", "source", "task", "reason code", "detail"], rejections, function (item) {
        return [item.stage, sourceLink(item.source), item.task_id || "—", item.code, item.detail || "—"];
      }, "Rejections");
    } else { empty(rejectionHost, "No recorded rejections. Missing reports are not evidence of acceptance."); }
    host.appendChild(section("Explicit rejection reasons", [rejectionHost]));
    host.appendChild(section("Frozen curated suites", [
      suites.length ? C.tableOf(
        ["suite", "tasks", "sources", "repositories", "groups", "train / validation / test", "snapshot"],
        suites.map(function (suite) {
          var summary = suite.summary || {}, splits = summary.splits || {};
          return [el("a", {href: benchmarkHref(data.name, suite.suite_id), text: suite.suite_id}),
            count(summary.tasks), count(summary.sources), count(summary.repositories), count(summary.groups),
            count(splits.train) + " / " + count(splits.validation) + " / " + count(splits.test),
            badge(suite.valid ? "verified" : "incomplete")];
        })
      ) : el("p", {text: "No curated suites. Live tasks are mutable publications, not a frozen evaluation cohort."})
    ]));
    var suiteId = (ctx.params || {}).suite || "";
    var suite = suites.filter(function (item) { return item.suite_id === suiteId; })[0];
    var suiteLabels = {"": "live published tasks"};
    suites.forEach(function (item) { suiteLabels[item.suite_id] = item.suite_id; });
    host.appendChild(el("div", {class: "filters benchmark-section"}, [
      select("Task snapshot", [""].concat(suites.map(function (item) { return item.suite_id; })), suiteId,
        function (value) { C.go("tasks", "", {name: data.name, suite: value}); }, suiteLabels)
    ]));
    if (suiteId && !suite) {
      host.appendChild(el("p", {class: "benchmark-warning", role: "alert",
        text: "Frozen suite " + suiteId + " is unavailable. Live tasks are not substituted for a missing suite."}));
    }
    if (suite) {
      var policy = suite.policy || {}, summary = suite.summary || {};
      host.appendChild(section("Suite " + suite.suite_id, [
        kv([["created", suite.created_at], ["content SHA-256", hash(suite.content_sha256)],
          ["split unit", policy.split_unit], ["seed", count(policy.seed)],
          ["split ratios", Object.keys(policy.split_ratios || {}).map(function (key) {
            return key + " " + rate(policy.split_ratios[key]);
          }).join(" · ")], ["tasks / groups", count(summary.tasks) + " / " + count(summary.groups)],
          ["anchored reconstructions allowed", decision(policy.allow_reconstructed)],
          ["judged scorers allowed", decision(policy.include_judged)]]),
        policy.split_ratios && policy.split_ratios.train === 0 && policy.split_ratios.validation === 0 &&
          policy.split_ratios.test === 1 ? el("p", {class: "muted",
            text: "Explicit test-only release: train 0%, validation 0%, test 100%. " +
              "The default does not imply that these tasks form training data."}) : null,
        el("p", {class: "muted", text: "This is a frozen task copy. Repository/source split units and groups stay " +
          "together; suite and cohort hashes must match before comparisons are enabled. " +
          "Training sampling priority NEVER changes benchmark score."}),
        el("p", {class: "muted", text: "Verification checks manifest/public-JSON bindings. Private bundle/scorer bytes " +
          "are not rescanned here; validation and audit decisions are published evidence, not re-executed checks."}),
        C.tableOf(["group", "split", "tasks", "sources", "repositories"], list(suite.groups).map(function (group) {
          return [el("a", {text: group.group_id, href: benchmarkHref(data.name, suiteId, {group: group.group_id})}),
            group.split, count(group.tasks), count(group.sources), count(group.repositories)];
        })),
        warnings(suite.warnings, !suite.valid)
      ]));
    }
    renderTasks(host, suite ? list(suite.tasks) : suiteId ? [] : list(data.tasks), data.name, suiteId, ctx);
  }

  function filterTasks(tasks, state) {
    var terms = (state.q || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    return tasks.filter(function (task) {
      if (state.split && task.split !== state.split) { return false; }
      if (state.group && task.group_id !== state.group) { return false; }
      if (state.kind && task.kind !== state.kind) { return false; }
      if (state.validation && task.validation !== state.validation) { return false; }
      if (state.family && task.family !== state.family) { return false; }
      if (state.active === "active" && task.active !== true) { return false; }
      if (state.active === "inactive" && task.active !== false) { return false; }
      var haystack = [task.task_id, task.kind, task.repo_id, task.prompt_preview, task.group_id, task.family,
        (task.source || {}).source_id, list(task.tags).join(" "), task.project,
        (task.overview || {}).title, (task.overview || {}).description,
        list((task.overview || {}).success_criteria).join(" "),
        list(((task.overview || {}).environment || {}).runtimes).join(" ")].join(" ").toLowerCase();
      if (state.project && task.project !== state.project) { return false; }
      return terms.every(function (term) { return haystack.indexOf(term) !== -1; });
    });
  }

  function graderRule(scorer) {
    scorer = scorer || {};
    var gates = list(scorer.components).filter(function (component) { return component.hard_gate === true; }).length;
    return (gates ? "Every required check must pass. " : "") +
      (numeric(scorer.pass_threshold) ? "Pass score: at least " + exact(scorer.pass_threshold) + " / 1." :
        "Pass threshold is not recorded.");
  }

  function environmentSummary(task) {
    var overview = task.overview || {}, environment = task.environment || {};
    var runtime = list((overview.environment || {}).runtimes).join(" + ");
    return "Fresh isolated repository copy" + (runtime ? " · " + runtime : "") + ". " +
      (environment.network === "disabled" ? "Project network blocked." :
        environment.network === "policy" ? "Project network restricted." : "Network contract not recorded.");
  }

  function renderTaskCard(task, name, suiteId) {
    var overview = task.overview || {}, grader = overview.grader || {};
    var collection = task.name || name, snapshot = task.suite_id || suiteId;
    var criteria = list(overview.success_criteria);
    var href = taskHref(collection, task.task_id, snapshot);
    var release = task.split === "test" ? "Evaluation only" : task.split === "train" ? "Training split" :
      task.split === "validation" ? "Validation split" : snapshot ? "Frozen release" : "Unreleased task";
    return el("article", {class: "panel benchmark-task-card"}, [
      el("div", {class: "benchmark-card-heading"}, [
        el("span", {class: "muted", text: task.project || collection}), badge(task.provenance_grade)
      ]),
      el("h2", null, [el("a", {href: href, text: overview.title || task.prompt_preview || "Task " + task.task_id})]),
      el("p", {text: overview.description || task.prompt_preview || "Task description is not available."}),
      el("h3", {text: "Environment"}),
      el("p", {text: environmentSummary(task)}),
      el("p", {class: "muted", text: release + " · " +
        (task.environment && task.environment.pinned ? "Image pinned" : "Image not verified") +
        (numeric((task.limits || {}).wall_time_seconds) ?
          " · " + fmt.duration(task.limits.wall_time_seconds) + " time limit" : "")}),
      el("h3", {text: "How it is graded"}),
      el("p", {text: (grader.label || "Grader details not recorded") + " · " +
        (criteria.length ? criteria.length + " behavior requirement" + (criteria.length === 1 ? "" : "s") : "Rubric unavailable")}),
      el("ul", null, criteria.slice(0, 2).map(function (text) { return el("li", {text: text}); })),
      criteria.length > 2 ? el("p", {class: "muted", text: "+" + (criteria.length - 2) + " more requirements in task details"}) : null,
      el("p", {class: "muted", text: graderRule(task.scorer)}),
      el("div", {class: "benchmark-card-actions"}, [
        el("a", {href: href, text: "View task, environment & grader →"})
      ]),
      el("div", {class: "mono muted", text: "Task " + task.task_id + (snapshot ? " · " + snapshot : "")})
    ]);
  }

  function renderTasks(host, tasks, name, suiteId, ctx) {
    var state = {}, initial = ctx.params || {};
    ["q", "split", "group", "kind", "validation", "family", "active", "project", "layout"].forEach(function (key) {
      state[key] = initial[key] || "";
    });
    state.layout = state.layout || "cards";
    var results = el("div");
    var note = el("p", {class: "muted", role: "status", "aria-live": "polite"});
    var filters = el("div", {class: "filters", role: "search", "aria-label": "Benchmark task filters"});
    filters.appendChild(el("label", null, ["Search tasks", el("input", {type: "search", value: state.q,
      "aria-label": "Search tasks", placeholder: "task, goal, grader requirement, project or tooling",
      oninput: function (event) { state.q = event.target.value; refresh(); }})]));
    function facet(label, key, values) {
      filters.appendChild(select(label, [""].concat(unique(values)), state[key], function (value) {
        state[key] = value; refresh();
      }));
    }
    facet("Kind", "kind", tasks.map(function (task) { return task.kind; }));
    facet("Validation", "validation", tasks.map(function (task) { return task.validation; }));
    if (!name) { facet("Project", "project", tasks.map(function (task) { return task.project; })); }
    filters.appendChild(select("View", ["cards", "table"], state.layout, function (value) {
      state.layout = value; refresh();
    }, {cards: "Task cards", table: "Technical table"}));
    if (suiteId) {
      facet("Split", "split", ["train", "validation", "test"]);
      facet("Group", "group", tasks.map(function (task) { return task.group_id; }));
      facet("Family", "family", tasks.map(function (task) { return task.family; }));
    } else if (name) {
      filters.appendChild(select("Publication", ["", "active", "inactive"], state.active, function (value) {
        state.active = value; refresh();
      }));
    }
    function refresh() {
      var filtered = filterTasks(tasks, state);
      note.textContent = count(filtered.length) + " of " + count(tasks.length) + " tasks · " +
        (suiteId ? "frozen suite " + suiteId : name ? "live published metadata" : "current verified releases");
      C.clear(results);
      if (!filtered.length) {
        empty(results, tasks.length ? "No tasks match these filters." :
          "No verified released tasks yet. Open creation history to inspect candidate sources and missing validation.");
        return;
      }
      if (state.layout !== "table") {
        paged(results, null, filtered, function (task) { return renderTaskCard(task, name, suiteId); }, "Tasks");
        return;
      }
      paged(results, ["task / prompt", "kind / provenance grade", "source / repository", "split / group", "family / tags",
        "validation", "audit", "publication"], filtered, function (task) {
        return [el("div", null, [
          el("a", {class: "mono", href: taskHref(task.name || name, task.task_id, task.suite_id || suiteId), text: task.task_id}),
          el("div", {text: (task.overview || {}).title}),
          el("div", {class: "benchmark-preview muted", text: task.prompt_preview || "prompt unavailable"})
        ]), el("div", null, [task.kind || "—", badge(task.provenance_grade)]), el("div", null, [sourceLink(task.source),
          el("div", {class: "mono muted", text: task.repo_id || "—"})]),
        el("div", null, [task.split || "unassigned", el("div", {class: "mono muted", text: task.group_id || "—"})]),
        el("div", null, [task.family || "—", el("div", {text: list(task.tags).join(", ") || "—"}),
          task.priority !== undefined && task.priority !== null ? el("div", {class: "muted", text: "priority " + task.priority}) : null]),
        badge(task.validation), badge(task.audit), suiteId ? "frozen" :
          task.active === true ? "active" : task.active === false ? "inactive" : "index unavailable"];
      }, "Tasks");
    }
    host.appendChild(section("Tasks & environments", [filters, note, results]));
    refresh();
  }

  function withSet(container, ctx, type, done) {
    var parts = (ctx.id || "").split("/");
    var name = parts[0], id = parts.slice(1).join("/");
    var host = el("div", {text: "loading benchmark metadata…"});
    container.appendChild(host);
    var manifest = C.manifest.benchmarks;
    if (!manifest) { C.clear(host); empty(host, "No benchmarks in this archive."); return; }
    load(manifest.index, host, function (index) {
      var set = list(index.tasksets).filter(function (item) { return item.name === name; })[0];
      C.clear(host);
      if (!set) { empty(host, "Taskset " + name + " is not in this build."); return; }
      load(set.detail, host, function (data) {
        var suiteId = (ctx.params || {}).suite || "";
        var suite = list(data.suites).filter(function (item) { return item.suite_id === suiteId; })[0];
        var records = type === "eval" ? list(data.evaluations) : suiteId ? list((suite || {}).tasks) : list(data.tasks);
        var item = records.filter(function (record) { return (type === "eval" ? record.eval_id : record.task_id) === id; })[0];
        if (!item) {
          empty(host, (type === "eval" ? "Evaluation " : "Task ") + id + " is not in this " +
            (suiteId ? "frozen suite. Live task metadata is not substituted." : "dashboard generation."));
          return;
        }
        load(item.detail, host, function (detail) { C.clear(host); done(host, detail); });
      });
    });
  }

  views["benchmark-task"] = function (container, ctx) {
    withSet(container, ctx, "task", function (host, data) { renderTask(host, data); });
  };
  views.task = views["benchmark-task"];

  function renderTask(host, data) {
    host.appendChild(el("p", null, [el("a", {href: benchmarkHref(data.name, data.suite_id), text: "← tasks"})]));
    var overview = data.overview || {}, scorer = data.scorer || {}, environment = data.environment || {};
    host.appendChild(el("h1", {text: overview.title || "Task " + data.task_id}));
    host.appendChild(el("p", {class: "sub"}, [
      badge(data.provenance_grade), " · " + (data.project || data.name) +
        (data.suite_id ? " · Frozen benchmark version" : " · Published task without a frozen version")
    ]));
    host.appendChild(el("p", {class: "mono muted", text: "Task " + data.task_id}));
    host.appendChild(warnings(data.warnings, list(data.warnings).length > 0) || el("span"));
    host.appendChild(section("At a glance", [
      el("p", {text: overview.description || data.prompt || "Task requirements are unavailable."}),
      kv([["project", data.project], ["grader", (overview.grader || {}).label],
        ["passing result", graderRule(scorer)], ["environment", environmentSummary(data)],
        ["release use", (data.membership || {}).split === "test" ? "Evaluation only — not training data" :
          (data.membership || {}).split || "Not assigned to a training/evaluation split"]])
    ]));
    host.appendChild(section("How the grader decides", [
      el("p", {text: "The hidden grader checks the final repository, not whether the agent copied the original patch. " +
        "A different correct implementation is allowed. " + graderRule(scorer)}),
      el("ul", null, list(overview.success_criteria).map(function (text) { return el("li", {text: text}); })),
      el("p", {class: "muted", text: "A required check failure makes the reward 0. A grader or sandbox failure has no numeric reward."}),
      el("details", null, [
        el("summary", {text: "Scoring components and weights"}),
        C.tableOf(["component", "kind", "weight", "required"], list(scorer.components).map(function (component) {
          return [component.id, component.kind, exact(component.weight), decision(component.hard_gate)];
        }))
      ])
    ]));
    var inspector = section("Grader inputs & output", [
      el("p", {text: scorer.mode === "deterministic" || scorer.mode === "code" ?
        "This grader runs code; it does not send a prompt to an LLM. Its input is the agent's final repository, the public task and the selected seed." :
        "This grader combines the declared scoring logic with a pinned judge. Inspect its judge prompt, allowed inputs and result schema below."}),
      el("pre", {class: "benchmark-prompt", text: JSON.stringify(data.grader_contract || {}, null, 2)})
    ]);
    var inspectorResult = el("div");
    inspector.appendChild(el("button", {type: "button", text: "Inspect grader content",
      "aria-label": "Inspect grader content", onclick: function () {
        if (global.RETRO.operator) { global.RETRO.operator.inspectGrader(inspectorResult, data); }
        else { empty(inspectorResult, "Local grader inspection is unavailable. Rebuild the dashboard and start retro dashboard serve."); }
      }}));
    inspector.appendChild(inspectorResult);
    host.appendChild(inspector);
    var environmentOverview = overview.environment || {}, repository = data.repository || {}, limits = data.limits || {};
    host.appendChild(section("Environment & episode contract", [
      el("p", {text: "This is a repository-task environment with a final 0–1 reward, not a running training job or an RL optimizer."}),
      el("ol", {class: "benchmark-episode-steps"}, [
        el("li", null, [el("strong", {text: "Reset"}), " — create a fresh repository copy at commit ",
          hash(repository.base_sha ? repository.base_sha.slice(0, 12) : null), " inside the pinned container."]),
        el("li", null, [el("strong", {text: "Work"}), " — the agent edits files and runs checks in that isolated copy."]),
        el("li", null, [el("strong", {text: "Grade"}), " — export its final repository and run the hidden grader separately."])
      ]),
      kv([["declared tooling", list(environmentOverview.runtimes).join(", ") || "Not recorded"],
        ["image", environmentOverview.image_label || "Not recorded"],
        ["project network", environment.network === "disabled" ? "Blocked; model access is supplied separately by the runner" :
          environment.network || "Not recorded"],
        ["working directory", environmentOverview.workdir],
        ["setup / environment checks", maps(environmentOverview.command_counts)],
        ["agent time limit", numeric(limits.wall_time_seconds) ? fmt.duration(limits.wall_time_seconds) : "Not recorded"]])
    ]));
    var originDescriptions = {
      exact_replay: "Rebuilt from a captured clean starting repository state.",
      anchored_reconstruction: "Rebuilt from a known Git revision because a clean starting state was not captured. " +
        "This is a reconstructed task, not an exact replay.",
      adjacent_synthesis: "A generated variant inspired by the captured task, not a replay of the original."
    };
    host.appendChild(section("Why this is a task", [
      el("p", {text: originDescriptions[data.provenance_grade] || "The task's origin is not recorded."}), kv([
      ["starting repository", overview.base_failure],
      ["accepted outcome", overview.accepted_outcome],
      ["origin", badge(data.provenance_grade)]
    ])]));
    host.appendChild(section("Full task instructions", [
      el("pre", {class: "benchmark-prompt", text: data.prompt || "Public prompt unavailable."}),
      data.prompt_truncated ? el("p", {class: "benchmark-warning", text: "Prompt exceeds the dashboard text limit; truncated."}) : null
    ]));
    var technical = el("details", {class: "benchmark-history"}, [
      el("summary", {text: "Provenance, exact versions & validation evidence"})
    ]);
    host.appendChild(technical);
    host = technical;
    host.appendChild(el("p", {class: "muted", text: "Summary evidence, hashes and event IDs only. Hidden scorer source, " +
      "accepted patches, candidate code, environment variables and full assistant trajectories are not published here. " +
      "Text is bounded and obvious credentials/code blocks are redacted."}));
    if (data.membership) {
      var membership = data.membership;
      host.appendChild(section("Frozen suite membership and curation evidence", [kv([
        ["split", membership.split], ["group", membership.group_id], ["family", membership.family],
        ["provenance grade", el("span", null, [badge(data.provenance_grade), " · ", hash(data.provenance_grade)])],
        ["tags", list(membership.tags).join(", ") || "none"],
        ["training sampling priority", exact(membership.priority)], ["curation reason", membership.curation_reason],
        ["goal summary", membership.goal_summary], ["task content SHA-256", hash(membership.content_sha256)]
      ]), el("p", {class: "muted", text: "Exact replay, anchored reconstruction and adjacent synthesis are distinct provenance " +
        "grades. Reconstructions are not guessed bases. Sampling priority NEVER changes benchmark score."})]));
    }
    host.appendChild(el("div", {class: "grid two benchmark-section"}, [
      C.panel("Repository and pinned environment", [kv([
        ["repository", hash(repository.repo_id)], ["base commit", hash(repository.base_sha)],
        ["base tree", hash(repository.base_tree)], ["outcome commit", hash(repository.outcome_sha)],
        ["outcome tree", hash(repository.outcome_tree)], ["state confidence", repository.state_confidence],
        ["base / outcome resolution", (repository.base_resolution || "—") + " / " + (repository.outcome_resolution || "—")],
        ["image", hash(environment.image)], ["digest pinned", decision(environment.pinned)],
        ["environment SHA-256", hash(environment.environment_id)], ["network", environment.network],
        ["environment base / outcome validated", decision((environment.validated || {}).base) + " / " +
          decision((environment.validated || {}).outcome)], ["environment validation runs", count(environment.validation_runs)]
      ])]),
      C.panel("Publication and scorer summary", [kv([
        ["build", data.build_id], ["published", data.published_at], ["scorer mode", scorer.mode],
        ["pass threshold", exact(scorer.pass_threshold)]
      ].concat(Object.keys(data.hashes || {}).map(function (key) { return [key, hash(data.hashes[key])]; }))),
      C.tableOf(["component", "kind", "weight", "hard gate"], list(scorer.components).map(function (component) {
        return [component.id, component.kind, exact(component.weight), decision(component.hard_gate)];
      }))])
    ]));
    var provenance = data.provenance || {}, goal = provenance.goal || {};
    host.appendChild(section("Source and goal evidence", [kv([
      ["captured source", sourceLink(data.source)], ["prompt resolution", provenance.prompt_mode],
      ["user events", eventLinks(provenance.user_event_ids, data.source)],
      ["goal introduced", eventLinks(goal.introduced_event_id ? [goal.introduced_event_id] : [], data.source)],
      ["goal closed", eventLinks(goal.closed_event_id ? [goal.closed_event_id] : [], data.source)],
      ["goal", goal.summary]
    ]), el("p", {class: "muted", text: "Event links open the captured session drilldown with the event ID. " +
      "Use its transcript/normalized artifact to locate the event; trajectories are not copied into benchmark payloads."})]));
    var rubric = data.rubric || {};
    host.appendChild(section("Observable and regression rubric", [
      C.tableOf(["observable", "importance", "description", "source evidence"], list(rubric.observables).map(function (item) {
        return [item.id, item.importance, item.description, eventLinks(item.evidence, data.source)];
      })),
      el("h3", {class: "benchmark-section", text: "Regressions to protect"}),
      list(rubric.regressions_to_protect).length ? el("ul", null, rubric.regressions_to_protect.map(function (text) {
        return el("li", {text: text});
      })) : el("p", {text: "No recorded regression rubric."}),
      list(rubric.forbidden_shortcuts).length ? el("details", null, [
        el("summary", {text: "Forbidden shortcuts"}),
        el("ul", null, rubric.forbidden_shortcuts.map(function (text) { return el("li", {text: text}); }))
      ]) : null
    ]));
    var audit = data.audit || {};
    host.appendChild(section("Audit decision", [
      badge(audit.decision), el("p", {class: "muted",
        text: "Only the independent audit decision and finding counts are published, not private audit/code evidence."}),
      kv(Object.keys(audit.counts || {}).map(function (key) { return [key, count(audit.counts[key])]; }))
    ]));
    renderValidation(host, data.validation || {});
  }

  function renderValidation(host, validation) {
    var cases = list(validation.cases).slice();
    REQUIRED.forEach(function (kind) {
      if (!cases.some(function (item) { return item.kind === kind; })) {
        cases.push({kind: kind, status: "missing", detail: "Mandatory validation evidence unavailable."});
      }
    });
    host.appendChild(section("Mandatory scorer validation", [
      badge(validation.status),
      el("p", {class: "muted", text: "Each mandatory construct has its own expected result. A successful base/no-op " +
        "or regression validation check can correctly produce a low/zero reward or fail the candidate pass threshold. " +
        "An unavailable score is never converted into zero."}),
      C.tableOf(["construct / case", "target component", "status", "check satisfied", "reward", "candidate passed",
        "runs", "failed gates", "detail"],
        cases.map(function (item) {
          return [el("div", null, [item.kind, el("div", {class: "mono muted", text: item.case_id || "—"})]),
            item.targeted_component || "—", item.status, decision(item.ok), exact(item.score_total), decision(item.passed), count(item.runs),
            list(item.hard_gate_failures).join(", ") || "—",
            el("div", null, [item.code || "", item.detail || "", el("details", null, [
              el("summary", {text: "Component scores"}),
              kv(Object.keys(item.component_values || {}).map(function (key) { return [key, exact(item.component_values[key])]; }))
            ])])];
        })),
      list(validation.codes).length ? el("p", {text: "Validation codes: " + validation.codes.join(", ")}) : null
    ]));
    var repeat = validation.repeatability || {};
    host.appendChild(section("Repeatability", [kv([
      ["repeatability satisfied", decision(repeat.ok)],
      ["deterministic mismatches", list(repeat.deterministic_mismatches).join(", ") || "none recorded"],
      ["performance spread", exact(repeat.performance_spread)],
      ["judge standard deviations", Object.keys(repeat.judge_stdev || {}).map(function (key) {
        return key + " " + exact(repeat.judge_stdev[key]);
      }).join(" · ") || "—"], ["summary", repeat.detail]
    ])]));
  }

  views["benchmark-eval"] = function (container, ctx) {
    function saved() {
      var host = el("div");
      container.appendChild(host);
      withSet(host, ctx, "eval", function (body, data) { renderEvaluation(body, data, ctx); });
    }
    if (global.RETRO.operator && global.RETRO.operator.renderLiveEvaluation(container, ctx, saved)) { return; }
    saved();
  };
  views["agent-run"] = views["benchmark-eval"];

  function scoreValue(attempt) { return attempt.status === "scored" && numeric(attempt.score) ? attempt.score : null; }
  function agentScore(agent) { return agent.scored_attempts > 0 && numeric(agent.benchmark_score) ? agent.benchmark_score : null; }

  function renderEvaluation(host, data, ctx) {
    var cohort = data.cohort || {};
    host.appendChild(el("p", null, [el("a", {href: "#/experiments", text: "← experiments"})]));
    host.appendChild(el("h1", {text: "Agent run " + data.eval_id}));
    host.appendChild(el("p", {class: "sub", text: data.name + " · published " + (data.generated_at || "time unavailable")}));
    host.appendChild(warnings(data.warnings, !data.comparison_allowed) || el("span"));
    host.appendChild(section("Bound evaluation cohort", [
      badge(data.cohort_status), kv([
        ["suite", cohort.suite_id ? el("a", {text: cohort.suite_id, href: benchmarkHref(data.name, cohort.suite_id)}) : "unbound"],
        ["suite content SHA-256", hash(cohort.suite_content_sha256)], ["split", cohort.split || "unbound"],
        ["task count", data.cohort ? count(list(cohort.task_ids).length) : "unavailable"],
        ["seeds", list(cohort.seeds).join(", ") || "unavailable"],
        ["task IDs", el("details", null, [el("summary", {text: "Cohort task IDs"}),
          el("p", {class: "mono benchmark-hash", text: list(cohort.task_ids).join(", ") || "unavailable"})])]
      ]),
      el("p", {class: "muted", text: "Comparisons below belong ONLY to this evaluation's suite hash, split, task IDs and " +
        "seeds. Other evaluations are never merged or ranked here. " + SCORE_NOTE}),
      el("p", {class: "muted", text: "Hash verification covers published metadata/public JSON, not a rescan of private " +
        "bundle or scorer bytes. The dashboard never re-executes scoring or audit code."})
    ]));
    if (!data.comparison_allowed) {
      host.appendChild(el("p", {class: "benchmark-warning", role: "alert",
        text: "Agent comparison and heatmap disabled: cohort binding or aggregate/attempt evidence is unavailable, " +
          "malformed or incompatible. The published summaries below are inspection-only, not a ranking."}));
    }
    var agents = list(data.agents).slice().sort(function (a, b) { return a.agent_id.localeCompare(b.agent_id); });
    host.appendChild(section(data.comparison_allowed ? "Agent comparison — this cohort only" : "Published agent summaries (unverified)", [
      el("p", {class: "muted", text: "Agents are listed alphabetically, not ranked by incomplete coverage."}),
      C.tableOf(["agent", "benchmark score", "95% source interval", "pass rate", "pass@k", "valid coverage", "scored / requested",
        "missing attempt artifacts", "status counts", "error counts"],
        agents.map(function (agent) {
          var interval = agent.score_interval;
          var passAtK = Object.keys(agent.pass_at_k || {}).map(function (k) {
            return "@" + k + " " + rate(agent.pass_at_k[k]);
          }).join(" · ") || "—";
          return [agent.agent_id, reward(agentScore(agent)),
            interval ? reward(interval.lower) + "–" + reward(interval.upper) + " (" + count(interval.sources) + " sources)" : "—",
            rate(agent.pass_rate), passAtK, rate(agent.valid_coverage),
            count(agent.scored_attempts) + " / " + count(agent.requested_attempts),
            count(agent.missing_attempts || 0), maps(agent.status_counts), maps(agent.error_counts)];
        })),
      el("p", {class: "muted", text: "Intervals resample scored rollout sources, not model draws or unseen tasks. " +
        "Pass@k uses scored trials only; outages are not failures or trials. Incomplete coverage remains visible."})
    ]));
    if (data.comparison_allowed) { renderHeatmap(host, data, agents); }
    host.appendChild(section("Component means and resources (separate from reward)", agents.map(function (agent) {
      var resources = agent.resources || {};
      return el("details", null, [el("summary", {text: agent.agent_id + " · scored-attempt resources and components"}),
        kv([
          ["resource sample size", count(resources.scored_attempts)],
          ["mean / total wall time", numeric(resources.wall_time_ms_mean) ? fmt.duration(resources.wall_time_ms_mean / 1000) +
            " / " + fmt.duration(resources.wall_time_ms_total / 1000) : "—"],
          ["mean / total cost", numeric(resources.cost_usd_mean) ? fmt.money(resources.cost_usd_mean) +
            " / " + fmt.money(resources.cost_usd_total) : "—"],
          ["mean tokens", maps(resources.tokens_mean)], ["total tokens", maps(resources.tokens_total)]
        ]),
        C.tableOf(["component", "mean scored value"], Object.keys(agent.component_means || {}).map(function (key) {
          return [key, reward(agent.component_means[key])];
        }))]);
    })));
    renderAttempts(host, data, ctx);
  }

  function renderHeatmap(host, data, agents) {
    var cohort = data.cohort;
    var tableHost = el("div");
    var page = 0, tasks = list(cohort.task_ids);
    var taskMatrix = Object.create(null);
    agents.forEach(function (agent) {
      var entries = Object.create(null);
      list(agent.tasks).forEach(function (task) { entries[task.task_id] = task; });
      taskMatrix[agent.agent_id] = entries;
    });
    function refresh() {
      C.clear(tableHost);
      var headers = el("tr", null, [el("th", {scope: "col", text: "task"})].concat(agents.map(function (agent) {
        return el("th", {scope: "col", text: agent.agent_id});
      })));
      var body = tasks.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(function (taskId) {
        return el("tr", null, [el("th", {scope: "row"}, [
          el("a", {class: "mono", text: taskId, href: taskHref(data.name, taskId, cohort.suite_id)})
        ])].concat(agents.map(function (agent) {
          var task = taskMatrix[agent.agent_id][taskId] || {};
          var score = task.scored_attempts > 0 && numeric(task.mean_score) ? task.mean_score : null;
          var requested = numeric(task.cohort_requested_attempts) ? task.cohort_requested_attempts : list(cohort.seeds).length;
          var coverage = count(task.scored_attempts || 0) + " / " + count(requested);
          var label = agent.agent_id + ", task " + taskId + ": " + (score === null ? "unavailable reward" :
            "mean reward " + reward(score) + ", standard deviation " + reward(task.std_score)) +
            "; " + coverage + " attempts scored; pass rate " + rate(task.pass_rate) + "; " + maps(task.status_counts);
          return el("td", {class: score === null ? "benchmark-unscored" : "benchmark-score-" + Math.min(4, Math.floor(score * 5))}, [
            el("a", {href: C.buildHash("agent-run", data.name + "/" + data.eval_id,
              {task: taskId, agent: agent.agent_id}), title: label, "aria-label": label, text: reward(score)}),
            el("div", {class: "muted", text: coverage + " scored"})
          ]);
        })));
      });
      tableHost.appendChild(el("div", {class: "table-wrap"}, [
        el("table", {class: "benchmark-heatmap", "aria-label": "Mean scored reward by task and agent"}, [
          el("caption", {text: "Reward heatmap — same bound cohort. Each cell shows mean reward and scored/requested coverage; " +
            "— means unavailable, not zero."}), el("thead", null, headers), el("tbody", null, body)
        ])
      ]));
      if (tasks.length > PAGE_SIZE) {
        tableHost.appendChild(el("div", {class: "benchmark-pagination"}, [
          el("button", {type: "button", text: "previous tasks", disabled: page === 0 ? "" : null,
            onclick: function () { page -= 1; refresh(); }}),
          el("span", {role: "status", text: "task page " + (page + 1) + " of " + Math.ceil(tasks.length / PAGE_SIZE)}),
          el("button", {type: "button", text: "next tasks", disabled: (page + 1) * PAGE_SIZE >= tasks.length ? "" : null,
            onclick: function () { page += 1; refresh(); }})
        ]));
      }
    }
    host.appendChild(section("Score by task heatmap", [tableHost]));
    refresh();
  }

  function renderAttempts(host, data, ctx) {
    var recorded = list(data.attempts), missing = list(data.missing_attempts);
    var attempts = recorded.concat(missing), state = {}, initial = ctx.params || {};
    ["task", "agent", "status", "seed"].forEach(function (key) { state[key] = initial[key] || ""; });
    var results = el("div"), note = el("p", {class: "muted", role: "status", "aria-live": "polite"});
    var filters = el("div", {class: "filters", role: "search", "aria-label": "Attempt filters"});
    [["Task", "task", "task_id"], ["Agent", "agent", "agent_id"], ["Status", "status", "status"]].forEach(function (facet) {
      filters.appendChild(select(facet[0], [""].concat(unique(attempts.map(function (attempt) { return attempt[facet[2]]; }))),
        state[facet[1]], function (value) { state[facet[1]] = value; refresh(); }));
    });
    filters.appendChild(select("Seed", [""].concat(unique(attempts.map(function (attempt) { return String(attempt.seed); }))),
      state.seed, function (value) { state.seed = value; refresh(); }));
    function refresh() {
      var filtered = attempts.filter(function (attempt) {
        return (!state.task || state.task === attempt.task_id) && (!state.agent || state.agent === attempt.agent_id) &&
          (!state.status || state.status === attempt.status) && (!state.seed || state.seed === String(attempt.seed));
      });
      note.textContent = count(filtered.length) + " of " + count(attempts.length) +
        (missing.length ? " cohort cells (" + count(recorded.length) + " published per-seed attempts; " +
          count(missing.length) + " requested cells have no artifact). " : " published per-seed attempts. ") +
        "Filters do not change the evaluation cohort or aggregate score.";
      C.clear(results);
      if (!filtered.length) { empty(results, "No published attempts match these filters."); return; }
      paged(results, ["task", "agent", "seed", "status", "reward", "passed", "wall time", "cost", "error summary", "evidence"],
        filtered, function (attempt) {
          var score = scoreValue(attempt);
          return [(data.cohort || {}).suite_id ?
            el("a", {class: "mono", text: attempt.task_id,
              href: taskHref(data.name, attempt.task_id, data.cohort.suite_id)}) : attempt.task_id,
          attempt.agent_id, count(attempt.seed), attempt.status, reward(score),
          score === null ? "unavailable" : decision(attempt.passed),
          numeric(attempt.wall_time_ms) ? fmt.duration(attempt.wall_time_ms / 1000) : "—",
          numeric(attempt.cost_usd) ? fmt.money(attempt.cost_usd) : "—", attempt.failure_reason || attempt.error || "—",
          el("details", null, [el("summary", {text: "Attempt summary"}),
            kv([["attempt ID", attempt.attempt_id], ["source ID", attempt.source_id], ["created", attempt.created_at],
              ["pass threshold", exact(attempt.pass_threshold)], ["tokens", maps(attempt.tokens)],
              ["error", attempt.error], ["grader reason", attempt.failure_reason]].concat(Object.keys(attempt.hashes || {}).map(function (key) {
                return [key, hash(attempt.hashes[key])];
              }))),
            C.tableOf(["component", "value", "weight", "hard gate", "gate passed", "grader evidence"], list(attempt.components).map(function (item) {
              return [item.id, exact(score === null ? null : item.value), exact(item.weight),
                decision(item.hard_gate), decision(item.gate_passed),
                el("ul", null, list(item.evidence).map(function (evidence) { return el("li", {text: evidence.summary || evidence.ref}); }))];
            })), attempt.synthetic ? null : el("button", {type: "button", text: "Inspect grader input & result",
              "aria-label": "Inspect grader input & result " + attempt.task_id + " " + attempt.agent_id + " seed " + attempt.seed,
              onclick: function (event) {
                var output = el("div");
                event.target.parentNode.appendChild(output);
                if (global.RETRO.operator) { global.RETRO.operator.inspectRun(output, data, attempt); }
                else { empty(output, "Local run inspection is unavailable. Start retro dashboard serve."); }
              }}), warnings(attempt.warnings, true)])];
        }, "Attempts");
    }
    host.appendChild(section("Per-seed attempts and errors", [filters, note, results]));
    refresh();
  }

  global.RETRO.benchmarks = {
    filterTasks: filterTasks, scoreValue: scoreValue, agentScore: agentScore, explainWarning: explainWarning,
    renderTasks: renderTasks, renderEvaluation: renderEvaluation, load: load, warnings: warnings
  };
})(window);
