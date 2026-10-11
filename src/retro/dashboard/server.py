"""Opt-in localhost controls over the otherwise static dashboard."""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..archive_health import require_archive_volume
from ..benchmarks.task_scorer.build import utc_now
from ..benchmarks.task_scorer.experiments import (
    cleanup_experiment,
    create_experiment,
    list_experiments,
    load_experiment,
    run_experiment,
)
from ..benchmarks.task_scorer.ghostlab_cli import GhostlabCli, GhostlabError, write_json
from ..benchmarks.task_scorer.run import AgentSpec, TaskVerificationError, parse_seeds
from ..locking import LockUnavailableError
from ..storage import Layout
from .benchmarks import _text, project_experiment, read_benchmarks
from .inspector import InspectionError, _json, inspect_grader, inspect_run
from .publish import BUILDS_DIRNAME, CURRENT_LINK, PUBLISHED_LINKS, build_dashboard

LOGGER = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 64 * 1024
_SEED = re.compile(r"^(0|[1-9][0-9]*)$")


class ControlError(ValueError):
    pass


class ExperimentController:
    def __init__(
        self,
        layout: Layout,
        output_dir: Path,
        *,
        agents: Sequence[AgentSpec] = (),
        ghostlab: GhostlabCli | None = None,
        max_attempts: int = 100,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ControlError("max_attempts must be a positive integer")
        if len({agent.agent_id for agent in agents}) != len(agents):
            raise ControlError("Registered agent IDs must be unique")
        for agent in agents:
            agent.verify()
        self.layout = layout
        self.output_dir = output_dir
        self.agents = {agent.agent_id: agent for agent in agents}
        self.ghostlab = ghostlab
        self.max_attempts = max_attempts
        self.control_token = secrets.token_urlsafe(32)
        self._guard = threading.Lock()
        self._publish_guard = threading.Lock()
        self._worker: threading.Thread | None = None
        self._active_job: str | None = None
        self._worker_errors: dict[str, str] = {}

    def _datasets(self) -> list[dict[str, Any]]:
        view = read_benchmarks(self.layout)
        sets = {ref["name"]: payload for ref, payload in view.payloads}
        selections = []
        for dataset in view.datasets:
            if not dataset["valid"] or not dataset["current"]:
                continue
            collection = sets[dataset["detail"]["name"]]
            suite = next(item for item in collection["suites"] if item["suite_id"] == dataset["suite_id"])
            for split in ("test", "validation", "train"):
                ids = [task["task_id"] for task in suite["tasks"] if task["split"] == split]
                if ids:
                    selections.append(
                        {
                            "name": dataset["name"],
                            "suite_id": dataset["suite_id"],
                            "split": split,
                            "task_ids": ids,
                            "suite_content_sha256": dataset["content_sha256"],
                        }
                    )
        return selections

    def context(self) -> dict[str, Any]:
        descriptors = []
        for spec in self.agents.values():
            spec.verify()
            labels = spec.runtime_labels()
            descriptors.append(
                {
                    **{
                        key: _text(value, 250)
                        for key, value in labels.items()
                        if key in {"model", "backend", "agent", "parser", "runner", "script"}
                    },
                    "agent_id": spec.agent_id,
                    "config_sha256": spec.config_sha256,
                }
            )
        return {
            "schema_version": "retro-experiment-controls-v1",
            "control_token": self.control_token,
            "agents": descriptors,
            "datasets": self._datasets(),
            "max_attempts": self.max_attempts,
            "active_experiment": self._active_job,
        }

    def jobs(self) -> list[dict[str, Any]]:
        return [project_experiment(job) for job in list_experiments(self.layout)]

    def job(self, experiment_id: str) -> dict[str, Any]:
        record = load_experiment(self.layout, experiment_id)
        projected = project_experiment(record)
        control_path = Path(record["path"]) / "controller-status.json"
        if control_path.exists() or control_path.is_symlink():
            control = _json(self.layout.root, control_path)
            if (
                control.get("schema_version") != "retro-dashboard-control-status-v1"
                or control.get("experiment_id") != experiment_id
            ):
                raise ControlError("Saved dashboard controller status is invalid")
            if control.get("error"):
                projected["controller_error"] = _text(control["error"], 2000, multiline=True)
                projected["error"] = "Dashboard controller: " + projected["controller_error"]
        if experiment_id in self._worker_errors:
            projected["controller_error"] = self._worker_errors[experiment_id]
            projected["error"] = self._worker_errors[experiment_id]
        if self._active_job == experiment_id and projected["status"] != "running":
            projected["status"] = "running"
        return projected

    def _controller_status(self, experiment_id: str, error: str | None) -> None:
        record = load_experiment(self.layout, experiment_id)
        path = Path(record["path"]) / "controller-status.json"
        if path.is_symlink():
            raise ControlError("Dashboard controller status must not be a symlink")
        write_json(
            path,
            {
                "schema_version": "retro-dashboard-control-status-v1",
                "experiment_id": experiment_id,
                "updated_at": utc_now(),
                "error": error,
            },
        )

    def evaluation(self, name: str, eval_id: str) -> dict[str, Any]:
        view = read_benchmarks(self.layout)
        for _reference, payload in view.payloads:
            if payload.get("name") == name and payload.get("eval_id") == eval_id:
                return payload
        raise ControlError("This agent run has no published result artifacts yet")

    def _publish(self) -> None:
        require_archive_volume(self.layout.root)
        with self._publish_guard:
            build_dashboard(self.layout, self.output_dir, mode="auto", publish=True)

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed_fields = {
            "title",
            "description",
            "datasets",
            "agents",
            "seeds",
            "aut_timeout_seconds",
            "scorer_timeout_seconds",
        }
        if set(payload) - allowed_fields:
            raise ControlError("Experiment setup has unknown fields")
        title = payload.get("title")
        description = payload.get("description", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ControlError("Experiment name must contain 1-160 characters")
        if not isinstance(description, str) or len(description) > 4000:
            raise ControlError("Experiment question must be at most 4000 characters")
        raw_datasets, raw_agents = payload.get("datasets"), payload.get("agents")
        if not isinstance(raw_datasets, list) or not raw_datasets:
            raise ControlError("Select at least one frozen dataset")
        if (
            not isinstance(raw_agents, list)
            or not raw_agents
            or any(not isinstance(item, str) for item in raw_agents)
        ):
            raise ControlError("Select registered candidate agent IDs")
        if len(set(raw_agents)) != len(raw_agents) or any(item not in self.agents for item in raw_agents):
            raise ControlError("Candidate agents must be distinct and registered on this server")
        raw_seeds = payload.get("seeds", [0])
        if (
            not isinstance(raw_seeds, list)
            or not raw_seeds
            or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in raw_seeds)
            or len(set(raw_seeds)) != len(raw_seeds)
        ):
            raise ControlError("Seeds must be distinct non-negative integers")
        seeds = parse_seeds(raw_seeds)
        choices = {(item["name"], item["suite_id"], item["split"]): item for item in self._datasets()}
        datasets = []
        identities: set[tuple[str, str, str]] = set()
        for raw in raw_datasets:
            if not isinstance(raw, dict) or set(raw) - {"name", "suite_id", "split", "task_ids"}:
                raise ControlError("Dataset selection has unknown or invalid fields")
            name, suite, split = raw.get("name"), raw.get("suite_id"), raw.get("split", "test")
            if not isinstance(name, str) or not isinstance(suite, str) or not isinstance(split, str):
                raise ControlError("Dataset identity must contain collection, version and split")
            identity = (name, suite, split)
            if identity not in choices or identity in identities:
                raise ControlError("Select distinct current verified dataset splits")
            identities.add(identity)
            task_ids = raw.get("task_ids", choices[identity]["task_ids"])
            if (
                not isinstance(task_ids, list)
                or not task_ids
                or any(not isinstance(task_id, str) for task_id in task_ids)
                or len(set(task_ids)) != len(task_ids)
                or not set(task_ids) <= set(choices[identity]["task_ids"])
            ):
                raise ControlError("Selected tasks must belong to the frozen dataset split")
            datasets.append({**raw, "split": identity[2], "task_ids": task_ids})
        requested = sum(len(dataset["task_ids"]) for dataset in datasets) * len(raw_agents) * len(seeds)
        if requested > self.max_attempts:
            raise ControlError(f"This server permits at most {self.max_attempts} agent trials per job")
        timeouts: dict[str, float | None] = {}
        for field in ("aut_timeout_seconds", "scorer_timeout_seconds"):
            value = payload.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 3600
                or not math.isfinite(value)
            ):
                raise ControlError(f"{field} must be a positive finite number at most 3600")
            timeouts[field] = float(value) if value is not None else None
        require_archive_volume(self.layout.root)
        record = create_experiment(
            self.layout,
            title=title.strip(),
            description=description,
            datasets=datasets,
            agents=[self.agents[agent_id] for agent_id in raw_agents],
            seeds=seeds,
            aut_timeout_seconds=timeouts["aut_timeout_seconds"],
            scorer_timeout_seconds=timeouts["scorer_timeout_seconds"],
        )
        try:
            self._publish()
        except (OSError, ValueError, LockUnavailableError) as exc:
            detail = (
                f"Experiment {record['experiment_id']} was saved, but dashboard publication failed: {exc}"
            )
            self._controller_status(record["experiment_id"], _text(detail, 2000, multiline=True))
            raise OSError(detail) from exc
        return project_experiment(record)

    def start(self, experiment_id: str) -> dict[str, Any]:
        require_archive_volume(self.layout.root)
        with self._guard:
            if self._worker is not None and self._worker.is_alive():
                raise LockUnavailableError("Another experiment is running in this local server")
            job = self.job(experiment_id)
            if job["status"] not in {"planned", "interrupted"}:
                raise ControlError(
                    "Only a planned or interrupted experiment can be started; failed setup needs a new plan"
                )
            if any(agent["agent_id"] not in self.agents for agent in job["agents"]):
                raise ControlError("Register this experiment's pinned candidate agents before starting it")
            if any(
                self.agents[agent["agent_id"]].config_sha256 != agent["config_sha256"]
                for agent in job["agents"]
            ):
                raise ControlError(
                    "Registered candidate configurations differ from this experiment's frozen setup"
                )
            for agent in job["agents"]:
                self.agents[agent["agent_id"]].verify()
            self._active_job = experiment_id
            self._worker_errors.pop(experiment_id, None)
            self._worker = threading.Thread(
                target=self._run_job, args=(experiment_id,), name=f"retro-experiment-{experiment_id}"
            )
            self._worker.start()
        return {"experiment_id": experiment_id, "status": "running", "accepted": True}

    def _run_job(self, experiment_id: str) -> None:
        try:
            run_experiment(self.layout, experiment_id, ghostlab=self.ghostlab)
            self._publish()
            self._controller_status(experiment_id, None)
        except Exception as exc:
            detail = _text(str(exc), 2000, multiline=True) or type(exc).__name__
            self._worker_errors[experiment_id] = detail
            LOGGER.exception("Experiment %s failed; its durable state must be inspected", experiment_id)
            try:
                self._controller_status(experiment_id, detail)
            except (OSError, ValueError) as record_error:
                LOGGER.exception(
                    "Could not persist controller failure for %s: %s", experiment_id, record_error
                )
        finally:
            with self._guard:
                self._active_job = None

    def cleanup(self, experiment_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) - {"prune_artifacts"} or payload.get("prune_artifacts") is not True:
            raise ControlError("Explicitly confirm prune_artifacts=true before deleting bulky exports")
        if self._active_job == experiment_id:
            raise ControlError("A running experiment cannot be cleaned")
        require_archive_volume(self.layout.root)
        record = cleanup_experiment(self.layout, experiment_id, prune_artifacts=True)
        self._publish()
        self._controller_status(experiment_id, None)
        return project_experiment(record)

    def close(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            LOGGER.warning(
                "Waiting for active experiment %s to finish and clean its workspace", self._active_job
            )
            worker.join()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller: ExperimentController, port: int = 8877) -> None:
        self.controller = controller
        super().__init__(("127.0.0.1", port), DashboardRequestHandler)


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    server: DashboardServer

    def __init__(
        self, request: socket.socket, client_address: tuple[str, int], server: DashboardServer
    ) -> None:
        super().__init__(request, client_address, server, directory=str(server.controller.output_dir))

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _origin(self) -> bool:
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._response(403, {"error": "Local dashboard requests require a loopback Host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            self._response(403, {"error": "Cross-origin dashboard control requests are not allowed"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._response(403, {"error": "Cross-site dashboard requests are not allowed"})
            return False
        return True

    def _authorized(self) -> bool:
        token = self.headers.get("X-Retro-Control-Token", "")
        if not secrets.compare_digest(token, self.server.controller.control_token):
            self._response(403, {"error": "A current local dashboard control token is required"})
            return False
        return True

    def _response(self, status: int, data: Mapping[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _static_path(self) -> bool:
        decoded = unquote(urlsplit(self.path).path)
        if decoded in {"", "/"}:
            decoded = "/index.html"
        parts = Path(decoded.lstrip("/")).parts
        if "\x00" in decoded or "\\" in decoded or ".." in parts:
            self._response(400, {"error": "Invalid dashboard resource path"})
            return False
        if decoded not in {"", "/", "/index.html"} and (not parts or parts[0] not in {"assets", "data"}):
            self._response(404, {"error": "Dashboard resource not found"})
            return False
        root = self.server.controller.output_dir
        target = root / decoded.lstrip("/")
        try:
            target.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            self._response(403, {"error": "Dashboard resources must remain inside their generation"})
            return False
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                published = (
                    index == 0 and part in PUBLISHED_LINKS
                    and current.readlink() == Path(CURRENT_LINK) / part
                )
                if published:
                    try:
                        (root / CURRENT_LINK).resolve().relative_to((root / BUILDS_DIRNAME).resolve())
                    except (ValueError, OSError):
                        published = False
                if not published:
                    self._response(403, {"error": "Only dashboard publication symlinks are served"})
                    return False
        if parts and not target.is_file():
            self._response(404, {"error": "Dashboard resource not found"})
            return False
        return True

    def do_HEAD(self) -> None:
        if self._origin() and self._static_path():
            super().do_HEAD()

    def do_GET(self) -> None:
        if not self._origin():
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            if self._static_path():
                super().do_GET()
            return
        parts = unquote(parsed.path).strip("/").split("/")
        if parts == ["api", "experiments", "context"]:
            self._dispatch(lambda: self.server.controller.context())
            return
        if not self._authorized():
            return
        if parts == ["api", "experiments"]:
            self._dispatch(lambda: {"jobs": self.server.controller.jobs()})
        elif len(parts) == 3 and parts[:2] == ["api", "experiments"]:
            self._dispatch(lambda: self.server.controller.job(parts[2]))
        elif len(parts) == 4 and parts[:2] == ["api", "evaluations"]:
            self._dispatch(lambda: self.server.controller.evaluation(parts[2], parts[3]))
        elif len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[-1] == "grader":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"suite"} or any(len(values) != 1 for values in query.values()):
                self._response(400, {"error": "Invalid grader inspection query"})
                return
            suite = query.get("suite", [None])[0]
            self._dispatch(
                lambda: inspect_grader(self.server.controller.layout, parts[2], parts[3], suite_id=suite)
            )
        elif len(parts) == 8 and parts[:2] == ["api", "runs"] and parts[-1] == "grader":
            if not _SEED.fullmatch(parts[6]):
                self._response(400, {"error": "Run seed must be a canonical non-negative integer"})
                return
            self._dispatch(
                lambda: inspect_run(
                    self.server.controller.layout, parts[2], parts[3], parts[4], parts[5], int(parts[6])
                )
            )
        else:
            self._response(404, {"error": "Unknown local dashboard endpoint"})

    def do_POST(self) -> None:
        if not self._origin() or not self._authorized():
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            self._response(400, {"error": "Dashboard controls require a bounded JSON request body"})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or not 0 < int(length) <= _MAX_REQUEST_BYTES:
            self._response(413, {"error": "Dashboard request body must be between 1 byte and 64 KiB"})
            return
        self.connection.settimeout(15)
        try:
            payload = json.loads(self.rfile.read(int(length)))
        except (ValueError, OSError) as exc:
            self._response(400, {"error": f"Invalid dashboard JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            self._response(400, {"error": "Dashboard request must be a JSON object"})
            return
        parsed = urlsplit(self.path)
        parts = unquote(parsed.path).strip("/").split("/")
        if parsed.query:
            self._response(400, {"error": "Experiment controls do not accept query parameters"})
        elif parts == ["api", "experiments"]:
            self._dispatch(lambda: self.server.controller.create(payload), status=201)
        elif len(parts) == 4 and parts[:2] == ["api", "experiments"] and parts[-1] == "run":
            if payload:
                self._response(
                    400, {"error": "Starting a frozen experiment does not accept settings changes"}
                )
                return
            self._dispatch(lambda: self.server.controller.start(parts[2]), status=202)
        elif len(parts) == 4 and parts[:2] == ["api", "experiments"] and parts[-1] == "cleanup":
            self._dispatch(lambda: self.server.controller.cleanup(parts[2], payload))
        else:
            self._response(404, {"error": "Unknown local dashboard control endpoint"})

    def _dispatch(self, action: Callable[[], Mapping[str, Any]], *, status: int = 200) -> None:
        try:
            self._response(status, action())
        except LockUnavailableError as exc:
            self._response(409, {"error": _text(str(exc), 2000)})
        except (ValueError, InspectionError, TaskVerificationError, GhostlabError) as exc:
            self._response(400, {"error": _text(str(exc), 2000, multiline=True)})
        except OSError as exc:
            LOGGER.exception("Local dashboard operation failed")
            self._response(500, {"error": _text(str(exc), 2000, multiline=True)})


def serve_dashboard(
    layout: Layout,
    output_dir: Path,
    *,
    port: int = 8877,
    agents: Sequence[AgentSpec] = (),
    ghostlab: GhostlabCli | None = None,
    max_attempts: int = 100,
) -> None:
    controller = ExperimentController(
        layout, output_dir, agents=agents, ghostlab=ghostlab, max_attempts=max_attempts
    )
    server = DashboardServer(controller, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        controller.close()
