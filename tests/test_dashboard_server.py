"""Local experiment controls are explicit, loopback-only and artifact-safe."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer.ghostlab_cli import sha256_file
from retro.benchmarks.task_scorer.run import AgentSpec
from retro.benchmarks.task_scorer.suite import CurationPolicy, curate_taskset
from retro.dashboard.benchmarks import read_benchmarks
from retro.dashboard.publish import build_dashboard
from retro.dashboard.server import ControlError, DashboardServer, ExperimentController
from tests.test_task_scorer_suite import make_published_fixture


@pytest.fixture
def live_server(tmp_path: Path):
    fixture = make_published_fixture(tmp_path)
    curate_taskset(
        fixture.layout, "pilot", suite_id="http-v1", policy=CurationPolicy(allow_reconstructed=True)
    )
    output = tmp_path / "dashboard"
    build_dashboard(fixture.layout, output, publish=True)
    controller = ExperimentController(
        fixture.layout,
        output,
        agents=[AgentSpec.from_path(fixture.good_agent), AgentSpec.from_path(fixture.bad_agent)],
        ghostlab=fixture.client(),
    )
    server = DashboardServer(controller, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield fixture, controller, server
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
        thread.join(timeout=10)


def request(server, path, *, method="GET", payload=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    try:
        outgoing = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload)
            outgoing.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        raw = response.read()
        result = (
            json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        )
        return response.status, result, dict(response.getheaders())
    finally:
        connection.close()


def setup_payload(fixture):
    return {
        "title": "Candidate comparison",
        "description": "Correct behavior versus an incomplete implementation",
        "datasets": [
            {"name": "pilot", "suite_id": "http-v1", "split": "test", "task_ids": [fixture.task_id]}
        ],
        "agents": ["candidate-good", "candidate-bad"],
        "seeds": [0, 1],
        "aut_timeout_seconds": 1800,
    }


def token_headers(controller):
    return {"X-Retro-Control-Token": controller.control_token}


def test_context_and_static_dashboard_do_not_run_agents(live_server):
    fixture, controller, server = live_server
    status, data, headers = request(server, "/api/experiments/context")
    assert status == 200
    assert data["control_token"] == controller.control_token
    assert len(data["agents"]) == 2
    assert data["datasets"][0]["task_ids"] == [fixture.task_id]
    assert "config_path" not in json.dumps(data)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in headers
    assert controller.jobs() == []
    status, html, _ = request(server, "/index.html")
    assert status == 200 and b">Tasks<" in html and b">Experiments<" in html


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Retro-Control-Token": "wrong-token"},
        {"Origin": "https://untrusted.example"},
        {"Host": "untrusted.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_foreign_origins_and_missing_tokens_cannot_create_jobs(live_server, headers):
    fixture, controller, server = live_server
    sent = token_headers(controller) if headers else {}
    sent.update(headers)
    status, data, _ = request(
        server, "/api/experiments", method="POST", payload=setup_payload(fixture), headers=sent
    )
    assert status == 403
    assert data["error"]
    assert controller.jobs() == []


def test_private_grader_source_requires_current_local_token(live_server):
    fixture, controller, server = live_server
    path = f"/api/tasks/pilot/{fixture.task_id}/grader?suite=http-v1"
    assert request(server, path)[0] == 403
    status, data, _ = request(server, path, headers=token_headers(controller))
    assert status == 200
    assert data["prompt_status"] == "no_llm_prompt"
    assert any(file["path"] == "score.py" for file in data["files"])
    assert "score_total" in data["output_schema"]["properties"]


def test_create_then_explicit_start_scores_every_trial_and_preserves_task_artifacts(live_server):
    fixture, controller, server = live_server
    before = sha256_file(fixture.paths.task_dir(fixture.task_id) / "public" / "base.bundle")
    headers = token_headers(controller)
    status, planned, _ = request(
        server, "/api/experiments", method="POST", payload=setup_payload(fixture), headers=headers
    )
    assert status == 201
    assert planned["status"] == "planned"
    assert planned["requested_attempts"] == 4
    assert all(run["completed_attempts"] == 0 for run in planned["runs"])
    experiment_id = planned["experiment_id"]
    status, started, _ = request(
        server, f"/api/experiments/{experiment_id}/run", method="POST", payload={}, headers=headers
    )
    assert status == 202 and started["accepted"] is True
    controller.close()
    status, complete, _ = request(server, f"/api/experiments/{experiment_id}", headers=headers)
    assert status == 200 and complete["status"] == "completed"
    assert complete["progress"] == {
        "completed_attempts": 4, "scored_attempts": 4, "unscored_attempts": 0,
    }
    assert sum(run["scored_attempts"] for run in complete["runs"]) == 4
    assert len({run["eval_id"] for run in complete["runs"]}) == 1
    evaluation_id = complete["runs"][0]["eval_id"]
    status, results, _ = request(server, f"/api/evaluations/pilot/{evaluation_id}", headers=headers)
    assert status == 200 and results["comparison_allowed"]
    assert len(results["attempts"]) == 4
    assert sorted(attempt["score"] for attempt in results["attempts"]) == [0, 0, 1, 1]
    for attempt in results["attempts"]:
        path = fixture.paths.attempt_dir(evaluation_id, fixture.task_id, attempt["agent_id"], attempt["seed"])
        assert not (path / "workspace").exists()
        assert not (path / ".runtime-inputs").exists()
        assert "experiment" in (path / "prompt.txt").read_text(encoding="utf-8")
        assert (path / "attempt-metadata.json").exists()
    assert sha256_file(fixture.paths.task_dir(fixture.task_id) / "public" / "base.bundle") == before
    view = read_benchmarks(fixture.layout)
    assert view.jobs[0]["experiment_id"] == experiment_id
    assert view.jobs[0]["status"] == "completed"
    assert view.ready_tasks[0].get("latest_evaluation") is None

    status, rejected, _ = request(
        server, f"/api/experiments/{experiment_id}/cleanup", method="POST", payload={}, headers=headers
    )
    assert status == 400 and rejected["error"]
    status, cleaned, _ = request(
        server,
        f"/api/experiments/{experiment_id}/cleanup",
        method="POST",
        payload={"prune_artifacts": True},
        headers=headers,
    )
    assert status == 200 and cleaned["cleanup"]["artifacts_pruned"] is True
    for attempt in results["attempts"]:
        path = fixture.paths.attempt_dir(evaluation_id, fixture.task_id, attempt["agent_id"], attempt["seed"])
        assert (path / "score-report.json").exists()
        assert (path / "scorer" / "scorer-run.json").exists()
    assert sha256_file(fixture.paths.task_dir(fixture.task_id) / "public" / "base.bundle") == before


def test_unregistered_configs_and_unbounded_requests_are_rejected(live_server):
    fixture, controller, server = live_server
    payload = setup_payload(fixture)
    payload["agents"] = [str(fixture.good_agent)]
    status, data, _ = request(
        server, "/api/experiments", method="POST", payload=payload, headers=token_headers(controller)
    )
    assert status == 400 and "registered" in data["error"]
    payload = setup_payload(fixture)
    payload["seeds"] = list(range(100))
    assert (
        request(
            server, "/api/experiments", method="POST", payload=payload, headers=token_headers(controller)
        )[0]
        == 400
    )
    assert controller.jobs() == []


@pytest.mark.parametrize("path", ["/../outside", "/assets/../../outside", "/data/", "/raw/private.json"])
def test_server_never_serves_archive_or_external_directory_content(live_server, path):
    _fixture, _controller, server = live_server
    assert request(server, path)[0] in (400, 403, 404)


def test_saved_plan_remains_discoverable_when_dashboard_publication_fails(live_server, monkeypatch):
    fixture, controller, server = live_server
    monkeypatch.setattr(controller, "_publish", lambda: (_ for _ in ()).throw(OSError("output unavailable")))
    status, data, _ = request(
        server,
        "/api/experiments",
        method="POST",
        payload=setup_payload(fixture),
        headers=token_headers(controller),
    )
    assert status == 500
    assert "was saved" in data["error"]
    jobs = controller.jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "planned"
    another = ExperimentController(fixture.layout, controller.output_dir)
    saved = another.job(jobs[0]["experiment_id"])
    assert "publication failed" in saved["controller_error"]


def test_same_agent_id_with_changed_configuration_cannot_start_a_frozen_plan(live_server):
    fixture, controller, _server = live_server
    planned = controller.create(setup_payload(fixture))
    changed = json.loads(fixture.good_agent.read_text(encoding="utf-8"))
    changed["runtime"]["model"] = "different-model"
    fixture.good_agent.write_text(json.dumps(changed), encoding="utf-8")
    restarted = ExperimentController(
        fixture.layout, controller.output_dir,
        agents=[AgentSpec.from_path(fixture.good_agent), AgentSpec.from_path(fixture.bad_agent)],
    )
    with pytest.raises(ControlError, match="frozen setup"):
        restarted.start(planned["experiment_id"])
    assert restarted.job(planned["experiment_id"])["status"] == "planned"
