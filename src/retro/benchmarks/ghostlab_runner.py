"""Run time-consistent benchmark tasks in GhostLab OpenShell sandboxes."""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..storage import Layout
from .time_consistent import (
    PROMPT_LEVELS,
    BenchmarkEvaluationResult,
    evaluate_time_consistent_benchmark,
    load_time_consistent_manifest,
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_TOOLS = ("glob", "grep", "view")
_COPILOT_ENDPOINTS = (
    "api.github.com",
    "api.githubcopilot.com",
    "api.enterprise.githubcopilot.com",
    "proxy.individual.githubcopilot.com",
    "copilot-proxy.githubusercontent.com",
)
_COPILOT_CREDENTIAL_VARS = (
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
_PREDICTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GhostlabBenchmarkRunResult:
    evaluation: BenchmarkEvaluationResult
    task_count: int
    model: str
    prompt_level: str


@dataclass(frozen=True)
class _GhostlabApi:
    runner_config: Any
    create_runner: Callable[[Any, str], Any]
    render_egress_policy: Callable[..., str]
    version: str


@dataclass(frozen=True)
class _Task:
    task_id: str
    instruction: str


def run_ghostlab_benchmark(
    layout: Layout,
    *,
    benchmark_id: str,
    run_id: str,
    model: str,
    prompt_level: str = "contextual",
    condition: str = "baseline",
    workers: int = 2,
    timeout_seconds: int = 600,
    attempts: int = 1,
    reasoning_effort: str = "medium",
    context: str = "default",
    credential_env: str = "COPILOT_GITHUB_TOKEN",
    use_git_credential: bool = False,
    sandbox_image: Path | None = None,
    cpu: str = "2",
    memory: str = "4Gi",
) -> GhostlabBenchmarkRunResult:
    """Run one model in a fresh GhostLab OpenShell sandbox for every task."""
    _validate_identifier(benchmark_id, "benchmark id")
    _validate_identifier(run_id, "run id")
    if not model.strip():
        raise ValueError("model must not be empty")
    if prompt_level not in PROMPT_LEVELS:
        raise ValueError(f"prompt level must be one of: {', '.join(PROMPT_LEVELS)}")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if not _SAFE_ID_RE.fullmatch(condition):
        raise ValueError("condition contains unsupported characters")
    if credential_env not in _COPILOT_CREDENTIAL_VARS:
        raise ValueError(
            "credential_env must be one of: "
            + ", ".join(_COPILOT_CREDENTIAL_VARS)
        )

    api = _load_ghostlab_api()
    benchmark_dir, manifest = load_time_consistent_manifest(layout, benchmark_id)
    repository = Path(_require_mapping(manifest, "repository")["local_path"])
    snapshot_commit = str(_require_mapping(manifest, "repository")["snapshot_commit"])
    tasks = _load_tasks(benchmark_dir, prompt_level, snapshot_commit)
    run_target = layout.benchmark_runs_dir(benchmark_id) / run_id
    if run_target.exists():
        raise FileExistsError(f"benchmark run already exists: {run_target}")

    image = (
        sandbox_image.expanduser().resolve()
        if sandbox_image is not None
        else Path(__file__).with_name("docker") / "copilot-openshell.Dockerfile"
    )
    if not image.is_file():
        raise FileNotFoundError(f"OpenShell sandbox image does not exist: {image}")

    run_parent = layout.benchmark_runs_dir(benchmark_id)
    run_parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix=f".{run_id}.ghostlab.", dir=run_parent))
    private_artifacts = work_root / "runner"
    snapshot = work_root / "snapshot" / "repository"
    private_artifacts.mkdir(parents=True)
    started = time.monotonic()
    try:
        _materialize_snapshot(repository, snapshot_commit, snapshot)
        policy_path = private_artifacts / "openshell-policy.yaml"
        policy_path.write_text(
            api.render_egress_policy(
                list(_COPILOT_ENDPOINTS),
                binaries=["/usr/bin/node"],
            ),
            encoding="utf-8",
        )
        _protect_tree(private_artifacts)
        with _credential(credential_env, use_git_credential) as credential_source:
            outcomes = _run_tasks(
                api=api,
                tasks=tasks,
                snapshot=snapshot,
                private_artifacts=private_artifacts,
                policy_path=policy_path,
                image=image,
                model=model,
                prompt_level=prompt_level,
                condition=condition,
                workers=workers,
                timeout_seconds=timeout_seconds,
                attempts=attempts,
                reasoning_effort=reasoning_effort,
                context=context,
                credential_env=credential_env,
                cpu=cpu,
                memory=memory,
            )
        failures = [outcome for outcome in outcomes if outcome.get("error")]
        copilot_versions = sorted(
            {
                str(outcome["runtime"]["copilot_version"])
                for outcome in outcomes
                if isinstance(outcome.get("runtime"), dict)
                and outcome["runtime"].get("copilot_version")
            }
        )
        runner_protocol = {
            "allowed_tools": list(_ALLOWED_TOOLS),
            "attempts": attempts,
            "backend": "ghostlab-openshell",
            "condition": condition,
            "copilot_versions": copilot_versions,
            "cpu_per_sandbox": cpu,
            "credential_source": credential_source,
            "ghostlab_version": api.version,
            "independent_sandbox_per_task": True,
            "memory_per_sandbox": memory,
            "model": model,
            "network_endpoints": list(_COPILOT_ENDPOINTS),
            "openshell_version": _command_version(["openshell", "--version"]),
            "prompt_level": prompt_level,
            "reasoning_effort": reasoning_effort,
            "repository_snapshot": snapshot_commit,
            "sandbox_image_sha256": _file_sha256(image),
            "task_count": len(tasks),
            "timeout_seconds": timeout_seconds,
            "workers": workers,
        }
        runner_manifest = {
            "completed_at": _utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "failure_count": len(failures),
            "protocol": runner_protocol,
            "schema_version": 1,
        }
        _write_json(private_artifacts / "manifest.json", runner_manifest)
        _protect_tree(private_artifacts)
        if failures:
            failed_path = _publish_failed_attempt(
                run_parent,
                run_id,
                private_artifacts,
            )
            raise RuntimeError(
                f"{len(failures)} sandbox task(s) failed; private diagnostics: {failed_path}"
            )

        predictions = [outcome["prediction"] for outcome in outcomes]
        predictions.sort(key=lambda item: item["task_id"])
        predictions_path = work_root / "predictions.jsonl"
        _write_jsonl(predictions_path, predictions)
        evaluation = evaluate_time_consistent_benchmark(
            layout,
            benchmark_id=benchmark_id,
            predictions_path=predictions_path,
            run_id=run_id,
            runner_protocol=runner_protocol,
            private_artifacts_dir=private_artifacts,
        )
        return GhostlabBenchmarkRunResult(
            evaluation=evaluation,
            task_count=len(tasks),
            model=model,
            prompt_level=prompt_level,
        )
    finally:
        _remove_private_worktree(work_root)


def _run_tasks(
    *,
    api: _GhostlabApi,
    tasks: Sequence[_Task],
    snapshot: Path,
    private_artifacts: Path,
    policy_path: Path,
    image: Path,
    model: str,
    prompt_level: str,
    condition: str,
    workers: int,
    timeout_seconds: int,
    attempts: int,
    reasoning_effort: str,
    context: str,
    credential_env: str,
    cpu: str,
    memory: str,
) -> list[dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _run_task,
                api=api,
                task=task,
                snapshot=snapshot,
                private_artifacts=private_artifacts,
                policy_path=policy_path,
                image=image,
                model=model,
                prompt_level=prompt_level,
                condition=condition,
                timeout_seconds=timeout_seconds,
                attempts=attempts,
                reasoning_effort=reasoning_effort,
                context=context,
                credential_env=credential_env,
                cpu=cpu,
                memory=memory,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                outcomes[task.task_id] = future.result()
            except Exception as exc:
                outcome = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "task_id": task.task_id,
                }
                _write_json(private_artifacts / "tasks" / f"{task.task_id}.json", outcome)
                outcomes[task.task_id] = outcome
    return [outcomes[task.task_id] for task in tasks]


def _run_task(
    *,
    api: _GhostlabApi,
    task: _Task,
    snapshot: Path,
    private_artifacts: Path,
    policy_path: Path,
    image: Path,
    model: str,
    prompt_level: str,
    condition: str,
    timeout_seconds: int,
    attempts: int,
    reasoning_effort: str,
    context: str,
    credential_env: str,
    cpu: str,
    memory: str,
) -> dict[str, Any]:
    task_artifacts = private_artifacts / "tasks" / task.task_id
    task_artifacts.mkdir(parents=True)
    attempt_records: list[dict[str, Any]] = []
    remote_workspace = f"/sandbox/workspace/{snapshot.name}"
    for attempt in range(1, attempts + 1):
        attempt_dir = task_artifacts / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True)
        config = _runner_config(
            api,
            task_id=task.task_id,
            snapshot=snapshot,
            remote_workspace=remote_workspace,
            artifacts=attempt_dir,
            policy_path=policy_path,
            image=image,
            model=model,
            timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort,
            context=context,
            credential_env=credential_env,
            cpu=cpu,
            memory=memory,
        )
        runner = api.create_runner(config, f"retro-{task.task_id}-{attempt}")
        started = time.monotonic()
        close_error = ""
        try:
            copilot_version = _sandbox_copilot_version(runner)
            result = runner.run_turn(_task_prompt(task))
        finally:
            try:
                runner.close()
            except Exception as exc:
                close_error = f"{type(exc).__name__}: {exc}"
        elapsed = round(time.monotonic() - started, 3)
        timed_out = result.timed_out or result.stderr.startswith("sandbox_timeout:")
        parsed = _parse_copilot_stream(result.output)
        record = {
            "attempt": attempt,
            "close_error": close_error,
            "elapsed_seconds": elapsed,
            "exit_code": result.exit_code,
            "model_errors": parsed["errors"],
            "stderr": result.stderr,
            "stdout": result.output,
            "timed_out": timed_out,
            "tool_calls": parsed["tool_calls"],
        }
        attempt_records.append(record)
        _write_json(attempt_dir / "result.json", record)
        disallowed = sorted(
            {
                call["tool"]
                for call in parsed["tool_calls"]
                if call["tool"] not in _ALLOWED_TOOLS
            }
        )
        if (
            result.exit_code != 0
            or timed_out
            or close_error
            or parsed["errors"]
            or disallowed
        ):
            continue
        try:
            response = _parse_prediction(parsed["message"], task.task_id)
        except ValueError:
            continue
        prediction = {
            "condition": condition,
            "metadata": {
                "attempt": attempt,
                "backend": "ghostlab-openshell",
                "elapsed_seconds": elapsed,
                "independent_sandbox": True,
                "tool_calls": parsed["tool_calls"],
            },
            "model": model,
            "predicted_files": response["predicted_files"],
            "prompt_level": prompt_level,
            "schema_version": _PREDICTION_SCHEMA_VERSION,
            "task_id": task.task_id,
        }
        outcome = {
            "attempts": attempt_records,
            "prediction": prediction,
            "runtime": {"copilot_version": copilot_version},
            "task_id": task.task_id,
        }
        _write_json(task_artifacts / "outcome.json", outcome)
        _protect_tree(task_artifacts)
        return outcome
    outcome = {
        "attempts": attempt_records,
        "error": "all sandbox attempts failed",
        "task_id": task.task_id,
    }
    _write_json(task_artifacts / "outcome.json", outcome)
    _protect_tree(task_artifacts)
    return outcome


def _runner_config(
    api: _GhostlabApi,
    *,
    task_id: str,
    snapshot: Path,
    remote_workspace: str,
    artifacts: Path,
    policy_path: Path,
    image: Path,
    model: str,
    timeout_seconds: int,
    reasoning_effort: str,
    context: str,
    credential_env: str,
    cpu: str,
    memory: str,
) -> Any:
    command = _copilot_command(
        model=model,
        remote_workspace=remote_workspace,
        reasoning_effort=reasoning_effort,
        context=context,
        credential_env=credential_env,
    )
    wrapper = f'prompt="$(cat)"; exec {shlex.join(command)} --prompt "$prompt"'
    if "\n" in wrapper or "\r" in wrapper:
        raise ValueError("sandbox command unexpectedly contains a newline")
    sandbox = {
        "artifact_dir": str(artifacts),
        "backend": "openshell",
        "cpu": cpu,
        "env_allowlist": [credential_env],
        "image": str(image),
        "keep": False,
        "memory": memory,
        "name": f"retro-{task_id}",
        "network": "policy",
        "policy": str(policy_path),
        "respect_git_ignore": False,
        "uploads": [{"source": str(snapshot), "target": "/sandbox/workspace"}],
        "workdir": remote_workspace,
    }
    return api.runner_config(
        kind="process",
        command=["/bin/sh", "-c", wrapper],
        env={},
        timeout_seconds=timeout_seconds,
        prompt_mode="stdin",
        parser="text",
        sandbox=sandbox,
    )


def _copilot_command(
    *,
    model: str,
    remote_workspace: str,
    reasoning_effort: str,
    context: str,
    credential_env: str,
) -> list[str]:
    return [
        "copilot",
        "--output-format",
        "json",
        "--stream",
        "off",
        "--no-color",
        "--no-remote",
        "--no-remote-export",
        "--no-auto-update",
        "--no-ask-user",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--disallow-temp-dir",
        "--model",
        model,
        "--effort",
        reasoning_effort,
        "--context",
        context,
        f"--secret-env-vars={credential_env}",
        "-C",
        remote_workspace,
        "--allow-all-tools",
        "--deny-tool=write",
        "--deny-tool=shell",
        "--deny-tool=url",
        f"--available-tools={','.join(_ALLOWED_TOOLS)}",
    ]


def _sandbox_copilot_version(runner: Any) -> str:
    sandbox = getattr(runner, "sandbox", None)
    if sandbox is None or not callable(getattr(sandbox, "exec", None)):
        raise RuntimeError("GhostLab OpenShell runner did not expose its sandbox")
    result = sandbox.exec(
        ["copilot", "--version"],
        input_text=None,
        env={},
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "version probe failed").strip()
        raise RuntimeError(f"could not query sandbox Copilot version: {detail}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("sandbox Copilot version probe returned no output")
    return output.splitlines()[0]


def _task_prompt(task: _Task) -> str:
    return (
        "You are being evaluated only on repository file localization. "
        "The current directory is a fixed repository snapshot.\n\n"
        f"Task ID: {task.task_id}\n"
        f"Task instruction: {task.instruction}\n\n"
        "Inspect only the current directory. Do not use git history, git diff, "
        "the network beyond the model provider, memory, benchmark files, or files "
        "outside this directory. Do not modify anything. Identify the smallest "
        "complete set of repository-relative files that would need to be created "
        "or modified to solve the task. Return exactly one JSON object and nothing "
        f"else: {{\"task_id\":\"{task.task_id}\","
        "\"predicted_files\":[\"path/to/file\"]}}. Use forward slashes and "
        "repository-relative paths."
    )


def _parse_copilot_stream(stream: str) -> dict[str, Any]:
    messages: list[str] = []
    errors: list[str] = []
    calls: dict[str, dict[str, str]] = {}
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "assistant.message" and data.get("content"):
            messages.append(str(data["content"]))
        elif kind == "tool.execution_start":
            call_id = str(data.get("toolCallId") or len(calls))
            calls[call_id] = {
                "status": "started",
                "tool": str(data.get("toolName") or data.get("name") or "?"),
            }
        elif kind == "tool.execution_complete":
            call_id = str(data.get("toolCallId") or len(calls))
            call = calls.setdefault(
                call_id,
                {"status": "unknown", "tool": str(data.get("toolName") or "?")},
            )
            call["status"] = "completed" if data.get("success") else "failed"
        elif kind in ("session.error", "assistant.error"):
            errors.append(str(data.get("message") or data.get("error") or kind))
        elif kind == "result" and int(event.get("exitCode") or 0) != 0:
            errors.append(f"Copilot CLI exited with code {event.get('exitCode')}")
    return {
        "errors": errors,
        "message": messages[-1].strip() if messages else "",
        "tool_calls": list(calls.values()),
    }


def _parse_prediction(message: str, task_id: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    value: dict[str, Any] | None = None
    for index, character in enumerate(message):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(message[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("task_id") == task_id:
            value = candidate
            break
    if value is None:
        raise ValueError("model response did not contain the required prediction object")
    files = value.get("predicted_files")
    if not isinstance(files, list) or not all(
        isinstance(path, str) and path.strip() for path in files
    ):
        raise ValueError("predicted_files must contain non-empty strings")
    return {
        "predicted_files": sorted({path.strip() for path in files}),
        "task_id": task_id,
    }


def _materialize_snapshot(repository: Path, commit: str, destination: Path) -> None:
    repository = repository.expanduser().resolve()
    if not repository.is_dir():
        raise FileNotFoundError(f"benchmark repository does not exist: {repository}")
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", "--format=tar", commit],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        detail = archive.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not archive benchmark snapshot {commit}: {detail}")
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as handle:
        members = handle.getmembers()
        for member in members:
            archive_path = PurePosixPath(member.name)
            if archive_path.is_absolute() or ".." in archive_path.parts:
                raise ValueError(f"unsafe path in repository snapshot: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(
                    f"repository snapshot contains unsupported link or device: {member.name}"
                )
        handle.extractall(destination, members=members)
    for filesystem_path in sorted(destination.rglob("*"), reverse=True):
        filesystem_path.chmod(0o555 if filesystem_path.is_dir() else 0o444)
    destination.chmod(0o555)


def _load_tasks(
    benchmark_dir: Path,
    prompt_level: str,
    snapshot_commit: str,
) -> list[_Task]:
    path = benchmark_dir / "tasks" / "prompts" / f"{prompt_level}.jsonl"
    records = _read_jsonl(path)
    tasks: list[_Task] = []
    seen: set[str] = set()
    for record in records:
        task_id = record.get("task_id")
        instruction = record.get("instruction")
        if not isinstance(task_id, str) or not _SAFE_ID_RE.fullmatch(task_id):
            raise ValueError(f"invalid task id in {path}")
        if task_id in seen:
            raise ValueError(f"duplicate task id in {path}: {task_id}")
        if record.get("prompt_level") != prompt_level:
            raise ValueError(f"mismatched prompt level for task {task_id}")
        if record.get("snapshot_commit") != snapshot_commit:
            raise ValueError(f"mismatched repository snapshot for task {task_id}")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"empty instruction for task {task_id}")
        seen.add(task_id)
        tasks.append(_Task(task_id=task_id, instruction=instruction))
    if not tasks:
        raise ValueError(f"benchmark contains no {prompt_level} tasks")
    return tasks


def _load_ghostlab_api() -> _GhostlabApi:
    if sys.version_info < (3, 10):
        raise RuntimeError(
            "GhostLab requires Python 3.10 or newer. Run this command from a "
            "Python 3.10+ environment installed with `retro-ai[sandbox]`."
        )
    try:
        config = importlib.import_module("rehearsal.config")
        runners = importlib.import_module("rehearsal.runners")
        agent_sandbox = importlib.import_module("rehearsal.agent_sandbox")
    except ImportError as exc:
        raise RuntimeError(
            "GhostLab is not installed. Install the sandbox extra with "
            "`pip install 'retro-ai[sandbox]'`."
        ) from exc
    try:
        version = importlib.metadata.version("ghostlab")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return _GhostlabApi(
        runner_config=config.RunnerConfig,
        create_runner=runners.create_runner,
        render_egress_policy=agent_sandbox.render_egress_policy,
        version=version,
    )


@contextmanager
def _credential(name: str, use_git_credential: bool) -> Iterator[str]:
    previous = os.environ.get(name)
    source = f"environment:{name}"
    if previous:
        yield source
        return
    if not use_git_credential:
        raise RuntimeError(
            f"{name} is not set. Export a GitHub user token with Copilot access, "
            "or pass --use-git-credential to request one from Git's credential helper."
        )
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    token = values.get("password")
    if result.returncode != 0 or not token:
        raise RuntimeError("Git's credential helper returned no GitHub token")
    os.environ[name] = token
    try:
        yield "git-credential:github.com"
    finally:
        os.environ.pop(name, None)


def _publish_failed_attempt(
    run_parent: Path,
    run_id: str,
    private_artifacts: Path,
) -> Path:
    failed_parent = run_parent / "failed"
    failed_parent.mkdir(parents=True, exist_ok=True)
    failed_parent.chmod(0o700)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = failed_parent / f"{run_id}-{suffix}"
    shutil.copytree(private_artifacts, target)
    _protect_tree(target)
    return target


def _protect_tree(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o700)
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)


def _remove_private_worktree(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    root.chmod(0o700)
    shutil.rmtree(root)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            records.append(value)
    return records


def _require_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ValueError(f"benchmark manifest is missing {key!r}")
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else "unknown"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_identifier(value: str, label: str) -> None:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
