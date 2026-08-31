"""Candidate-agent evaluation for published benchmark tasks.

Implements spec section 15: verify the published task, materialize a fresh base
repository for every attempt, run the agent under test once through
``ghostlab artifact-run``, score the exported state through
``ghostlab scorer-run``, and write one immutable, hash-addressed ``attempt.json``.

Agent, harness, and scorer failures receive distinct statuses and are never
converted into a numeric zero.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aggregate import (
    ATTEMPT_SCHEMA,
    SCORED_STATUS,
    AttemptRecord,
    BenchmarkAggregate,
    aggregate_attempts,
    iter_status_rows,
    load_attempts,
    validate_attempt_record,
    validate_score_report,
    write_aggregate,
)
from .build import (
    BENCHMARK_TASK_SCHEMA,
    TasksetPaths,
    compute_scorer_package_hash,
    list_published_tasks,
    resolve_taskset_paths,
    unpack_bundle,
    utc_now,
)
from .ghostlab_cli import (
    SCORER_RUN_REPORT_NAME,
    ArtifactRunRequest,
    GhostlabBinaryError,
    GhostlabCli,
    GhostlabContractError,
    GhostlabError,
    GhostlabInvocationError,
    GhostlabTimeoutError,
    ScorerRunRequest,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
    sha256_path,
    validate_scorer_run_attestation,
    write_json,
)
from .schema import BenchmarkTask, ProjectEnvironment, SchemaError, ScorerManifest

CANDIDATE_EXPORT_NAME = "candidate-state.tar.zst"
RESOURCE_USAGE_SCHEMA = "retro-attempt-resources-v1"

#: ``artifact-run`` status -> attempt status. ``None`` means "keep going".
ARTIFACT_STATUS_TO_ATTEMPT: Mapping[str, str | None] = {
    "completed": None,
    "timed_out": "agent_timeout",
    "timeout": "agent_timeout",
    "agent_error": "agent_error",
    "model_unavailable": "model_unavailable",
    "export_failed": "harness_error",
    "output_contract_failed": "harness_error",
    "contract_violation": "harness_error",
    "sandbox_error": "harness_error",
    "harness_error": "harness_error",
}


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class TaskVerificationError(RuntimeError):
    """A published task failed its integrity checks before any model ran."""


def _validate_safe_id(value: str, label: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise TaskVerificationError(f"{label} {value!r} contains unsupported characters")
    return value


def _referenced_agent_paths(payload: Mapping[str, Any]) -> tuple[str, ...]:
    references: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str) and value:
            references.append(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        collect(runtime.get("instructions"))
        skills = runtime.get("skills")
        if isinstance(skills, Mapping):
            collect(skills.get("paths"))
        agents = runtime.get("agents")
        if isinstance(agents, Mapping):
            for definition in agents.values():
                if not isinstance(definition, Mapping):
                    continue
                prompt = definition.get("prompt")
                if (
                    isinstance(prompt, str)
                    and Path(prompt).suffix in {".md", ".txt"}
                ):
                    collect(prompt)
    inputs = payload.get("inputs")
    if isinstance(inputs, Mapping):
        for item in inputs.get("skills") or []:
            if isinstance(item, str):
                collect(item)
            elif isinstance(item, Mapping):
                collect(item.get("path"))
        for item in inputs.get("assets") or []:
            if isinstance(item, str):
                collect(item)
            elif isinstance(item, Mapping):
                collect(item.get("source"))
        for item in inputs.get("mcps") or []:
            if not isinstance(item, Mapping):
                continue
            collect(item.get("config_ref"))
            if item.get("config_ref") or item.get("transport") != "stdio":
                continue
            connection = item.get("connection")
            if not isinstance(connection, Mapping):
                continue
            command = connection.get("command")
            parts = (
                [command]
                if isinstance(command, str)
                else list(command) if isinstance(command, list) else []
            )
            args = connection.get("args")
            if isinstance(args, list):
                parts.extend(args)
            for value in parts:
                if isinstance(value, str) and value.startswith(("/", "~")):
                    collect(value)
    sandbox = payload.get("sandbox")
    if isinstance(sandbox, Mapping):
        collect(sandbox.get("image"))
        collect(sandbox.get("policy"))
        uploads = sandbox.get("uploads")
        if isinstance(uploads, list):
            for upload in uploads:
                if isinstance(upload, Mapping):
                    collect(upload.get("source"))
    return tuple(dict.fromkeys(references))


def _resolve_agent_reference(config_path: Path, reference: str) -> Path | None:
    candidate = Path(reference).expanduser()
    candidates = (
        (candidate,)
        if candidate.is_absolute()
        else (
            config_path.parent / candidate,
            Path.cwd() / candidate,
            Path(__file__).resolve().parent / "instructions" / candidate.name,
        )
    )
    return next((item.resolve() for item in candidates if item.exists()), None)


def _directory_upload_references(payload: Mapping[str, Any]) -> set[str]:
    references: set[str] = set()
    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        for value in runtime.get("instructions") or []:
            if isinstance(value, str):
                references.add(value)
        skills = runtime.get("skills")
        if isinstance(skills, Mapping):
            for value in skills.get("paths") or []:
                if isinstance(value, str):
                    references.add(value)
        agents = runtime.get("agents")
        if isinstance(agents, Mapping):
            for definition in agents.values():
                if not isinstance(definition, Mapping):
                    continue
                prompt = definition.get("prompt")
                if isinstance(prompt, str) and Path(prompt).suffix in {".md", ".txt"}:
                    references.add(prompt)
    inputs = payload.get("inputs")
    if isinstance(inputs, Mapping):
        for item in inputs.get("skills") or []:
            if isinstance(item, str):
                references.add(item)
            elif isinstance(item, Mapping) and isinstance(item.get("path"), str):
                references.add(item["path"])
    return references


def _mcp_config_references(payload: Mapping[str, Any]) -> set[str]:
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        return set()
    return {
        str(item["config_ref"])
        for item in inputs.get("mcps") or []
        if isinstance(item, Mapping)
        and isinstance(item.get("config_ref"), str)
        and item["config_ref"]
    }


def _mcp_servers_for_reference(
    payload: Mapping[str, Any], reference: str
) -> tuple[str | None, ...]:
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        return ()
    selectors: list[str | None] = []
    for item in inputs.get("mcps") or []:
        if not isinstance(item, Mapping) or item.get("config_ref") != reference:
            continue
        server = item.get("server")
        selectors.append(server if isinstance(server, str) and server else None)
    return tuple(dict.fromkeys(selectors))


def _inline_mcp_program_references(payload: Mapping[str, Any]) -> set[str]:
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        return set()
    references: set[str] = set()
    for item in inputs.get("mcps") or []:
        if (
            not isinstance(item, Mapping)
            or item.get("config_ref")
            or item.get("transport") != "stdio"
        ):
            continue
        connection = item.get("connection")
        if not isinstance(connection, Mapping):
            continue
        command = connection.get("command")
        parts = (
            [command]
            if isinstance(command, str)
            else list(command) if isinstance(command, list) else []
        )
        args = connection.get("args")
        if isinstance(args, list):
            parts.extend(args)
        for value in parts:
            if isinstance(value, str) and value.startswith(("/", "~")):
                references.add(value)
    return references


_PROJECT_MARKERS = (
    "package.json",
    "node_modules",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    ".venv",
    "venv",
    "go.mod",
    "Cargo.toml",
    "deno.json",
)
_INTERPRETERS = {
    "node",
    "nodejs",
    "npx",
    "bun",
    "deno",
    "python",
    "python2",
    "python3",
    "uv",
    "uvx",
    "ruby",
    "sh",
    "bash",
    "zsh",
    "env",
    "java",
    "dotnet",
}
_FORBIDDEN_AUTO_UPLOAD_ROOTS = {Path("/"), Path.home().resolve()}


def _program_root(path: Path) -> Path:
    start = path.parent if path.is_file() else path
    candidate = start.resolve()
    stop = {Path("/"), Path.home().resolve(), Path("/tmp"), Path("/private/tmp")}
    for _ in range(2):
        if candidate in stop or candidate.parent == candidate:
            break
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate
        candidate = candidate.parent
    return start.resolve()


def _is_interpreter_path(path: Path) -> bool:
    stem = path.name.lower()
    if stem in _INTERPRETERS:
        return True
    return any(
        stem.startswith(name)
        and stem[len(name) :].replace(".", "").rstrip("m").isdigit()
        for name in ("python", "node")
    )


def _resolved_config_mcp_path(config_parent: Path, value: str) -> Path | None:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    sibling = config_parent / candidate
    return sibling.resolve() if sibling.is_file() else None


def _selected_mcp_connections(
    document: dict[str, Any],
    servers: tuple[str | None, ...],
    *,
    config_path: Path,
) -> list[dict[str, Any]]:
    configured = document.get("mcpServers")
    if isinstance(configured, dict):
        selected: list[dict[str, Any]] = []
        for server in servers or (None,):
            if server is None:
                if len(configured) != 1:
                    raise TaskVerificationError(
                        f"MCP config {config_path} requires an explicit server selector"
                    )
                server = next(iter(configured))
            entry = configured.get(server)
            if not isinstance(entry, dict):
                raise TaskVerificationError(
                    f"MCP config {config_path} has no server {server!r}"
                )
            if entry.get("command"):
                selected.append(entry)
        return selected
    if document.get("transport") != "stdio":
        return []
    connection = document.get("connection")
    if not isinstance(connection, dict):
        raise TaskVerificationError(
            f"MCP config {config_path} has no stdio connection object"
        )
    return [connection]


def _mcp_dependency_roots(
    config_path: Path,
    servers: tuple[str | None, ...],
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskVerificationError(
            f"MCP config {config_path} must be valid JSON for reproducible evaluation: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise TaskVerificationError(f"MCP config {config_path} is not a JSON object")
    roots: list[Path] = []
    for connection in _selected_mcp_connections(
        document, servers, config_path=config_path
    ):
        command = connection.get("command")
        parts = (
            [command]
            if isinstance(command, str)
            else list(command) if isinstance(command, list) else []
        )
        args = connection.get("args")
        if isinstance(args, list):
            parts.extend(args)
        for value in parts:
            if not isinstance(value, str) or not value or "://" in value:
                continue
            candidate = _resolved_config_mcp_path(config_path.parent, value)
            if candidate is None:
                continue
            if candidate.is_file() and _is_interpreter_path(candidate):
                continue
            root = _program_root(candidate)
            if root in _FORBIDDEN_AUTO_UPLOAD_ROOTS:
                continue
            if root not in roots:
                roots.append(root)
    return document, tuple(roots)


def _assert_no_escaping_symlinks(source: Path) -> None:
    root = source.resolve()
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        if Path(os.readlink(path)).is_absolute():
            raise TaskVerificationError(
                f"agent runtime asset {source} contains absolute symlink {path}"
            )
        target = path.resolve()
        if target != root and root not in target.parents:
            raise TaskVerificationError(
                f"agent runtime asset {source} contains escaping symlink {path}"
            )


def _mcp_asset_hash(
    config_path: Path, servers: tuple[str | None, ...]
) -> str:
    _document, roots = _mcp_dependency_roots(config_path, servers)
    entries: list[dict[str, Any]] = [
        {"kind": "config", "sha256": sha256_file(config_path)}
    ]
    for index, root in enumerate(roots):
        _assert_no_escaping_symlinks(root)
        entries.append(
            {
                "kind": "dependency",
                "index": index,
                "sha256": sha256_path(root, excludes=()),
            }
        )
    return sha256_json(entries)


def _reference_source(
    config_path: Path,
    payload: Mapping[str, Any],
    reference: str,
) -> tuple[Path, Path] | None:
    resolved = _resolve_agent_reference(config_path, reference)
    if resolved is None:
        return None
    if reference in _inline_mcp_program_references(payload):
        if resolved.is_file() and _is_interpreter_path(resolved):
            return None
        root = _program_root(resolved)
        if root in _FORBIDDEN_AUTO_UPLOAD_ROOTS:
            return None
        return root, resolved.relative_to(root)
    if reference in _directory_upload_references(payload) and resolved.is_file():
        return resolved.parent, Path(resolved.name)
    return resolved, Path(".")


def _rewrite_agent_path_fields(
    payload: dict[str, Any], replacements: Mapping[str, str]
) -> dict[str, Any]:
    inline_mcp_paths = _inline_mcp_program_references(payload)

    def replaced(value: Any) -> Any:
        return replacements.get(value, value) if isinstance(value, str) else value

    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        instructions = runtime.get("instructions")
        if isinstance(instructions, list):
            runtime["instructions"] = [replaced(item) for item in instructions]
        skills = runtime.get("skills")
        if isinstance(skills, dict) and isinstance(skills.get("paths"), list):
            skills["paths"] = [replaced(item) for item in skills["paths"]]
        agents = runtime.get("agents")
        if isinstance(agents, dict):
            for definition in agents.values():
                if isinstance(definition, dict) and isinstance(
                    definition.get("prompt"), str
                ) and Path(definition["prompt"]).suffix in {".md", ".txt"}:
                    definition["prompt"] = replaced(definition["prompt"])

    inputs = payload.get("inputs")
    if isinstance(inputs, dict):
        skills = inputs.get("skills")
        if isinstance(skills, list):
            for index, item in enumerate(skills):
                if isinstance(item, str):
                    skills[index] = replaced(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    item["path"] = replaced(item["path"])
        assets = inputs.get("assets")
        if isinstance(assets, list):
            for index, item in enumerate(assets):
                if isinstance(item, str):
                    assets[index] = replaced(item)
                elif isinstance(item, dict) and isinstance(item.get("source"), str):
                    item["source"] = replaced(item["source"])
        mcps = inputs.get("mcps")
        if isinstance(mcps, list):
            for item in mcps:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("config_ref"), str):
                    item["config_ref"] = replaced(item["config_ref"])
                    continue
                if item.get("transport") != "stdio":
                    continue
                connection = item.get("connection")
                if not isinstance(connection, dict):
                    continue
                command = connection.get("command")
                if isinstance(command, str):
                    if command in inline_mcp_paths:
                        connection["command"] = replaced(command)
                elif isinstance(command, list):
                    connection["command"] = [
                        replaced(part)
                        if isinstance(part, str) and part in inline_mcp_paths
                        else part
                        for part in command
                    ]
                args = connection.get("args")
                if isinstance(args, list):
                    connection["args"] = [
                        replaced(part)
                        if isinstance(part, str) and part in inline_mcp_paths
                        else part
                        for part in args
                    ]

    sandbox = payload.get("sandbox")
    if isinstance(sandbox, dict):
        for key in ("image", "policy"):
            if isinstance(sandbox.get(key), str):
                sandbox[key] = replaced(sandbox[key])
        uploads = sandbox.get("uploads")
        if isinstance(uploads, list):
            for upload in uploads:
                if isinstance(upload, dict) and isinstance(upload.get("source"), str):
                    upload["source"] = replaced(upload["source"])
    return payload


def _agent_asset_hashes(
    path: Path, payload: Mapping[str, Any] | None = None
) -> dict[str, str]:
    if payload is None:
        loaded = read_json(path, label="candidate agent config")
        if not isinstance(loaded, Mapping):
            raise TaskVerificationError(f"agent config {path} is not a JSON object")
        payload = loaded
    hashes: dict[str, str] = {}
    sandbox = payload.get("sandbox")
    image_reference = (
        sandbox.get("image") if isinstance(sandbox, Mapping) else None
    )
    mcp_references = _mcp_config_references(payload)
    inline_mcp_references = _inline_mcp_program_references(payload)
    for reference in _referenced_agent_paths(payload):
        if reference in inline_mcp_references:
            resolved_program = _resolve_agent_reference(path, reference)
            if (
                resolved_program is not None
                and (
                    (
                        resolved_program.is_file()
                        and _is_interpreter_path(resolved_program)
                    )
                    or _program_root(resolved_program)
                    in _FORBIDDEN_AUTO_UPLOAD_ROOTS
                )
            ):
                continue
        if reference in mcp_references:
            resolved = _resolve_agent_reference(path, reference)
            hashes[reference] = (
                _mcp_asset_hash(
                    resolved,
                    _mcp_servers_for_reference(payload, reference),
                )
                if resolved is not None
                else "<missing>"
            )
            continue
        source = _reference_source(path, payload, reference)
        hash_target = source[0] if source is not None else None
        if (
            reference == image_reference
            and hash_target is not None
            and hash_target.is_file()
        ):
            hash_target = hash_target.parent
        if hash_target is not None:
            _assert_no_escaping_symlinks(hash_target)
        hashes[reference] = (
            sha256_path(hash_target, excludes=())
            if hash_target is not None
            else "<missing>"
        )
    return hashes


def parse_seeds(value: str | Sequence[int]) -> tuple[int, ...]:
    """Parse ``--seeds 0,1,2`` into a de-duplicated, ordered tuple."""
    if not isinstance(value, str):
        return tuple(dict.fromkeys(int(item) for item in value))
    seeds: list[int] = []
    for chunk in value.split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            seeds.append(int(text))
        except ValueError as exc:
            raise TaskVerificationError(f"seed {text!r} is not an integer") from exc
    if not seeds:
        raise TaskVerificationError("at least one seed is required")
    return tuple(dict.fromkeys(seeds))


def default_eval_id(now: datetime | None = None) -> str:
    """Timestamped eval id used when the caller does not supply one."""
    moment = now or datetime.now(timezone.utc)
    return "eval-" + moment.strftime("%Y%m%dT%H%M%SZ")


def unique_eval_id(paths: TasksetPaths) -> str:
    """A timestamped eval id that does not collide with an existing directory."""
    base = default_eval_id()
    existing = set(list_evals(paths))
    if base not in existing:
        return base
    for index in range(2, 1000):
        candidate = f"{base}-{index}"
        if candidate not in existing:
            return candidate
    raise TaskVerificationError(f"could not allocate a fresh eval id beside {base!r}")


def list_evals(paths: TasksetPaths) -> list[str]:
    root = paths.eval_dir("probe").parent
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def resolve_eval_id(paths: TasksetPaths, requested: str | None) -> str:
    """Resolve ``--eval latest`` (or a missing value) against existing evals."""
    if requested and requested != "latest":
        if not _SAFE_ID_RE.fullmatch(requested):
            raise TaskVerificationError(f"eval id {requested!r} contains unsupported characters")
        return requested
    existing = list_evals(paths)
    if not existing:
        raise TaskVerificationError(
            f"no evals exist under {paths.eval_dir('').parent}; run the taskset first"
        )
    return existing[-1]


@dataclass(frozen=True)
class AgentSpec:
    """One candidate agent configuration under evaluation."""

    agent_id: str
    config_path: Path
    config_sha256: str
    expected_sha256: str | None = None

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        agent_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> AgentSpec:
        if not path.is_file():
            raise TaskVerificationError(f"agent config {path} does not exist")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TaskVerificationError(f"agent config {path} is invalid: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise TaskVerificationError(f"agent config {path} is not a JSON object")
        resolved = agent_id
        if resolved is None:
            declared = payload.get("id")
            if isinstance(declared, str) and declared:
                resolved = declared
        if resolved is None:
            resolved = path.stem
        resolved = _validate_safe_id(resolved, "agent id")
        return cls(
            agent_id=resolved,
            config_path=path,
            config_sha256=sha256_bytes(raw),
            expected_sha256=expected_sha256,
        )

    def _payload(self) -> dict[str, Any]:
        try:
            raw = self.config_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TaskVerificationError(
                f"agent config {self.config_path} is invalid: {exc}"
            ) from exc
        actual = sha256_bytes(raw)
        if actual != self.config_sha256:
            raise TaskVerificationError(
                f"agent config {self.config_path} changed after it was loaded: "
                f"{self.config_sha256} -> {actual}"
            )
        if not isinstance(payload, dict):
            raise TaskVerificationError(
                f"agent config {self.config_path} is not a JSON object"
            )
        return payload

    def verify(self) -> None:
        _validate_safe_id(self.agent_id, "agent id")
        self._payload()
        if self.expected_sha256 and self.expected_sha256 != self.config_sha256:
            raise TaskVerificationError(
                f"agent config {self.config_path} hashes to {self.config_sha256}, "
                f"expected {self.expected_sha256}"
            )

    def referenced_asset_hashes(self) -> dict[str, str]:
        return _agent_asset_hashes(self.config_path, self._payload())

    def _local_sandbox_image(
        self, task_image: str
    ) -> tuple[dict[str, Any], Path, Path] | None:
        payload = self._payload()
        sandbox = payload.get("sandbox") if isinstance(payload, Mapping) else None
        declared = sandbox.get("image") if isinstance(sandbox, Mapping) else None
        if not isinstance(declared, str) or not declared:
            return None
        candidate = _resolve_agent_reference(self.config_path, declared)
        if candidate is None:
            return None
        dockerfile = candidate / "Dockerfile" if candidate.is_dir() else candidate
        if not dockerfile.is_file():
            raise TaskVerificationError(
                f"agent sandbox image {declared!r} has no Dockerfile"
            )
        if dockerfile.stat().st_size > 1_000_000:
            raise TaskVerificationError(
                f"agent sandbox Dockerfile {dockerfile} exceeds the size limit"
            )
        base_images = re.findall(
            r"(?im)^\s*FROM\s+([^\s]+)",
            dockerfile.read_text(encoding="utf-8"),
        )
        if base_images != [task_image]:
            raise TaskVerificationError(
                "agent sandbox Dockerfile must be single-stage and start FROM "
                f"the task image {task_image!r}"
            )
        return copy.deepcopy(dict(payload)), dockerfile.parent, dockerfile

    def sandbox_image_override(self, task_image: str) -> str | None:
        return None if self._local_sandbox_image(task_image) is not None else task_image

    def execution_config(
        self,
        task_image: str,
        destination: Path,
        *,
        expected_asset_hashes: Mapping[str, str],
    ) -> tuple[Path, str | None]:
        payload = self._payload()
        resolved_payload: dict[str, Any] = copy.deepcopy(dict(payload))
        local_image = self._local_sandbox_image(task_image)
        sandbox = resolved_payload.get("sandbox")
        uploads = sandbox.get("uploads") if isinstance(sandbox, dict) else None

        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)

        def snapshot(source: Path, target: Path, expected: str) -> None:
            source = source.resolve()
            target = target.resolve()
            if (
                source == target
                or source in target.parents
                or target in source.parents
            ):
                raise TaskVerificationError(
                    f"agent runtime snapshot {target} overlaps source {source}"
                )
            _assert_no_escaping_symlinks(source)
            before = sha256_path(source, excludes=())
            if before != expected:
                raise TaskVerificationError(
                    f"agent runtime asset {source} changed after attempt fingerprinting"
                )
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
            else:
                raise TaskVerificationError(
                    f"agent runtime asset {source} is not a regular file or directory"
                )
            if (
                sha256_path(source, excludes=()) != expected
                or sha256_path(target, excludes=()) != expected
            ):
                raise TaskVerificationError(
                    f"agent runtime asset {source} changed while it was snapshotted"
                )

        sandbox_payload = payload.get("sandbox")
        image_reference = (
            sandbox_payload.get("image")
            if isinstance(sandbox_payload, Mapping)
            else None
        )
        upload_references = {
            str(upload["source"])
            for upload in uploads or []
            if isinstance(upload, Mapping)
            and isinstance(upload.get("source"), str)
            and upload["source"]
        }
        replacements: dict[str, str] = {}
        mcp_references = _mcp_config_references(payload)
        reference_index = 0
        for reference in _referenced_agent_paths(payload):
            if reference == image_reference or reference in upload_references:
                continue
            if reference in mcp_references:
                servers = _mcp_servers_for_reference(payload, reference)
                config_source = _resolve_agent_reference(
                    self.config_path, reference
                )
                expected = expected_asset_hashes.get(reference)
                if (
                    config_source is None
                    or not isinstance(expected, str)
                    or expected == "<missing>"
                ):
                    raise TaskVerificationError(
                        f"MCP config {reference!r} is missing or unhashed"
                    )
                if _mcp_asset_hash(config_source, servers) != expected:
                    raise TaskVerificationError(
                        f"MCP config {config_source} changed after attempt fingerprinting"
                    )
                document, dependency_roots = _mcp_dependency_roots(
                    config_source, servers
                )
                config_parent = config_source.parent
                mcp_snapshot = destination / "mcps" / str(reference_index)
                reference_index += 1
                config_snapshot = mcp_snapshot / config_source.name
                config_snapshot.parent.mkdir(parents=True)
                shutil.copy2(config_source, config_snapshot)
                root_targets: list[tuple[Path, Path]] = []
                for root_index, root in enumerate(dependency_roots):
                    target_root = (
                        mcp_snapshot
                        / "dependencies"
                        / str(root_index)
                        / root.name
                    )
                    snapshot(
                        root,
                        target_root,
                        sha256_path(root, excludes=()),
                    )
                    root_targets.append((root, target_root))

                def rewrite_executable(
                    value: str,
                    *,
                    config_parent: Path = config_parent,
                    targets: tuple[tuple[Path, Path], ...] = tuple(root_targets),
                ) -> str:
                    if value and "://" not in value:
                        resolved = _resolved_config_mcp_path(
                            config_parent, value
                        )
                        if resolved is not None:
                            for root, target_root in targets:
                                if resolved == root or root in resolved.parents:
                                    return str(target_root / resolved.relative_to(root))
                    return value

                snapshotted_document = copy.deepcopy(document)
                for connection in _selected_mcp_connections(
                    snapshotted_document,
                    servers,
                    config_path=config_source,
                ):
                    command = connection.get("command")
                    if isinstance(command, str):
                        connection["command"] = rewrite_executable(command)
                    elif isinstance(command, list):
                        connection["command"] = [
                            rewrite_executable(item)
                            if isinstance(item, str)
                            else item
                            for item in command
                        ]
                    args = connection.get("args")
                    if isinstance(args, list):
                        connection["args"] = [
                            rewrite_executable(item)
                            if isinstance(item, str)
                            else item
                            for item in args
                        ]
                config_snapshot.write_text(
                    json.dumps(snapshotted_document, indent=2) + "\n",
                    encoding="utf-8",
                )
                if _mcp_asset_hash(config_source, servers) != expected:
                    raise TaskVerificationError(
                        f"MCP config {config_source} changed while it was snapshotted"
                    )
                replacements[reference] = str(config_snapshot)
                continue
            source_info = _reference_source(self.config_path, payload, reference)
            expected = expected_asset_hashes.get(reference)
            if source_info is None or expected in (None, "<missing>"):
                continue
            source, relative = source_info
            target_root = (
                destination
                / "references"
                / str(reference_index)
                / source.name
            )
            reference_index += 1
            snapshot(source, target_root, str(expected))
            target = target_root if relative == Path(".") else target_root / relative
            replacements[reference] = str(target)

        resolved_payload = _rewrite_agent_path_fields(
            resolved_payload, replacements
        )
        image_override: str | None = task_image
        if local_image is not None:
            _payload, context, dockerfile = local_image
            declared = str(payload["sandbox"]["image"])
            expected = expected_asset_hashes.get(declared)
            if not isinstance(expected, str) or expected == "<missing>":
                raise TaskVerificationError(
                    "agent sandbox image was not bound to the attempt fingerprint"
                )
            image_snapshot = destination / "image-context"
            snapshot(context, image_snapshot, expected)
            resolved_payload["sandbox"]["image"] = str(
                image_snapshot / dockerfile.relative_to(context)
            )
            image_override = None

        sandbox = resolved_payload.get("sandbox")
        rewritten_uploads = (
            sandbox.get("uploads") if isinstance(sandbox, dict) else None
        )
        if isinstance(uploads, list):
            snapshotted_uploads: list[Any] = []
            for index, upload in enumerate(rewritten_uploads or uploads):
                if not isinstance(upload, Mapping):
                    snapshotted_uploads.append(upload)
                    continue
                copied = dict(upload)
                upload_reference = upload.get("source")
                if not isinstance(upload_reference, str) or not upload_reference:
                    snapshotted_uploads.append(copied)
                    continue
                upload_source_info = _reference_source(
                    self.config_path,
                    payload,
                    upload_reference,
                )
                expected = expected_asset_hashes.get(upload_reference)
                if (
                    upload_source_info is None
                    or not isinstance(expected, str)
                    or expected == "<missing>"
                ):
                    raise TaskVerificationError(
                        f"agent upload source {upload_reference!r} is missing or unhashed"
                    )
                upload_source, upload_relative = upload_source_info
                target = (
                    destination / "uploads" / str(index) / upload_source.name
                )
                snapshot(upload_source, target, expected)
                copied["source"] = str(
                    target
                    if upload_relative == Path(".")
                    else target / upload_relative
                )
                replacements[upload_reference] = copied["source"]
                snapshotted_uploads.append(copied)
            resolved_payload["sandbox"]["uploads"] = snapshotted_uploads
            resolved_payload = _rewrite_agent_path_fields(
                resolved_payload, replacements
            )

        if self.referenced_asset_hashes() != dict(expected_asset_hashes):
            raise TaskVerificationError(
                "agent referenced assets changed while preparing execution"
            )
        config_path = destination / "agent.json"
        write_json(config_path, resolved_payload)
        return config_path, image_override


@dataclass(frozen=True)
class PublishedTask:
    """The verified public/private material for one benchmark task."""

    task_id: str
    task_dir: Path
    public_task: Mapping[str, Any]
    public_task_path: Path
    prompt_path: Path
    public_environment: Mapping[str, Any]
    public_environment_path: Path
    base_bundle: Path
    scorer_manifest: Mapping[str, Any]
    scorer_manifest_path: Path
    provenance: Mapping[str, Any]
    public_task_sha256: str
    prompt_sha256: str
    public_environment_sha256: str
    base_bundle_sha256: str
    scorer_package_sha256: str

    @property
    def source_id(self) -> str | None:
        value = self.provenance.get("source_id")
        return value if isinstance(value, str) else None

    @property
    def pass_threshold(self) -> float:
        scoring = self.public_task.get("scoring")
        if isinstance(scoring, Mapping):
            value = scoring.get("pass_threshold")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return 0.8

    @property
    def wall_time_seconds(self) -> int:
        limits = self.public_task.get("limits")
        if isinstance(limits, Mapping):
            value = limits.get("wall_time_seconds")
            if isinstance(value, int):
                return value
        return 1800

    @property
    def environment_image(self) -> str:
        image = self.public_environment.get("image")
        if isinstance(image, str) and image:
            return image
        raise TaskVerificationError(f"task {self.task_id} has no pinned environment image")

    @property
    def setup_commands(self) -> tuple[tuple[str, ...], ...]:
        commands = self.public_environment.get("setup")
        if not isinstance(commands, list):
            raise TaskVerificationError(f"task {self.task_id} has invalid environment setup")
        normalized: list[tuple[str, ...]] = []
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                raise TaskVerificationError(
                    f"task {self.task_id} has an invalid environment setup command"
                )
            normalized.append(tuple(command))
        return tuple(normalized)

    @property
    def scorer_timeout_seconds(self) -> float:
        runtime = self.scorer_manifest.get("runtime")
        if isinstance(runtime, Mapping):
            value = runtime.get("timeout_seconds")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return float(value)
        raise TaskVerificationError(f"task {self.task_id} has no valid scorer timeout")


def verify_published_task(paths: TasksetPaths, task_id: str) -> PublishedTask:
    """Check every published artifact hash before an attempt starts."""
    _validate_safe_id(task_id, "task id")
    task_dir = paths.task_dir(task_id)
    public_task_path = task_dir / "public" / "task.json"
    prompt_path = task_dir / "public" / "prompt.txt"
    public_environment_path = task_dir / "public" / "environment.json"
    base_bundle = task_dir / "public" / "base.bundle"
    scorer_manifest = task_dir / "private" / "scorer" / "scorer.json"
    provenance_path = task_dir / "private" / "provenance.json"
    for path, label in (
        (public_task_path, "public/task.json"),
        (prompt_path, "public/prompt.txt"),
        (public_environment_path, "public/environment.json"),
        (base_bundle, "public/base.bundle"),
        (scorer_manifest, "private/scorer/scorer.json"),
        (provenance_path, "private/provenance.json"),
    ):
        if not path.is_file():
            raise TaskVerificationError(f"published task {task_id} is missing {label}")

    public_task = read_json(public_task_path, label="public task")
    if not isinstance(public_task, Mapping):
        raise TaskVerificationError(f"published task {task_id} public/task.json is not an object")
    try:
        parsed_task = BenchmarkTask.from_dict(public_task, where="public/task.json")
    except SchemaError as exc:
        raise TaskVerificationError(f"published task {task_id} is invalid: {exc}") from exc
    if public_task.get("schema_version") != BENCHMARK_TASK_SCHEMA:
        raise TaskVerificationError(
            f"published task {task_id} declares schema_version="
            f"{public_task.get('schema_version')!r}, expected {BENCHMARK_TASK_SCHEMA!r}"
        )
    if public_task.get("task_id") != task_id:
        raise TaskVerificationError(
            f"published task directory {task_id} holds task_id={public_task.get('task_id')!r}"
        )

    public_environment = read_json(public_environment_path, label="public environment")
    if not isinstance(public_environment, Mapping):
        raise TaskVerificationError(
            f"published task {task_id} public/environment.json is not an object"
        )
    try:
        parsed_environment = ProjectEnvironment.from_dict(
            public_environment, where="public/environment.json"
        )
    except SchemaError as exc:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid public environment: {exc}"
        ) from exc
    if any(not token for command in parsed_environment.setup for token in command):
        raise TaskVerificationError(
            f"published task {task_id} public environment has an invalid setup command"
        )
    task_environment = parsed_task.environment
    expected_projection = {
        "image": parsed_environment.image,
        "setup_command": parsed_environment.setup[0] if parsed_environment.setup else [],
        "network": parsed_environment.network_during_run,
    }
    for key, expected in expected_projection.items():
        if task_environment.get(key) != expected:
            raise TaskVerificationError(
                f"published task {task_id} environment.{key} does not match "
                f"public/environment.json"
            )
    if parsed_task.repository.get("base_sha") != parsed_environment.base_sha:
        raise TaskVerificationError(
            f"published task {task_id} environment base_sha does not match its repository"
        )
    wall_time = parsed_task.limits.get("wall_time_seconds")
    if isinstance(wall_time, bool) or not isinstance(wall_time, int) or wall_time <= 0:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid wall_time_seconds limit"
        )

    scorer_payload = read_json(scorer_manifest, label="scorer.json")
    if not isinstance(scorer_payload, Mapping):
        raise TaskVerificationError(
            f"published task {task_id} private/scorer/scorer.json is not an object"
        )
    try:
        parsed_scorer = ScorerManifest.from_dict(scorer_payload, where="private/scorer/scorer.json")
    except SchemaError as exc:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid scorer manifest: {exc}"
        ) from exc
    if parsed_scorer.task_id != task_id:
        raise TaskVerificationError(
            f"published task {task_id} scorer manifest targets {parsed_scorer.task_id!r}"
        )
    if not math.isfinite(parsed_scorer.pass_threshold) or any(
        not math.isfinite(component.weight) for component in parsed_scorer.components
    ):
        raise TaskVerificationError(
            f"published task {task_id} scorer manifest contains non-finite numbers"
        )
    public_threshold = parsed_task.scoring.get("pass_threshold")
    if (
        isinstance(public_threshold, bool)
        or not isinstance(public_threshold, (int, float))
        or not math.isfinite(float(public_threshold))
        or not 0.0 <= float(public_threshold) <= 1.0
    ):
        raise TaskVerificationError(
            f"published task {task_id} has an invalid public pass threshold"
        )
    if abs(parsed_scorer.pass_threshold - float(public_threshold)) > 1e-9:
        raise TaskVerificationError(
            f"published task {task_id} scorer and public pass thresholds disagree"
        )

    provenance = read_json(provenance_path, label="task provenance")
    if not isinstance(provenance, Mapping):
        raise TaskVerificationError(f"published task {task_id} provenance is not an object")

    public_task_sha256 = sha256_file(public_task_path)
    prompt_sha256 = sha256_file(prompt_path)
    public_environment_sha256 = sha256_file(public_environment_path)
    base_bundle_sha256 = sha256_file(base_bundle)

    for label, actual, declared in (
        ("public/task.json", public_task_sha256, provenance.get("public_task_sha256")),
        ("public/base.bundle", base_bundle_sha256, provenance.get("base_bundle_sha256")),
    ):
        if isinstance(declared, str) and declared and declared != actual:
            raise TaskVerificationError(
                f"published task {task_id} {label} hashes to {actual}, provenance says {declared}"
            )

    resolved_scorer_sha = _verify_scorer_package(task_id, scorer_manifest, provenance)
    return PublishedTask(
        task_id=task_id,
        task_dir=task_dir,
        public_task=public_task,
        public_task_path=public_task_path,
        prompt_path=prompt_path,
        public_environment=public_environment,
        public_environment_path=public_environment_path,
        base_bundle=base_bundle,
        scorer_manifest=scorer_payload,
        scorer_manifest_path=scorer_manifest,
        provenance=provenance,
        public_task_sha256=public_task_sha256,
        prompt_sha256=prompt_sha256,
        public_environment_sha256=public_environment_sha256,
        base_bundle_sha256=base_bundle_sha256,
        scorer_package_sha256=resolved_scorer_sha,
    )


def _scorer_package_hashes(
    scorer_manifest: Path,
) -> tuple[str, str | None, str | None]:
    """Return current, legacy, and package-declared scorer hashes."""
    manifest = read_json(scorer_manifest, label="scorer.json")
    payload: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    try:
        computed = compute_scorer_package_hash(scorer_manifest.parent)
    except ValueError as error:
        raise TaskVerificationError(str(error)) from error
    legacy_files: list[tuple[str, str]] = []
    legacy_compatible = True
    for path in sorted(scorer_manifest.parent.rglob("*")):
        if path.is_symlink():
            raise TaskVerificationError(
                "scorer package contains unsupported symlink: "
                f"{path.relative_to(scorer_manifest.parent).as_posix()}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(scorer_manifest.parent).as_posix()
        if path.name == "scorer.json":
            if relative != "scorer.json":
                legacy_compatible = False
            continue
        if path.is_file():
            legacy_files.append(
                (
                    relative,
                    sha256_file(path),
                )
            )
    legacy = (
        sha256_json(
            {
                "scorer_json": {
                    key: value
                    for key, value in payload.items()
                    if key != "package_sha256"
                },
                "files": legacy_files,
            }
        )
        if legacy_compatible
        else None
    )
    declared = payload.get("package_sha256")
    return computed, legacy, declared if isinstance(declared, str) and declared else None


def _verify_scorer_package(
    task_id: str, scorer_manifest: Path, provenance: Mapping[str, Any]
) -> str:
    computed, legacy, declared = _scorer_package_hashes(scorer_manifest)
    recorded = provenance.get("scorer_package_sha256")
    accepted = {computed}
    if legacy is not None:
        accepted.add(legacy)
    if declared is not None and declared not in accepted:
        raise TaskVerificationError(
            f"published task {task_id} scorer.json declares {declared}, but the package "
            f"hash is {computed}"
        )
    if not isinstance(recorded, str) or not recorded:
        raise TaskVerificationError(
            f"published task {task_id} provenance has no scorer_package_sha256"
        )
    if recorded not in accepted:
        raise TaskVerificationError(
            f"published task {task_id} private/scorer hashes to {computed}, provenance says "
            f"{recorded}; the scorer package changed after publication"
        )
    return computed


@dataclass(frozen=True)
class RunConfig:
    """Everything one ``retro benchmark taskset run`` invocation needs."""

    ghostlab: GhostlabCli
    eval_id: str
    seeds: tuple[int, ...] = (0,)
    aut_timeout_seconds: float | None = None
    scorer_timeout_seconds: float | None = None
    candidate_export_name: str = CANDIDATE_EXPORT_NAME
    force: bool = False


@dataclass(frozen=True)
class AttemptResult:
    """One immutable ``retro-benchmark-attempt-v1`` outcome."""

    attempt_id: str
    task_id: str
    agent_id: str
    seed: int
    status: str
    attempt_dir: Path
    input_sha256: str
    agent_config_sha256: str
    base_bundle_sha256: str
    scorer_package_sha256: str
    source_id: str | None = None
    candidate_state_sha256: str | None = None
    score: float | None = None
    passed: bool | None = None
    pass_threshold: float = 0.8
    components: tuple[Mapping[str, Any], ...] = ()
    tokens: Mapping[str, int] = field(default_factory=dict)
    wall_time_ms: int = 0
    cost_usd: float | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    reused: bool = False
    artifact_run: str | None = None
    score_report: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ATTEMPT_SCHEMA,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "source_id": self.source_id,
            "agent_id": self.agent_id,
            "seed": self.seed,
            "status": self.status,
            "agent_config_sha256": self.agent_config_sha256,
            "base_bundle_sha256": self.base_bundle_sha256,
            "candidate_state_sha256": self.candidate_state_sha256,
            "scorer_package_sha256": self.scorer_package_sha256,
            "input_sha256": self.input_sha256,
            "score": self.score,
            "passed": self.passed,
            "pass_threshold": self.pass_threshold,
            "components": [dict(component) for component in self.components],
            "tokens": dict(sorted(self.tokens.items())),
            "wall_time_ms": self.wall_time_ms,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "warnings": list(self.warnings),
            "artifact_run": self.artifact_run,
            "score_report": self.score_report,
            "created_at": self.created_at,
        }

    def record(self) -> AttemptRecord:
        return AttemptRecord.from_mapping(self.to_dict(), path=self.attempt_dir / "attempt.json")


def _normalize_tokens(report: Mapping[str, Any]) -> dict[str, int]:
    for key in ("tokens", "usage"):
        raw = report.get(key)
        if isinstance(raw, Mapping):
            tokens: dict[str, int] = {}
            for name, value in raw.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                normalized = str(name).replace("_tokens", "")
                tokens[normalized] = int(value)
            if tokens:
                return tokens
    return {}


def _cost_usd(report: Mapping[str, Any]) -> float | None:
    for container in (report, report.get("usage") if isinstance(report.get("usage"), Mapping) else {}):
        if isinstance(container, Mapping):
            value = container.get("cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def compute_attempt_id(
    task: PublishedTask,
    agent: AgentSpec,
    seed: int,
    ghostlab_version: Mapping[str, Any],
    *,
    aut_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
    candidate_export_name: str = CANDIDATE_EXPORT_NAME,
    agent_asset_hashes: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Hash-address one attempt over every input that can change its result."""
    effective_aut_timeout = (
        float(aut_timeout_seconds)
        if aut_timeout_seconds is not None
        else float(task.wall_time_seconds)
    )
    effective_scorer_timeout = (
        float(scorer_timeout_seconds)
        if scorer_timeout_seconds is not None
        else task.scorer_timeout_seconds
    )
    for label, value in (
        ("agent timeout", effective_aut_timeout),
        ("scorer timeout", effective_scorer_timeout),
    ):
        if not math.isfinite(value) or value <= 0:
            raise TaskVerificationError(f"{label} must be a positive finite number")
    if Path(candidate_export_name).name != candidate_export_name or candidate_export_name in {
        "",
        ".",
        "..",
    }:
        raise TaskVerificationError(
            f"candidate export name {candidate_export_name!r} must be a plain file name"
        )

    evaluation_fingerprint = sha256_json(
        {
            "task_id": task.task_id,
            "public_task_sha256": task.public_task_sha256,
            "prompt_sha256": task.prompt_sha256,
            "public_environment_sha256": task.public_environment_sha256,
            "public_environment": dict(task.public_environment),
            "base_bundle_sha256": task.base_bundle_sha256,
            "scorer_package_sha256": task.scorer_package_sha256,
            "agent_id": agent.agent_id,
            "agent_config_sha256": agent.config_sha256,
            "agent_referenced_assets": dict(
                agent_asset_hashes
                if agent_asset_hashes is not None
                else agent.referenced_asset_hashes()
            ),
            "ghostlab": dict(ghostlab_version),
            "artifact_run": {
                "timeout_seconds": effective_aut_timeout,
                "export_workspace": candidate_export_name,
                "task_base_image": task.environment_image,
                "sandbox_image_override": agent.sandbox_image_override(
                    task.environment_image
                ),
                "setup_commands": [list(command) for command in task.setup_commands],
            },
            "scorer_run": {"timeout_seconds": effective_scorer_timeout},
        }
    )
    attempt_id = sha256_json(
        {"evaluation_fingerprint": evaluation_fingerprint, "seed": seed}
    )[:20]
    return attempt_id, evaluation_fingerprint


def _write_attempt(attempt: AttemptResult) -> AttemptResult:
    write_json(attempt.attempt_dir / "attempt.json", attempt.to_dict())
    return attempt


def run_attempt(
    paths: TasksetPaths,
    config: RunConfig,
    task: PublishedTask,
    agent: AgentSpec,
    seed: int,
) -> AttemptResult:
    """Run one ``(task, agent, seed)`` attempt end to end."""
    agent.verify()
    _validate_safe_id(config.eval_id, "eval id")
    _validate_safe_id(task.task_id, "task id")
    _validate_safe_id(agent.agent_id, "agent id")
    if seed < 0:
        raise TaskVerificationError("seed must be non-negative")

    effective_aut_timeout = (
        config.aut_timeout_seconds
        if config.aut_timeout_seconds is not None
        else float(task.wall_time_seconds)
    )
    effective_scorer_timeout = (
        config.scorer_timeout_seconds
        if config.scorer_timeout_seconds is not None
        else task.scorer_timeout_seconds
    )
    version_error: GhostlabError | None = None
    try:
        version = config.ghostlab.version().fingerprint()
    except GhostlabError as exc:
        version_error = exc
        version = {
            "binary": config.ghostlab.binary,
            "version": None,
            "binary_sha256": None,
        }
    agent_asset_hashes = agent.referenced_asset_hashes()
    attempt_id, input_sha256 = compute_attempt_id(
        task,
        agent,
        seed,
        version,
        aut_timeout_seconds=effective_aut_timeout,
        scorer_timeout_seconds=effective_scorer_timeout,
        candidate_export_name=config.candidate_export_name,
        agent_asset_hashes=agent_asset_hashes,
    )
    attempt_dir = paths.attempt_dir(config.eval_id, task.task_id, agent.agent_id, seed)
    attempt_path = attempt_dir / "attempt.json"

    if attempt_path.is_file() and not config.force:
        payload = read_json(attempt_path, label="attempt record")
        if isinstance(payload, Mapping) and payload.get("input_sha256") == input_sha256:
            try:
                record = validate_attempt_record(payload, path=attempt_path)
            except ValueError:
                record = None
            if (
                record is not None
                and record.attempt_id == attempt_id
                and record.task_id == task.task_id
                and record.agent_id == agent.agent_id
                and record.seed == seed
            ):
                trusted = True
                if record.status == "scored":
                    try:
                        validate_scorer_run_attestation(
                            attempt_dir / "scorer" / SCORER_RUN_REPORT_NAME,
                            task_id=task.task_id,
                            attempt_id=attempt_id,
                            status="scored",
                            task_sha256=task.public_task_sha256,
                            scorer_package_sha256=task.scorer_package_sha256,
                            mode=str(task.scorer_manifest.get("mode", "")),
                        )
                    except GhostlabContractError:
                        trusted = False
                if trusted:
                    return _reuse(payload, attempt_dir)

    attempt_dir.mkdir(parents=True, exist_ok=True)

    def failed(status: str, detail: str, **extra: Any) -> AttemptResult:
        return _write_attempt(
            AttemptResult(
                attempt_id=attempt_id,
                task_id=task.task_id,
                agent_id=agent.agent_id,
                seed=seed,
                status=status,
                attempt_dir=attempt_dir,
                input_sha256=input_sha256,
                agent_config_sha256=agent.config_sha256,
                base_bundle_sha256=task.base_bundle_sha256,
                scorer_package_sha256=task.scorer_package_sha256,
                source_id=task.source_id,
                pass_threshold=task.pass_threshold,
                error=detail,
                **extra,
            )
        )

    if version_error is not None:
        return failed("harness_error", str(version_error))

    workspace = attempt_dir / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    try:
        unpack_bundle(task.base_bundle, workspace)
    except Exception as exc:  # noqa: BLE001 - materialization failure is a harness failure
        return failed("harness_error", f"could not materialize the base repository: {exc}")

    prompt_path = attempt_dir / "prompt.txt"
    shutil.copyfile(task.prompt_path, prompt_path)

    agent_run_dir = attempt_dir / "agent"
    if agent_run_dir.exists():
        shutil.rmtree(agent_run_dir)
    runtime_inputs = attempt_dir / ".runtime-inputs"
    try:
        execution_config, sandbox_image_override = agent.execution_config(
            task.environment_image,
            runtime_inputs,
            expected_asset_hashes=agent_asset_hashes,
        )
    except Exception:
        if runtime_inputs.exists():
            shutil.rmtree(runtime_inputs)
        raise
    artifact_request = ArtifactRunRequest(
        agent_config=execution_config,
        workspace=workspace,
        prompt_file=prompt_path,
        run_dir=agent_run_dir,
        export_workspace=config.candidate_export_name,
        timeout_seconds=effective_aut_timeout,
        sandbox_image=sandbox_image_override,
        setup_commands=task.setup_commands,
        label="candidate-agent",
    )
    started = time.monotonic()
    try:
        try:
            run = config.ghostlab.artifact_run(artifact_request)
        finally:
            if runtime_inputs.exists():
                shutil.rmtree(runtime_inputs)
    except GhostlabTimeoutError as exc:
        return failed("agent_timeout", str(exc))
    except (GhostlabBinaryError, GhostlabInvocationError) as exc:
        return failed("harness_error", str(exc))
    except GhostlabContractError as exc:
        return failed("harness_error", str(exc))
    except GhostlabError as exc:
        return failed("agent_error", str(exc))

    wall_time_ms = int((time.monotonic() - started) * 1000)
    tokens = _normalize_tokens(run.report)
    cost = _cost_usd(run.report)
    mapped = ARTIFACT_STATUS_TO_ATTEMPT.get(run.status, "harness_error")
    if mapped is not None:
        return failed(
            mapped,
            f"candidate artifact-run reported status={run.status}: "
            f"{run.stderr_tail or '<no stderr>'}",
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )

    candidate = next(
        (
            path
            for name in artifact_request.workspace_exports()
            if (path := run.export_path(name)) is not None
        ),
        None,
    )
    if candidate is None:
        return failed(
            "harness_error",
            "candidate artifact-run exported no workspace archive matching "
            f"{artifact_request.workspace_exports()}",
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    candidate_sha256 = run.export_sha256.get(config.candidate_export_name) or sha256_path(
        candidate, excludes=()
    )

    resources_path = attempt_dir / "resources.json"
    write_json(
        resources_path,
        {
            "schema_version": RESOURCE_USAGE_SCHEMA,
            "task_id": task.task_id,
            "attempt_id": attempt_id,
            "tokens": dict(sorted(tokens.items())),
            "wall_time_ms": wall_time_ms,
            "cost_usd": cost,
        },
    )

    trace = run.events_path if run.events_path and run.events_path.is_file() else None
    score_report_path = attempt_dir / "score-report.json"
    scorer_run_dir = attempt_dir / "scorer"
    score_report_path.unlink(missing_ok=True)
    if scorer_run_dir.exists():
        shutil.rmtree(scorer_run_dir)
    try:
        scored = config.ghostlab.scorer_run(
            ScorerRunRequest(
                task_path=task.public_task_path,
                scorer_path=task.scorer_manifest_path,
                candidate_path=candidate,
                output_path=score_report_path,
                attempt_id=attempt_id,
                trace_path=trace,
                resource_usage_path=resources_path,
                seed=seed,
                run_dir=scorer_run_dir,
                timeout_seconds=effective_scorer_timeout,
                label="scorer",
            )
        )
        validate_scorer_run_attestation(
            scored.run_report_path,
            task_id=task.task_id,
            attempt_id=attempt_id,
            status=scored.status,
            task_sha256=task.public_task_sha256,
            scorer_package_sha256=task.scorer_package_sha256,
            mode=str(task.scorer_manifest.get("mode", "")),
            run_dir=scorer_run_dir,
        )
    except GhostlabTimeoutError as exc:
        return failed(
            "scorer_timeout",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except (GhostlabBinaryError, GhostlabInvocationError) as exc:
        return failed(
            "harness_error",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except GhostlabContractError as exc:
        return failed(
            "invalid_result",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except GhostlabError as exc:
        return failed(
            "scorer_error",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )

    validation = validate_score_report(
        scored.report,
        pass_threshold=task.pass_threshold,
        scorer_manifest=task.scorer_manifest,
        expected_task_id=task.task_id,
        expected_attempt_id=attempt_id,
        expected_scorer_package_sha256=task.scorer_package_sha256,
    )
    status = scored.status
    report_errors = list(validation.errors)
    if scored.status == "scored":
        if scored.report.get("valid") is not True:
            report_errors.append("scored report must carry valid=true")
        reported_threshold = scored.report.get("pass_threshold")
        if (
            isinstance(reported_threshold, bool)
            or not isinstance(reported_threshold, (int, float))
            or not math.isfinite(float(reported_threshold))
            or abs(float(reported_threshold) - task.pass_threshold) > 1e-9
        ):
            report_errors.append(
                "score report pass_threshold does not match the published scorer"
            )
        reported_unscored = scored.report.get("unscored_weight")
        if (
            isinstance(reported_unscored, bool)
            or not isinstance(reported_unscored, (int, float))
            or not math.isfinite(float(reported_unscored))
            or abs(float(reported_unscored) - validation.unscored_weight) > 1e-9
        ):
            report_errors.append(
                "score report unscored_weight does not match its component results"
            )
    error: str | None = None
    if report_errors:
        status = "invalid_result"
        error = "; ".join(report_errors)

    return _write_attempt(
        AttemptResult(
            attempt_id=attempt_id,
            task_id=task.task_id,
            agent_id=agent.agent_id,
            seed=seed,
            status=status,
            attempt_dir=attempt_dir,
            input_sha256=input_sha256,
            agent_config_sha256=agent.config_sha256,
            base_bundle_sha256=task.base_bundle_sha256,
            scorer_package_sha256=task.scorer_package_sha256,
            source_id=task.source_id,
            candidate_state_sha256=candidate_sha256,
            score=validation.score_total if status == "scored" else None,
            passed=validation.passed if status == "scored" else None,
            pass_threshold=task.pass_threshold,
            components=tuple(dict(component) for component in scored.components),
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            error=error,
            warnings=validation.warnings,
            artifact_run=str(run.report_path),
            score_report=str(scored.report_path),
        )
    )


def _reuse(payload: Mapping[str, Any], attempt_dir: Path) -> AttemptResult:
    tokens_raw = payload.get("tokens")
    tokens = (
        {str(key): int(value) for key, value in tokens_raw.items() if isinstance(value, (int, float))}
        if isinstance(tokens_raw, Mapping)
        else {}
    )
    components = tuple(
        dict(item) for item in payload.get("components") or () if isinstance(item, Mapping)
    )
    score = payload.get("score")
    threshold = payload.get("pass_threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
    ):
        raise TaskVerificationError(
            f"attempt record {attempt_dir / 'attempt.json'} has no valid "
            "pass_threshold provenance"
        )
    cost = payload.get("cost_usd")
    created_at = payload.get("created_at")
    return AttemptResult(
        attempt_id=str(payload.get("attempt_id", "")),
        task_id=str(payload.get("task_id", "")),
        agent_id=str(payload.get("agent_id", "")),
        seed=int(payload.get("seed") or 0),
        status=str(payload.get("status", "harness_error")),
        attempt_dir=attempt_dir,
        input_sha256=str(payload.get("input_sha256", "")),
        agent_config_sha256=str(payload.get("agent_config_sha256", "")),
        base_bundle_sha256=str(payload.get("base_bundle_sha256", "")),
        scorer_package_sha256=str(payload.get("scorer_package_sha256", "")),
        source_id=payload.get("source_id") if isinstance(payload.get("source_id"), str) else None,
        candidate_state_sha256=(
            payload.get("candidate_state_sha256")
            if isinstance(payload.get("candidate_state_sha256"), str)
            else None
        ),
        score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        passed=payload.get("passed") if isinstance(payload.get("passed"), bool) else None,
        pass_threshold=float(threshold),
        components=components,
        tokens=tokens,
        wall_time_ms=int(payload.get("wall_time_ms") or 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        reused=True,
        artifact_run=payload.get("artifact_run") if isinstance(payload.get("artifact_run"), str) else None,
        score_report=payload.get("score_report") if isinstance(payload.get("score_report"), str) else None,
        created_at=created_at if isinstance(created_at, str) else utc_now(),
    )


@dataclass(frozen=True)
class EvalResult:
    name: str
    eval_id: str
    agent_id: str
    attempts: tuple[AttemptResult, ...]
    aggregate: BenchmarkAggregate
    results_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "aggregate": self.aggregate.to_dict(),
            "results_path": str(self.results_path),
        }


def run_agent(
    paths: TasksetPaths,
    config: RunConfig,
    agent: AgentSpec,
    task_ids: Iterable[str] | None = None,
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> EvalResult:
    """Evaluate one agent over the published task set at every requested seed."""
    _validate_safe_id(config.eval_id, "eval id")
    agent.verify()
    resolved = sorted(task_ids) if task_ids is not None else list_published_tasks(paths)
    attempts: list[AttemptResult] = []
    for task_id in resolved:
        task = verify_published_task(paths, task_id)
        for seed in config.seeds:
            attempts.append(run_attempt(paths, config, task, agent, seed))
    aggregate = collect_eval_report(
        paths,
        config.eval_id,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return EvalResult(
        name=paths.name,
        eval_id=config.eval_id,
        agent_id=agent.agent_id,
        attempts=tuple(attempts),
        aggregate=aggregate,
        results_path=paths.results_path(config.eval_id),
    )


def task_source_index(paths: TasksetPaths, task_ids: Sequence[str] | None = None) -> dict[str, str]:
    """Map every published task id to the rollout source it came from."""
    index: dict[str, str] = {}
    for task_id in task_ids if task_ids is not None else list_published_tasks(paths):
        provenance = paths.task_dir(task_id) / "private" / "provenance.json"
        if not provenance.is_file():
            continue
        payload = read_json(provenance, label="task provenance")
        if isinstance(payload, Mapping) and isinstance(payload.get("source_id"), str):
            index[task_id] = payload["source_id"]
    return index


def task_threshold_index(paths: TasksetPaths, task_ids: Sequence[str] | None = None) -> dict[str, float]:
    """Map current published tasks to thresholds; never use this for historical reports."""
    thresholds: dict[str, float] = {}
    for task_id in task_ids if task_ids is not None else list_published_tasks(paths):
        public = paths.task_dir(task_id) / "public" / "task.json"
        if not public.is_file():
            continue
        payload = read_json(public, label="public task")
        if isinstance(payload, Mapping) and isinstance(payload.get("scoring"), Mapping):
            value = payload["scoring"].get("pass_threshold")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                thresholds[task_id] = float(value)
    return thresholds


def collect_eval_report(
    paths: TasksetPaths,
    eval_id: str,
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> BenchmarkAggregate:
    """Aggregate every attempt written under one eval and publish ``results.json``."""
    eval_dir = paths.eval_dir(eval_id)
    records = load_attempts(eval_dir) if eval_dir.is_dir() else []
    aggregate = aggregate_attempts(
        records,
        name=paths.name,
        eval_id=eval_id,
        task_sources=task_source_index(paths),
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
        generated_at=utc_now(),
    )
    write_aggregate(paths.results_path(eval_id), aggregate)
    return aggregate


__all__ = [
    "ARTIFACT_STATUS_TO_ATTEMPT",
    "CANDIDATE_EXPORT_NAME",
    "RESOURCE_USAGE_SCHEMA",
    "AgentSpec",
    "AttemptResult",
    "EvalResult",
    "PublishedTask",
    "RunConfig",
    "TaskVerificationError",
    "TasksetReportSummary",
    "TasksetRunSummary",
    "collect_eval_report",
    "compute_attempt_id",
    "default_eval_id",
    "list_evals",
    "parse_seeds",
    "report_taskset",
    "resolve_eval_id",
    "resolve_run_eval_id",
    "run_agent",
    "unique_eval_id",
    "run_attempt",
    "run_taskset",
    "summarize_run",
    "task_source_index",
    "task_threshold_index",
    "verify_published_task",
]


# ---------------------------------------------------------------------------
# CLI-facing entry points: retro benchmark taskset run|report
# ---------------------------------------------------------------------------


def resolve_run_eval_id(paths: TasksetPaths, requested: str | None) -> str:
    """Pick the eval an evaluation run writes into.

    ``None``/``latest`` continues the newest eval so a second ``run`` for another
    agent lands beside the first and ``report --eval latest`` compares both.
    ``new`` always starts a fresh timestamped eval.
    """
    if requested is None or requested == "latest":
        existing = list_evals(paths)
        return existing[-1] if existing else unique_eval_id(paths)
    if requested == "new":
        return unique_eval_id(paths)
    if not _SAFE_ID_RE.fullmatch(requested):
        raise TaskVerificationError(f"eval id {requested!r} contains unsupported characters")
    return requested


def _score_row(attempt: AttemptResult) -> dict[str, Any]:
    return {
        "task_id": attempt.task_id,
        "source_id": attempt.source_id or "",
        "agent_id": attempt.agent_id,
        "seed": attempt.seed,
        "status": attempt.status,
        "score": attempt.score,
        "passed": attempt.passed,
        "tokens": attempt.record().total_tokens,
        "wall_time_ms": attempt.wall_time_ms,
        "cost_usd": attempt.cost_usd,
        "reused": attempt.reused,
        "attempt_id": attempt.attempt_id,
        "error": attempt.error or "",
    }


def _aggregate_task_rows(aggregate: BenchmarkAggregate) -> list[dict[str, Any]]:
    return list(iter_status_rows(aggregate))


def _agent_rows(aggregate: BenchmarkAggregate) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for agent in aggregate.agents:
        rows.append(
            {
                "agent_id": agent.agent_id,
                "benchmark_score": agent.benchmark_score,
                "pass_rate": agent.pass_rate,
                "valid_coverage": agent.coverage,
                "scored_attempts": agent.scored_attempts,
                "requested_attempts": agent.requested_attempts,
                "agent_errors": agent.error_counts.get("agent_error", 0),
                "scorer_errors": agent.error_counts.get("scorer_error", 0),
                "harness_errors": agent.error_counts.get("harness_error", 0),
                "sources": len(agent.sources),
                "tasks": len(agent.tasks),
            }
        )
    return rows


@dataclass(frozen=True)
class TasksetRunSummary:
    """Rich-renderable outcome of ``retro benchmark taskset run``."""

    name: str
    eval_id: str
    agent_id: str
    eval_dir: Path
    results_path: Path
    seeds: tuple[int, ...]
    attempts: tuple[AttemptResult, ...]
    requested_attempts: int
    scored_attempts: int
    reused_attempts: int
    status_counts: Mapping[str, int]
    benchmark_score: float | None
    pass_rate: float | None
    coverage: float
    aggregate: BenchmarkAggregate

    @property
    def failed_attempts(self) -> int:
        return self.requested_attempts - self.scored_attempts

    def attempt_rows(self) -> list[dict[str, Any]]:
        return [_score_row(attempt) for attempt in self.attempts]

    def task_rows(self) -> list[dict[str, Any]]:
        return [
            row for row in _aggregate_task_rows(self.aggregate) if row["agent_id"] == self.agent_id
        ]

    def error_rows(self) -> list[dict[str, Any]]:
        return [
            _score_row(attempt) for attempt in self.attempts if attempt.status != SCORED_STATUS
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "eval_dir": str(self.eval_dir),
            "results_path": str(self.results_path),
            "seeds": list(self.seeds),
            "requested_attempts": self.requested_attempts,
            "scored_attempts": self.scored_attempts,
            "reused_attempts": self.reused_attempts,
            "status_counts": dict(sorted(self.status_counts.items())),
            "benchmark_score": self.benchmark_score,
            "pass_rate": self.pass_rate,
            "valid_coverage": self.coverage,
            "attempts": self.attempt_rows(),
            "tasks": self.task_rows(),
        }


@dataclass(frozen=True)
class TasksetReportSummary:
    """Rich-renderable outcome of ``retro benchmark taskset report``."""

    name: str
    eval_id: str
    eval_dir: Path
    results_path: Path
    aggregate: BenchmarkAggregate

    @property
    def agents(self) -> tuple[Any, ...]:
        return self.aggregate.agents

    def agent_rows(self) -> list[dict[str, Any]]:
        return _agent_rows(self.aggregate)

    def task_rows(self) -> list[dict[str, Any]]:
        return _aggregate_task_rows(self.aggregate)

    def source_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in self.aggregate.agents:
            for source in agent.sources:
                rows.append(
                    {
                        "agent_id": agent.agent_id,
                        "source_id": source.source_id,
                        "mean_score": source.mean_score,
                        "pass_rate": source.pass_rate,
                        "tasks": len(source.task_ids),
                        "scored_tasks": len(source.scored_task_ids),
                    }
                )
        return rows

    def component_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, "component_id": key, "mean_value": value}
            for agent in self.aggregate.agents
            for key, value in sorted(agent.component_means.items())
        ]

    def resource_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in self.aggregate.agents:
            resources = dict(agent.resources)
            tokens = resources.get("tokens_mean") or {}
            rows.append(
                {
                    "agent_id": agent.agent_id,
                    "scored_attempts": resources.get("scored_attempts", 0),
                    "tokens_mean": tokens,
                    "wall_time_ms_mean": resources.get("wall_time_ms_mean"),
                    "cost_usd_mean": resources.get("cost_usd_mean"),
                    "cost_usd_total": resources.get("cost_usd_total"),
                }
            )
        return rows

    def budget_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, **budget.to_dict()}
            for agent in self.aggregate.agents
            for budget in agent.budget_conditionals
        ]

    def error_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, **{key: value for key, value in agent.error_counts.items()}}
            for agent in self.aggregate.agents
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "eval_dir": str(self.eval_dir),
            "results_path": str(self.results_path),
            "aggregate": self.aggregate.to_dict(),
            "agents": self.agent_rows(),
            "sources": self.source_rows(),
            "tasks": self.task_rows(),
            "components": self.component_rows(),
            "resources": self.resource_rows(),
            "budgets": self.budget_rows(),
            "errors": self.error_rows(),
        }


def run_taskset(
    layout: Any,
    name: str,
    agent: str | Path | AgentSpec | None = None,
    seeds: str | Sequence[int] = (0,),
    ghostlab_bin: str | Path | None = None,
    *,
    eval_id: str | None = None,
    task_ids: Iterable[str] | None = None,
    force: bool = False,
    agent_id: str | None = None,
    expected_agent_sha256: str | None = None,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
    ghostlab: GhostlabCli | None = None,
    ghostlab_env: Mapping[str, str] | None = None,
    aut_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
    candidate_export_name: str = CANDIDATE_EXPORT_NAME,
) -> TasksetRunSummary:
    """Run ``retro benchmark taskset run`` (spec sections 15 and 20).

    Attempts stay hash-addressed over task, prompt, environment, effective agent
    assets/settings, seed, base, scorer, and Ghostlab version. Only that complete
    fingerprint may reuse ``attempt.json`` unless ``force=True``.
    """
    paths = resolve_taskset_paths(layout, name)
    if agent is None:
        raise TaskVerificationError("--agent is required")
    spec = (
        agent
        if isinstance(agent, AgentSpec)
        else AgentSpec.from_path(
            Path(agent), agent_id=agent_id, expected_sha256=expected_agent_sha256
        )
    )
    resolved_seeds = parse_seeds(seeds)
    config = RunConfig(
        ghostlab=ghostlab or GhostlabCli(ghostlab_bin, env=ghostlab_env),
        eval_id=resolve_run_eval_id(paths, eval_id),
        seeds=resolved_seeds,
        aut_timeout_seconds=aut_timeout_seconds,
        scorer_timeout_seconds=scorer_timeout_seconds,
        candidate_export_name=candidate_export_name,
        force=force,
    )
    result = run_agent(
        paths,
        config,
        spec,
        task_ids,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return summarize_run(paths, result)


def summarize_run(paths: TasksetPaths, result: EvalResult) -> TasksetRunSummary:
    status_counts: dict[str, int] = {}
    for attempt in result.attempts:
        status_counts[attempt.status] = status_counts.get(attempt.status, 0) + 1
    agent = result.aggregate.agent(result.agent_id)
    return TasksetRunSummary(
        name=result.name,
        eval_id=result.eval_id,
        agent_id=result.agent_id,
        eval_dir=paths.eval_dir(result.eval_id),
        results_path=result.results_path,
        seeds=tuple(dict.fromkeys(attempt.seed for attempt in result.attempts)),
        attempts=result.attempts,
        requested_attempts=len(result.attempts),
        scored_attempts=sum(1 for attempt in result.attempts if attempt.status == SCORED_STATUS),
        reused_attempts=sum(1 for attempt in result.attempts if attempt.reused),
        status_counts=status_counts,
        benchmark_score=agent.benchmark_score if agent else None,
        pass_rate=agent.pass_rate if agent else None,
        coverage=agent.coverage if agent else 0.0,
        aggregate=result.aggregate,
    )


def report_taskset(
    layout: Any,
    name: str,
    eval_id: str | None = "latest",
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> TasksetReportSummary:
    """Run ``retro benchmark taskset report`` (spec sections 16 and 20).

    ``eval_id`` accepts ``latest`` (or ``None``) and resolves to the newest eval
    directory; the recomputed aggregate is republished to ``results.json``.
    """
    paths = resolve_taskset_paths(layout, name)
    resolved = resolve_eval_id(paths, eval_id)
    aggregate = collect_eval_report(
        paths,
        resolved,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return TasksetReportSummary(
        name=paths.name,
        eval_id=resolved,
        eval_dir=paths.eval_dir(resolved),
        results_path=paths.results_path(resolved),
        aggregate=aggregate,
    )
