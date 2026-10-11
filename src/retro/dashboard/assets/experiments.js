/* Frozen datasets, experiment setup and on-demand localhost inspection. */
(function (global) {
  "use strict";

  var C = global.RETRO.core, B = global.RETRO.benchmarks, el = C.el, views = global.RETRO.views;
  var sessionContext = null;

  function list(value) { return Array.isArray(value) ? value : []; }
  function errorText(error) { return error && error.message ? error.message : String(error); }
  function message(host, text, failed) {
    host.appendChild(el("p", {class: failed ? "benchmark-warning" : "muted",
      role: failed ? "alert" : "status", text: text}));
  }
  function jsonBlock(title, value) {
    return el("details", null, [el("summary", {text: title}),
      el("pre", {class: "operator-evidence", text: JSON.stringify(value, null, 2)})]);
  }
  function datasetHref(name, suite) { return C.buildHash("dataset", name + "/" + suite, {}); }
  function localPage() {
    return global.location.protocol === "http:" &&
      ["127.0.0.1", "localhost", "[::1]"].indexOf(global.location.hostname) !== -1;
  }
  function transport(path, method, payload, token) {
    if (!localPage()) { return Promise.reject(new Error("Local controls require retro dashboard serve on localhost.")); }
    var options = {method: method || "GET", credentials: "same-origin", cache: "no-store",
      headers: {"Accept": "application/json"}};
    if (token) { options.headers["X-Retro-Control-Token"] = token; }
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    return global.fetch("/api/" + path, options).then(function (response) {
      return response.text().then(function (text) {
        var body;
        try { body = JSON.parse(text); }
        catch (error) { throw new Error("Local controls are unavailable (HTTP " + response.status + "). Start retro dashboard serve."); }
        if (!response.ok) { throw new Error(body.error || "Local request failed: HTTP " + response.status); }
        return body;
      });
    });
  }
  function context() {
    if (sessionContext) { return Promise.resolve(sessionContext); }
    return transport("experiments/context").then(function (data) {
      if (!data.control_token) { throw new Error("The server did not provide an experiment control token."); }
      sessionContext = data;
      return data;
    });
  }
  function api(path, method, payload) {
    return context().then(function (data) {
      return transport(path, method, payload, data.control_token);
    });
  }
  function index(host, done) {
    var manifest = C.manifest.benchmarks;
    if (!manifest) { message(host, "No task datasets or experiment records have been published."); return; }
    B.load(manifest.index, host, done);
  }

  views.evals = function (container, ctx) {
    container.appendChild(el("h1", {text: "Evals"}));
    container.appendChild(el("p", {class: "sub",
      text: "Frozen task datasets: the repository environments and graders an experiment will use. Agent runs and scores live in Experiments."}));
    var host = el("div", {text: "Loading task datasets…"});
    container.appendChild(host);
    index(host, function (data) {
      C.clear(host);
      var all = list(data.datasets), historical = (ctx.params || {}).history === "1";
      var datasets = all.filter(function (item) { return historical || item.current && item.valid; });
      host.appendChild(el("div", {class: "experiment-actions"}, [
        el("p", {class: "muted", text: datasets.length + " frozen datasets" +
          (historical ? " including older/unverified versions" : " in current verified releases")}),
        el("a", {href: C.buildHash("evals", "", {history: historical ? "" : "1"}),
          text: historical ? "Show current datasets" : "Show older versions"})
      ]));
      if (!datasets.length) { message(host, "No verified dataset is available. Curate tasks before creating an experiment."); }
      var cards = el("div", {class: "grid two"});
      datasets.forEach(function (dataset) {
        var summary = dataset.summary || {}, splits = summary.splits || {};
        cards.appendChild(C.panel(dataset.name, [
          el("p", {text: dataset.suite_id}),
          el("p", {text: summary.tasks + " tasks · " + summary.repositories + " repositories · " + summary.sources + " source trajectories"}),
          el("p", {class: "muted", text: "Train / validation / test: " +
            (splits.train || 0) + " / " + (splits.validation || 0) + " / " + (splits.test || 0)}),
          el("p", {text: dataset.valid ? "Verified frozen task copies" : "Unverified version — not available for new experiments"}),
          el("div", {class: "experiment-actions"}, [
            el("a", {href: datasetHref(dataset.name, dataset.suite_id), text: "View dataset tasks"}),
            dataset.valid ? el("a", {href: C.buildHash("experiments", "", {new: "1",
              dataset: dataset.name + "/" + dataset.suite_id}), text: "Use in experiment"}) : null
          ]),
          jsonBlock("Exact dataset version", {created_at: dataset.created_at, content_sha256: dataset.content_sha256})
        ]));
      });
      host.appendChild(cards);
    });
  };

  views.dataset = function (container, ctx) {
    container.appendChild(el("p", null, [el("a", {href: "#/evals", text: "← eval datasets"})]));
    var parts = (ctx.id || "").split("/"), name = parts[0], suiteId = parts[1];
    var host = el("div", {text: "Loading frozen dataset…"});
    container.appendChild(host);
    index(host, function (data) {
      var dataset = list(data.datasets).filter(function (item) {
        return item.name === name && item.suite_id === suiteId;
      })[0];
      C.clear(host);
      if (!dataset) { message(host, "This frozen dataset is not in the published archive.", true); return; }
      B.load(dataset.detail, host, function (collection) {
        C.clear(host);
        var suite = list(collection.suites).filter(function (item) { return item.suite_id === suiteId; })[0];
        if (!suite) { message(host, "Frozen task copies are unavailable; live tasks are not substituted.", true); return; }
        host.appendChild(el("h1", {text: name + " · " + suiteId}));
        host.appendChild(el("p", {class: "sub",
          text: "This dataset fixes task, repository, environment and grader versions. It contains no agent results."}));
        host.appendChild(B.warnings(suite.warnings, !suite.valid) || el("span"));
        if (suite.valid) {
          host.appendChild(el("p", null, [el("a", {href: C.buildHash("experiments", "", {new: "1",
            dataset: name + "/" + suiteId}), text: "Set up an experiment with this dataset"})]));
        }
        host.appendChild(jsonBlock("Dataset policy and exact version", {
          content_sha256: suite.content_sha256, policy: suite.policy, summary: suite.summary
        }));
        B.renderTasks(host, list(suite.tasks), name, suiteId, ctx);
      });
    });
  };

  function savedRuns(host, data) {
    var evaluations = list(data.evaluations).slice().sort(function (a, b) {
      return (b.generated_at || "").localeCompare(a.generated_at || "");
    });
    host.appendChild(C.panel("Saved agent runs", [
      el("p", {class: "muted", text: "Older runs may not have a separately saved experiment plan. Each result remains scoped to its own frozen tasks and seeds."}),
      evaluations.length ? C.tableOf(["agent run", "dataset", "split", "seeds", "recorded trials", "verification"],
        evaluations.map(function (run) {
          var cohort = run.cohort || {};
          return [el("a", {href: C.buildHash("agent-run", run.name + "/" + run.eval_id, {}), text: run.eval_id}),
            cohort.suite_id ? el("a", {href: datasetHref(run.name, cohort.suite_id), text: cohort.suite_id}) : "Not bound",
            cohort.split || "Not recorded", list(cohort.seeds).join(", ") || "Not recorded",
            run.attempts, run.comparison_allowed ? "Verified" : "Inspection only"];
        })) : el("p", {text: "No agent results have been saved."})
    ]));
  }

  function jobList(host, jobs) {
    host.appendChild(C.panel("Experiment jobs", [
      jobs.length ? C.tableOf(["experiment", "status", "datasets", "agents", "requested trials", "created"],
        jobs.map(function (job) {
          return [el("a", {href: C.buildHash("experiment", job.experiment_id, {}), text: job.title || job.experiment_id}),
            job.status, list(job.datasets).length, list(job.agents).length, job.requested_attempts,
            job.created_at || "Not recorded"];
        })) : el("p", {text: "No experiment plans yet. Set up a job below; saving a plan does not run a model."})
    ]));
  }

  views.experiments = function (container, ctx) {
    container.appendChild(el("h1", {text: "Experiments"}));
    container.appendChild(el("p", {class: "sub",
      text: "Define a question, freeze datasets and agent configurations, then explicitly start a job and inspect every trial's grade and evidence."}));
    var host = el("div", {text: "Loading experiment records…"}), setup = C.panel("New experiment", []);
    container.appendChild(host); container.appendChild(setup);
    index(host, function (data) {
      C.clear(host);
      var jobHost = el("div", {"data-experiment-jobs": ""});
      host.appendChild(jobHost);
      jobList(jobHost, list(data.jobs));
      savedRuns(host, data);
      host.appendChild(B.warnings(data.warnings, false) || el("span"));
    });
    if (!localPage()) {
      message(setup, "This exported dashboard is read-only. Use retro dashboard serve to enable localhost job controls and private grader inspection.");
      return;
    }
    message(setup, "Connecting to localhost experiment controls…");
    context().then(function (data) {
      C.clear(setup); setup.appendChild(el("h2", {text: "New experiment"}));
      renderSetup(setup, data, ctx);
      return api("experiments");
    }).then(function (data) {
      var jobs = list(data.jobs);
      if (jobs.length) {
        var liveJobs = host.querySelector("[data-experiment-jobs]");
        if (liveJobs) { C.clear(liveJobs); jobList(liveJobs, jobs); }
      }
    }).catch(function (error) {
      C.clear(setup); setup.appendChild(el("h2", {text: "Local controls unavailable"}));
      message(setup, errorText(error), true);
      message(setup, "Start retro dashboard serve with registered --agent configuration files. Saved results above remain available.");
    });
  };

  function renderSetup(host, data, ctx) {
    var agents = list(data.agents), datasets = list(data.datasets), selected = (ctx.params || {}).dataset;
    if (!agents.length || !datasets.length) {
      message(host, !agents.length ? "No candidate agents are registered. Restart the local server with --agent <config.json>." :
        "No verified frozen task dataset is available.", true);
      return;
    }
    var form = el("form", {class: "experiment-setup", "aria-label": "Experiment setup"});
    var title = el("input", {type: "text", required: "", maxlength: "160", "aria-label": "Experiment name"});
    var description = el("textarea", {rows: "3", maxlength: "4000", "aria-label": "Question or hypothesis"});
    var seeds = el("input", {type: "text", value: "0,1,2", "aria-label": "Seeds"});
    var timeout = el("input", {type: "number", min: "1", max: "3600", value: "1800", "aria-label": "Agent time limit seconds"});
    form.appendChild(el("label", null, ["Experiment name", title]));
    form.appendChild(el("label", null, ["Question / hypothesis", description]));
    var datasetChoices = [], agentChoices = [];
    var datasetBox = el("fieldset", {class: "experiment-choices"}, [el("legend", {text: "Frozen task datasets"})]);
    datasets.forEach(function (dataset) {
      var box = el("input", {type: "checkbox", "aria-label": dataset.name + " " + dataset.suite_id + " " + dataset.split});
      box.checked = selected ? selected === dataset.name + "/" + dataset.suite_id && dataset.split === "test" : datasetChoices.length === 0;
      datasetChoices.push({input: box, dataset: dataset});
      datasetBox.appendChild(el("label", null, [box, dataset.name + " · " + dataset.suite_id + " · " +
        dataset.split + " · " + dataset.task_ids.length + " tasks"]));
    });
    var agentBox = el("fieldset", {class: "experiment-choices"}, [el("legend", {text: "Registered candidate agents"})]);
    agents.forEach(function (agent) {
      var box = el("input", {type: "checkbox", "aria-label": "Candidate " + agent.agent_id});
      box.checked = agentChoices.length === 0;
      agentChoices.push({input: box, agent: agent});
      agentBox.appendChild(el("label", null, [box, agent.agent_id + (agent.model ? " · " + agent.model : "")]));
    });
    form.appendChild(datasetBox); form.appendChild(agentBox);
    form.appendChild(el("div", {class: "grid two"}, [
      el("label", null, ["Seeds (comma-separated)", seeds]),
      el("label", null, ["Agent time limit (seconds)", timeout])
    ]));
    var count = el("p", {role: "status", "aria-live": "polite"}), result = el("div");
    function seedValues() {
      var values = seeds.value.split(",").map(function (value) { return value.trim(); });
      if (!values.length || values.some(function (value) { return !/^(0|[1-9][0-9]*)$/.test(value); })) {
        throw new Error("Seeds must be non-negative integers separated by commas.");
      }
      var numbers = values.map(Number);
      if (numbers.some(function (value) { return !Number.isSafeInteger(value); }) ||
          numbers.some(function (value, i) { return numbers.indexOf(value) !== i; })) {
        throw new Error("Seeds must be distinct safe integers.");
      }
      return numbers;
    }
    function requestedCount() {
      return datasetChoices.filter(function (choice) { return choice.input.checked; })
        .reduce(function (total, choice) { return total + choice.dataset.task_ids.length; }, 0) *
        agentChoices.filter(function (choice) { return choice.input.checked; }).length * seedValues().length;
    }
    function updateCount() {
      try { count.textContent = requestedCount() + " agent trials. Saving this plan does not call a model."; }
      catch (error) { count.textContent = errorText(error); }
    }
    form.addEventListener("change", updateCount); form.addEventListener("input", updateCount);
    form.appendChild(count);
    message(form, "Runs are tagged experiment + ignore and excluded from valuable learning traces. Disposable workspaces are cleaned after each trial; bulky retained exports can be removed after completion.");
    message(form, "Candidate models/scripts are pinned through registered configurations. Credentials are not editable or displayed in this page.");
    var create = el("button", {type: "submit", text: "Save experiment plan"});
    form.appendChild(create); form.appendChild(result);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); C.clear(result);
      var payload;
      try {
        var requested = requestedCount();
        if (!title.value.trim()) { throw new Error("Give the experiment a name."); }
        if (!requested) { throw new Error("Select at least one dataset and candidate agent."); }
        if (requested > data.max_attempts) { throw new Error("This local server permits at most " + data.max_attempts + " trials per job."); }
        payload = {title: title.value.trim(), description: description.value,
          datasets: datasetChoices.filter(function (choice) { return choice.input.checked; }).map(function (choice) {
            return {name: choice.dataset.name, suite_id: choice.dataset.suite_id,
              split: choice.dataset.split, task_ids: choice.dataset.task_ids};
          }),
          agents: agentChoices.filter(function (choice) { return choice.input.checked; }).map(function (choice) { return choice.agent.agent_id; }),
          seeds: seedValues(), aut_timeout_seconds: Number(timeout.value)};
      } catch (error) { message(result, errorText(error), true); return; }
      create.disabled = true;
      api("experiments", "POST", payload).then(function (job) {
        C.go("experiment", job.experiment_id, {});
      }).catch(function (error) { message(result, errorText(error), true); })
        .then(function () { create.disabled = false; });
    });
    host.appendChild(form); updateCount();
  }

  function drawJob(host, job, refresh) {
    C.clear(host);
    host.appendChild(el("h1", {text: job.title || job.experiment_id}));
    host.appendChild(el("p", {class: "sub", text: (job.description || "No hypothesis was recorded.") + " · " + job.status}));
    host.appendChild(el("p", {text: "Experiment " + job.experiment_id + " · " + job.requested_attempts +
      " requested trials · seeds " + list(job.seeds).join(", ")}));
    var progress = job.progress || {};
    message(host, (progress.completed_attempts || 0) + " trials finished; " +
      (progress.scored_attempts || 0) + " have numeric grades; " + (progress.unscored_attempts || 0) +
      " have unavailable grades. A valid task failure is 0; an agent, sandbox or grader error is not 0.",
      (progress.unscored_attempts || 0) > 0);
    message(host, "Experiment traces are tagged and excluded from valuable learning captures. Reports and grades are kept separately from task datasets.");
    host.appendChild(C.panel("Frozen job setup", [
      C.tableOf(["dataset", "split", "tasks", "exact version"], list(job.datasets).map(function (dataset) {
        return [el("a", {href: datasetHref(dataset.name, dataset.suite_id), text: dataset.name + " · " + dataset.suite_id}),
          dataset.split, list(dataset.task_ids).length, dataset.suite_content_sha256 || "Not recorded"];
      })),
      C.tableOf(["candidate", "model", "runtime / script", "configuration SHA-256", "referenced assets"], list(job.agents).map(function (agent) {
        var runtime = agent.runtime || {};
        return [agent.agent_id, agent.model || "Not recorded",
          [runtime.backend, runtime.agent, runtime.parser, runtime.runner, runtime.script].filter(Boolean).join(" · ") || "Not recorded",
          agent.config_sha256, agent.asset_count === null || agent.asset_count === undefined ? "Not recorded" : agent.asset_count];
      })),
      jsonBlock("Recorded experiment settings", {seeds: job.seeds, limits: job.limits, setup_sha256: job.setup_sha256, tags: job.tags})
    ]));
    if (job.error) { message(host, job.error, true); }
    var actions = el("div", {class: "experiment-actions"}), feedback = el("div");
    host.appendChild(actions); host.appendChild(feedback);
    actions.appendChild(el("button", {type: "button", text: "Refresh job", onclick: refresh}));
    if (localPage() && ["planned", "interrupted"].indexOf(job.status) !== -1) {
      actions.appendChild(el("button", {type: "button",
        text: (job.status === "interrupted" ? "Resume experiment (" : "Start experiment (") +
          job.requested_attempts + " trials)", onclick: function (event) {
          event.target.disabled = true;
          api("experiments/" + encodeURIComponent(job.experiment_id) + "/run", "POST", {}).then(function () { refresh(); })
            .catch(function (error) { message(feedback, errorText(error), true); event.target.disabled = false; });
        }}));
      message(host, "Starting runs the registered agents and may incur model charges. No model is called merely by viewing or saving this plan.");
    }
    if (localPage() && ["completed", "failed", "interrupted"].indexOf(job.status) !== -1) {
      var confirm = el("input", {type: "checkbox", "aria-label": "Confirm removal of bulky experiment exports"});
      actions.appendChild(el("label", null, [confirm, "Remove bulky candidate exports; keep grades, logs and hashes"]));
      actions.appendChild(el("button", {type: "button", text: "Clean experiment artifacts", onclick: function (event) {
        if (!confirm.checked) { message(feedback, "Confirm export removal before cleanup. Task datasets and score reports will not be deleted.", true); return; }
        event.target.disabled = true;
        api("experiments/" + encodeURIComponent(job.experiment_id) + "/cleanup", "POST", {prune_artifacts: true})
          .then(function () { refresh(); })
          .catch(function (error) { message(feedback, errorText(error), true); event.target.disabled = false; });
      }}));
    }
    host.appendChild(C.panel("Dataset × agent executions", [
      list(job.runs).length ? C.tableOf(["dataset", "candidate", "status", "scored / requested", "results / every trial"], list(job.runs).map(function (run) {
        return [run.name || run.dataset_name, run.agent_id, run.status,
          (run.scored_attempts || 0) + " / " + run.requested_attempts,
          run.results_available ? el("a", {href: C.buildHash("agent-run", (run.name || run.dataset_name) + "/" + run.eval_id,
            {agent: run.agent_id}), text: "Inspect grades and failure evidence"}) : run.error || "Not started"];
      })) : el("p", {text: "No agent execution has started."})
    ]));
    if (job.cleanup) { host.appendChild(jsonBlock("Experiment cleanup record", job.cleanup)); }
    if (job.status === "running" && localPage()) {
      global.setTimeout(function () { if (host.isConnected) { refresh(); } }, 3000);
    }
  }

  views.experiment = function (container, ctx) {
    container.appendChild(el("p", null, [el("a", {href: "#/experiments", text: "← experiments"})]));
    var host = el("div", {text: "Loading experiment setup and results…"});
    container.appendChild(host);
    function refresh() {
      if (localPage()) {
        api("experiments/" + encodeURIComponent(ctx.id)).then(function (job) { drawJob(host, job, refresh); })
          .catch(function (error) { C.clear(host); message(host, errorText(error), true); });
      } else {
        index(host, function (data) {
          var job = list(data.jobs).filter(function (item) { return item.experiment_id === ctx.id; })[0];
          if (job) { drawJob(host, job, refresh); }
          else { C.clear(host); message(host, "This experiment is not in the saved dashboard generation.", true); }
        });
      }
    }
    refresh();
  };

  function inspectGrader(host, task) {
    C.clear(host); message(host, "Loading private grader content from the local archive…");
    var path = "tasks/" + encodeURIComponent(task.name) + "/" + encodeURIComponent(task.task_id) + "/grader" +
      (task.suite_id ? "?suite=" + encodeURIComponent(task.suite_id) : "");
    api(path).then(function (data) {
      C.clear(host);
      message(host, data.prompt_status === "no_llm_prompt" ?
        "No LLM prompt is used. The pinned deterministic grader code below receives the declared input contract." :
        "The pinned judge prompt is shown below. Agent configuration credentials remain private.");
      if (data.judge_prompt !== null) {
        host.appendChild(C.panel("Judge prompt", [el("pre", {class: "operator-evidence", text: data.judge_prompt})]));
      }
      host.appendChild(jsonBlock("Grader invocation and runtime", data.manifest));
      host.appendChild(jsonBlock("Task content supplied to grader", data.public_task));
      host.appendChild(jsonBlock("Grader input contract", data.input_contract));
      host.appendChild(jsonBlock("Expected grader output schema", data.output_schema));
      list(data.files).forEach(function (file) {
        host.appendChild(el("details", null, [el("summary", {text: file.path + (file.truncated ? " (truncated)" : "")}),
          el("p", {class: "mono benchmark-hash", text: "SHA-256 " + file.sha256}),
          el("pre", {class: "operator-evidence", text: file.content})]));
      });
      list(data.notes).forEach(function (note) { message(host, note); });
    }).catch(function (error) { C.clear(host); message(host, errorText(error), true); });
  }

  function inspectRun(host, evaluation, attempt) {
    C.clear(host); message(host, "Loading the saved input and output for this trial…");
    var path = "runs/" + [evaluation.name, evaluation.eval_id, attempt.task_id, attempt.agent_id, String(attempt.seed)]
      .map(encodeURIComponent).join("/") + "/grader";
    api(path).then(function (data) {
      C.clear(host);
      message(host, data.input_status === "recorded" ? "This is the recorded grader input payload." :
        "The grader input payload was not retained. No substitute input is fabricated.");
      host.appendChild(jsonBlock("Recorded grader input", data.score_input));
      host.appendChild(jsonBlock("Actual grader output", data.score_report));
      host.appendChild(jsonBlock("Scorer execution and isolation evidence", data.scorer_attestation));
      host.appendChild(jsonBlock("Attempt status and exact candidate hash", data.attempt));
      if (list(data.missing_artifacts).length) { message(host, "Artifacts not retained: " + data.missing_artifacts.join(", ")); }
      if ((evaluation.cohort || {}).suite_id) {
        var codeHost = el("div");
        host.appendChild(el("button", {type: "button", text: "Inspect this run's grader logic", onclick: function () {
          inspectGrader(codeHost, {name: evaluation.name, task_id: attempt.task_id, suite_id: evaluation.cohort.suite_id});
        }}));
        host.appendChild(codeHost);
      }
      list(data.notes).forEach(function (note) { message(host, note); });
    }).catch(function (error) { C.clear(host); message(host, errorText(error), true); });
  }

  function renderLiveEvaluation(container, ctx, fallback) {
    if (!localPage()) { return false; }
    var parts = (ctx.id || "").split("/"), host = el("div");
    container.appendChild(host);
    message(host, "Loading current saved results from the local archive…");
    api("evaluations/" + parts.map(encodeURIComponent).join("/")).then(function (data) {
      C.clear(host); B.renderEvaluation(host, data, ctx);
    }).catch(function (error) {
      C.clear(host);
      message(host, "Live result refresh failed: " + errorText(error) + " Displaying the saved dashboard snapshot.", true);
      fallback();
    });
    return true;
  }

  global.RETRO.operator = {inspectGrader: inspectGrader, inspectRun: inspectRun, localPage: localPage,
    renderLiveEvaluation: renderLiveEvaluation};
})(window);
