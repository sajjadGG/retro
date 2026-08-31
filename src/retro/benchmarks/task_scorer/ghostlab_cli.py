"""Public Ghostlab CLI adapter for the rollout task/scorer pipeline.

Retro never imports Ghostlab internals for this pipeline. It exchanges versioned
JSON through ``ghostlab artifact-run`` and ``ghostlab scorer-run`` only, and
records version, configuration, input, and output hashes for every invocation so
that unchanged stages can be reused instead of re-executed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .schema import (
    PACKAGED_SCHEMAS,
    SCHEMA_ASSET_DIR,
    SCORER_MODES,
    SchemaError,
    packaged_schema_errors,
)

ARTIFACT_RUN_SCHEMA = "ghostlab-artifact-run-v1"
SCORE_INPUT_SCHEMA = "retro-score-input-v1"
SCORE_REPORT_SCHEMA = "retro-score-report-v1"

ARTIFACT_RUN_REPORT_NAME = "artifact-run.json"
SCORE_REPORT_NAME = "score-report.json"
SCORER_RUN_REPORT_NAME = "scorer-run.json"
GHOSTLAB_SCORER_RUN_SCHEMA = "ghostlab-scorer-run-v1"
GHOSTLAB_SCORER_ISOLATION_SCHEMA = "ghostlab-scorer-isolation-v1"

#: Packaged JSON Schema assets shipped in ``task_scorer/schemas/``.
TASK_DEFINITIONS_CONTRACT = "task-definitions"
SCORE_REPORT_CONTRACT = "score-report"
SCORER_AUDIT_CONTRACT = "scorer-audit"

#: Statuses ``artifact-run.json`` may report. Anything else is a contract error.
ARTIFACT_RUN_STATUSES = frozenset(
    {
        "completed",
        "agent_error",
        "timed_out",
        "timeout",
        "model_unavailable",
        "export_failed",
        "output_contract_failed",
        "contract_violation",
        "sandbox_error",
        "harness_error",
    }
)

#: Statuses ``score-report.json`` may report (spec section 10.4).
SCORE_REPORT_STATUSES = frozenset(
    {
        "scored",
        "invalid_candidate_artifact",
        "scorer_error",
        "scorer_timeout",
        "judge_unavailable",
    }
)

#: Canonical workspace-export exclusions (spec section 13.2).
DEFAULT_TREE_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
)

DEFAULT_TIMEOUT_SECONDS = 3600.0
GHOSTLAB_BIN_ENV = "RETRO_GHOSTLAB_BIN"

_STREAM_TAIL_CHARS = 4000


class GhostlabError(RuntimeError):
    """Base class for every Ghostlab adapter failure.

    The message is intentionally verbose: the caller is a batch pipeline and the
    operator needs the argv, exit status, and stream tails to act.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] | None = None,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        run_dir: Path | None = None,
        hint: str | None = None,
    ) -> None:
        self.summary = message
        self.argv = tuple(argv or ())
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.run_dir = run_dir
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [self.summary]
        if self.argv:
            lines.append(f"  command: {' '.join(self.argv)}")
        if self.exit_code is not None:
            lines.append(f"  exit code: {self.exit_code}")
        if self.run_dir is not None:
            lines.append(f"  run dir: {self.run_dir}")
        for label, stream in (("stdout", self.stdout), ("stderr", self.stderr)):
            tail = _tail(stream)
            if tail:
                lines.append(f"  {label} tail: {tail}")
        if self.hint:
            lines.append(f"  hint: {self.hint}")
        return "\n".join(lines)


class GhostlabBinaryError(GhostlabError):
    """The ghostlab executable could not be resolved or executed."""


class GhostlabInvocationError(GhostlabError):
    """Ghostlab exited non-zero without producing a usable report."""


class GhostlabTimeoutError(GhostlabError):
    """The adapter's own subprocess deadline elapsed."""


class GhostlabContractError(GhostlabError):
    """Ghostlab produced output that violates the versioned JSON contract."""


def _tail(stream: str | None, limit: int = _STREAM_TAIL_CHARS) -> str:
    if not stream:
        return ""
    text = stream.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    """Stable JSON text used for every configuration and input hash."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def write_json(path: Path, value: Any) -> None:
    """Write pretty, stable JSON atomically."""
    from ...utils import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, *, label: str | None = None) -> Any:
    what = label or str(path)
    if not path.exists():
        raise GhostlabContractError(
            f"{what} is missing",
            hint=f"expected a JSON file at {path}",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GhostlabContractError(
            f"{what} is not readable JSON: {exc}",
            hint=f"inspect {path}",
        ) from exc


def _iter_tree_files(root: Path, excludes: Sequence[str]) -> Iterator[Path]:
    blocked = set(excludes)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except (OSError, NotADirectoryError):
            continue
        for entry in entries:
            if entry.name in blocked:
                continue
            if entry.is_symlink():
                yield entry
            elif entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                yield entry


def tree_manifest(
    root: Path, *, excludes: Sequence[str] = DEFAULT_TREE_EXCLUDES
) -> list[dict[str, Any]]:
    """Sorted path/mode/size/sha256 records for a directory (spec section 13.2)."""
    records: list[dict[str, Any]] = []
    for entry in _iter_tree_files(root, excludes):
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            target = os.readlink(entry)
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": target,
                    "sha256": sha256_text(target),
                }
            )
            continue
        info = entry.stat()
        executable = bool(stat.S_IMODE(info.st_mode) & 0o111)
        records.append(
            {
                "path": relative,
                "kind": "file",
                "mode": 0o755 if executable else 0o644,
                "size": info.st_size,
                "sha256": sha256_file(entry),
            }
        )
    records.sort(key=lambda record: record["path"])
    return records


def sha256_path(path: Path, *, excludes: Sequence[str] = DEFAULT_TREE_EXCLUDES) -> str:
    """Content hash of a file or of a directory tree."""
    if path.is_dir():
        return sha256_json(tree_manifest(path, excludes=excludes))
    if path.exists():
        return sha256_file(path)
    raise GhostlabContractError(
        f"cannot hash missing path {path}",
        hint="the pipeline expected this artifact to exist before hashing",
    )


def schema_path(name: str) -> Path:
    """Absolute path to a packaged JSON Schema asset.

    These files are the single source of truth for the JSON exchanged with
    Ghostlab; callers pass them straight to ``--output-contract``.
    """
    filename = PACKAGED_SCHEMAS.get(name)
    if filename is None:
        raise GhostlabContractError(
            f"unknown packaged schema {name!r}",
            hint=f"expected one of {sorted(PACKAGED_SCHEMAS)}",
        )
    path = SCHEMA_ASSET_DIR / filename
    if not path.is_file():
        raise GhostlabContractError(
            f"packaged schema {name!r} is missing at {path}",
            hint="reinstall retro-ai; the schema assets ship with the package",
        )
    return path


def packaged_contract_errors(document: Any, name: str, *, where: str = "$") -> list[str]:
    """Validate ``document`` against a packaged schema asset."""
    try:
        return list(packaged_schema_errors(document, name, where=where))
    except SchemaError as exc:
        raise GhostlabContractError(
            f"packaged JSON Schema {name!r} could not be applied: {exc}",
            hint=f"expected an asset under {SCHEMA_ASSET_DIR}",
        ) from exc


def resolve_ghostlab_binary(
    explicit: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> str:
    """Resolve the ghostlab executable from an explicit path, env, or PATH."""
    environ = os.environ if env is None else env
    for candidate in (explicit, environ.get(GHOSTLAB_BIN_ENV)):
        if not candidate:
            continue
        text = str(candidate)
        path = Path(text).expanduser()
        if path.exists():
            if not os.access(path, os.X_OK):
                raise GhostlabBinaryError(
                    f"ghostlab binary {path} is not executable",
                    hint="chmod +x the binary or pass a different --ghostlab-bin",
                )
            return str(path)
        found = shutil.which(text)
        if found:
            return found
        raise GhostlabBinaryError(
            f"ghostlab binary {text!r} was not found",
            hint=f"pass --ghostlab-bin or set {GHOSTLAB_BIN_ENV}",
        )
    found = shutil.which("ghostlab")
    if found:
        return found
    raise GhostlabBinaryError(
        "ghostlab executable not found on PATH",
        hint=f"pass --ghostlab-bin /path/to/ghostlab or set {GHOSTLAB_BIN_ENV}",
    )


@dataclass(frozen=True)
class GhostlabVersion:
    binary: str
    version: str
    raw: str
    binary_sha256: str | None = None

    def fingerprint(self) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "version": self.version,
            "binary_sha256": self.binary_sha256,
        }


@dataclass(frozen=True)
class ExportSpec:
    """One ``--export <sandbox_path>=<local_name>`` mapping."""

    sandbox_path: str
    local_name: str
    required: bool = True

    @classmethod
    def parse(cls, text: str, *, required: bool = True) -> ExportSpec:
        if "=" not in text:
            raise ValueError(f"export spec {text!r} must be '<sandbox-path>=<local-name>'")
        sandbox_path, _, local_name = text.partition("=")
        if not sandbox_path or not local_name:
            raise ValueError(f"export spec {text!r} must be '<sandbox-path>=<local-name>'")
        return cls(sandbox_path=sandbox_path, local_name=local_name, required=required)

    def as_argument(self) -> str:
        return f"{self.sandbox_path}={self.local_name}"


@dataclass(frozen=True)
class ArtifactRunRequest:
    """Inputs for one ``ghostlab artifact-run`` invocation."""

    agent_config: Path
    workspace: Path
    prompt_file: Path
    run_dir: Path
    exports: tuple[ExportSpec, ...] = ()
    export_workspace: str | None = None
    output_contract: Path | None = None
    timeout_seconds: float | None = None
    sandbox_image: str | None = None
    setup_commands: tuple[tuple[str, ...], ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()
    label: str = "artifact-run"

    def workspace_exports(self) -> tuple[str, ...]:
        if not self.export_workspace:
            return ()
        requested = self.export_workspace
        names = [requested]
        if requested.endswith(".tar.zst"):
            names.append(requested[: -len(".tar.zst")] + ".tar.gz")
        elif requested.endswith(".tgz"):
            names.append(requested[: -len(".tgz")] + ".tar.gz")
        elif not requested.endswith((".tar.gz", ".tar")):
            names.append(requested + ".tar.gz")
        return tuple(dict.fromkeys(names))

    def required_exports(self) -> tuple[str, ...]:
        return tuple(sorted(spec.local_name for spec in self.exports if spec.required))

    def declared_exports(self) -> tuple[str, ...]:
        names = [spec.local_name for spec in self.exports]
        names.extend(self.workspace_exports())
        return tuple(sorted(names))


@dataclass(frozen=True)
class ArtifactRunResult:
    """Normalized ``artifact-run.json`` plus adapter-computed hashes."""

    status: str
    run_dir: Path
    report_path: Path
    report: dict[str, Any]
    exports: dict[str, Path]
    export_sha256: dict[str, str]
    agent_config_sha256: str
    workspace_input_sha256: str
    workspace_output_sha256: str
    prompt_sha256: str
    input_sha256: str
    output_sha256: str
    ghostlab_version: GhostlabVersion
    exit_code: int
    timed_out: bool
    duration_ms: int
    model: str | None
    events_path: Path | None
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def workspace_mutated(self) -> bool:
        """True when the sandbox workspace tree changed during the run."""
        return self.workspace_input_sha256 != self.workspace_output_sha256

    def export_path(self, name: str) -> Path | None:
        return self.exports.get(name)

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_dir": str(self.run_dir),
            "report_path": str(self.report_path),
            "agent_config_sha256": self.agent_config_sha256,
            "workspace_input_sha256": self.workspace_input_sha256,
            "workspace_output_sha256": self.workspace_output_sha256,
            "prompt_sha256": self.prompt_sha256,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "export_sha256": dict(sorted(self.export_sha256.items())),
            "ghostlab": self.ghostlab_version.fingerprint(),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "events_path": str(self.events_path) if self.events_path else None,
        }


@dataclass(frozen=True)
class ScorerRunRequest:
    """Inputs for one ``ghostlab scorer-run`` invocation."""

    task_path: Path
    scorer_path: Path
    candidate_path: Path
    output_path: Path
    attempt_id: str
    trace_path: Path | None = None
    resource_usage_path: Path | None = None
    seed: int = 0
    run_dir: Path | None = None
    timeout_seconds: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()
    label: str = "scorer-run"


@dataclass(frozen=True)
class ScorerRunResult:
    """Normalized ``retro-score-report-v1`` plus adapter-computed hashes."""

    status: str
    report_path: Path
    run_report_path: Path
    report: dict[str, Any]
    task_sha256: str
    scorer_sha256: str
    candidate_sha256: str
    input_sha256: str
    output_sha256: str
    ghostlab_version: GhostlabVersion
    exit_code: int
    duration_ms: int
    seed: int
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def scored(self) -> bool:
        return self.status == "scored"

    @property
    def score_total(self) -> float | None:
        value = self.report.get("score_total")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def passed(self) -> bool | None:
        value = self.report.get("passed")
        return bool(value) if isinstance(value, bool) else None

    @property
    def components(self) -> tuple[dict[str, Any], ...]:
        raw = self.report.get("components")
        if not isinstance(raw, list):
            return ()
        return tuple(item for item in raw if isinstance(item, dict))

    def component_value(self, component_id: str) -> float | None:
        for component in self.components:
            if component.get("id") == component_id:
                value = component.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
                return None
        return None

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "report_path": str(self.report_path),
            "run_report_path": str(self.run_report_path),
            "task_sha256": self.task_sha256,
            "scorer_sha256": self.scorer_sha256,
            "candidate_sha256": self.candidate_sha256,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "ghostlab": self.ghostlab_version.fingerprint(),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "seed": self.seed,
            "score_total": self.score_total,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class CommandOutcome:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


CommandRunner = Callable[[Sequence[str], Optional[float], Mapping[str, str], Optional[Path]], CommandOutcome]


def _default_runner(
    argv: Sequence[str],
    timeout: float | None,
    env: Mapping[str, str],
    cwd: Path | None,
) -> CommandOutcome:
    import time

    merged = dict(os.environ)
    merged.update(env)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=merged,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise GhostlabBinaryError(
            f"ghostlab executable {argv[0]!r} could not be launched: {exc}",
            argv=argv,
            hint="verify --ghostlab-bin points at an executable file",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - started) * 1000)
        return CommandOutcome(
            argv=tuple(argv),
            exit_code=124,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            duration_ms=duration,
            timed_out=True,
        )
    duration = int((time.monotonic() - started) * 1000)
    return CommandOutcome(
        argv=tuple(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=duration,
        timed_out=False,
    )


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class GhostlabCli:
    """Subprocess adapter over the public ``ghostlab`` commands."""

    def __init__(
        self,
        binary: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: Path | None = None,
        runner: CommandRunner | None = None,
        version_timeout_seconds: float = 30.0,
    ) -> None:
        self.binary = resolve_ghostlab_binary(binary, env=env)
        self.env = dict(env or {})
        self.default_timeout_seconds = default_timeout_seconds
        self.cwd = cwd
        self._runner: CommandRunner = runner or _default_runner
        self._version_timeout = version_timeout_seconds
        self._version: GhostlabVersion | None = None

    def version(self) -> GhostlabVersion:
        if self._version is not None:
            return self._version
        argv = [self.binary, "--version"]
        outcome = self._runner(argv, self._version_timeout, self.env, self.cwd)
        if outcome.timed_out:
            raise GhostlabTimeoutError(
                "ghostlab --version timed out",
                argv=argv,
                hint="the binary is not responding; check the installation",
            )
        if outcome.exit_code != 0:
            raise GhostlabBinaryError(
                "ghostlab --version failed",
                argv=argv,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                hint="Retro requires a ghostlab build exposing artifact-run and scorer-run",
            )
        raw = (outcome.stdout or outcome.stderr).strip()
        first = raw.splitlines()[0].strip() if raw else ""
        if not first:
            raise GhostlabContractError(
                "ghostlab --version produced no output",
                argv=argv,
                hint="Retro records the Ghostlab version in every build manifest",
            )
        binary_path = Path(self.binary)
        digest: str | None
        try:
            digest = sha256_file(binary_path) if binary_path.is_file() else None
        except OSError:
            digest = None
        self._version = GhostlabVersion(
            binary=self.binary, version=first.split()[-1] if first.split() else first, raw=first,
            binary_sha256=digest,
        )
        return self._version

    def artifact_run(self, request: ArtifactRunRequest) -> ArtifactRunResult:
        """Run one configured agent once and collect its declared exports."""
        version = self.version()
        _require_file(request.agent_config, "agent config")
        _require_file(request.prompt_file, "prompt file")
        _require_exists(request.workspace, "workspace")
        if request.output_contract is not None:
            _require_file(request.output_contract, "output contract")

        run_dir = request.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)

        agent_config_sha256 = sha256_file(request.agent_config)
        prompt_sha256 = sha256_file(request.prompt_file)
        workspace_local_sha256 = sha256_path(request.workspace)
        output_contract_sha256 = (
            sha256_file(request.output_contract) if request.output_contract else None
        )

        argv = [
            self.binary,
            "artifact-run",
            "--agent",
            str(request.agent_config),
            "--workspace",
            str(request.workspace),
            "--prompt-file",
            str(request.prompt_file),
            "--run-dir",
            str(run_dir),
        ]
        for spec in request.exports:
            argv.extend(
                [
                    "--export" if spec.required else "--optional-export",
                    spec.as_argument(),
                ]
            )
        if request.export_workspace:
            argv.extend(["--export-workspace", request.export_workspace])
        if request.output_contract is not None:
            argv.extend(["--output-contract", str(request.output_contract)])
        if request.sandbox_image:
            argv.extend(["--sandbox-image", request.sandbox_image])
        for command in request.setup_commands:
            if not command or any(not isinstance(part, str) or not part for part in command):
                raise GhostlabContractError(
                    "artifact-run setup commands must be non-empty argument arrays",
                    argv=argv,
                    run_dir=run_dir,
                )
            argv.extend(
                [
                    "--setup-command",
                    json.dumps(list(command), separators=(",", ":")),
                ]
            )
        argv.extend(request.extra_args)

        timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.default_timeout_seconds
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise GhostlabContractError(
                "artifact-run timeout must be a positive finite number",
                argv=argv,
                run_dir=run_dir,
            )
        input_sha256 = sha256_json(
            {
                "command": "artifact-run",
                "ghostlab": version.fingerprint(),
                "agent_config_sha256": agent_config_sha256,
                "prompt_sha256": prompt_sha256,
                "workspace_sha256": workspace_local_sha256,
                "exports": [
                    {
                        "sandbox_path": spec.sandbox_path,
                        "local_name": spec.local_name,
                        "required": spec.required,
                    }
                    for spec in request.exports
                ],
                "export_workspace": request.export_workspace,
                "output_contract_sha256": output_contract_sha256,
                "sandbox_image": request.sandbox_image,
                "setup_commands": [list(command) for command in request.setup_commands],
                "extra_args": list(request.extra_args),
                "timeout_seconds": timeout,
            }
        )

        report_path = run_dir / ARTIFACT_RUN_REPORT_NAME
        expected_outputs = [report_path]
        expected_outputs.extend(
            _run_output_path(run_dir, name, argv) for name in request.declared_exports()
        )
        _clear_expected_outputs(expected_outputs, argv=argv, run_dir=run_dir)

        merged_env = {**self.env, **dict(request.env)}
        outcome = self._runner(argv, timeout, merged_env, self.cwd)
        _persist_streams(run_dir, request.label, outcome)

        if outcome.timed_out and not report_path.exists():
            raise GhostlabTimeoutError(
                f"ghostlab artifact-run exceeded the {timeout:.0f}s adapter deadline",
                argv=argv,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                run_dir=run_dir,
                hint="raise timeout_seconds or lower the agent sandbox timeout",
            )
        if not report_path.exists():
            raise GhostlabInvocationError(
                f"ghostlab artifact-run wrote no {ARTIFACT_RUN_REPORT_NAME}",
                argv=argv,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                run_dir=run_dir,
                hint="the Ghostlab build must implement the artifact-run command",
            )

        report = read_json(report_path, label="artifact-run report")
        if not isinstance(report, dict):
            raise GhostlabContractError(
                "artifact-run report is not a JSON object",
                argv=argv,
                run_dir=run_dir,
            )
        _require_schema(report, ARTIFACT_RUN_SCHEMA, report_path, argv, run_dir)
        status = report.get("status")
        if not isinstance(status, str) or status not in ARTIFACT_RUN_STATUSES:
            raise GhostlabContractError(
                f"artifact-run reported unsupported status {status!r}",
                argv=argv,
                run_dir=run_dir,
                hint=f"expected one of {sorted(ARTIFACT_RUN_STATUSES)}",
            )
        if status == "completed":
            missing_hashes = [
                key
                for key in ("workspace_input_sha256", "workspace_output_sha256")
                if not isinstance(report.get(key), str) or not report.get(key)
            ]
            if missing_hashes:
                raise GhostlabContractError(
                    "completed artifact-run report is missing required workspace hashes: "
                    + ", ".join(missing_hashes),
                    argv=argv,
                    run_dir=run_dir,
                )
            missing_input_hashes = [
                key
                for key in ("agent_config_sha256", "prompt_sha256")
                if not isinstance(report.get(key), str) or not report.get(key)
            ]
            if missing_input_hashes:
                raise GhostlabContractError(
                    "completed artifact-run report is missing required input hashes: "
                    + ", ".join(missing_input_hashes),
                    argv=argv,
                    run_dir=run_dir,
                )
        for key, expected in (
            ("agent_config_sha256", agent_config_sha256),
            ("prompt_sha256", prompt_sha256),
        ):
            actual = report.get(key)
            if actual is not None and actual != expected:
                raise GhostlabContractError(
                    f"artifact-run report {key}={actual!r} does not match "
                    f"the current input hash {expected}",
                    argv=argv,
                    run_dir=run_dir,
                )

        workspace_after_sha256 = sha256_path(request.workspace)
        if workspace_after_sha256 != workspace_local_sha256:
            raise GhostlabContractError(
                "ghostlab artifact-run mutated the caller's workspace instead of its sandbox copy",
                argv=argv,
                run_dir=run_dir,
            )
        _require_unchanged_input(
            request.agent_config, agent_config_sha256, "agent config", argv, run_dir, excludes=()
        )
        _require_unchanged_input(
            request.prompt_file, prompt_sha256, "prompt file", argv, run_dir, excludes=()
        )
        if request.output_contract is not None and output_contract_sha256 is not None:
            _require_unchanged_input(
                request.output_contract,
                output_contract_sha256,
                "output contract",
                argv,
                run_dir,
                excludes=(),
            )
        exports, export_sha256 = self._collect_exports(request, report, run_dir, argv, status)

        events_value = report.get("events_path")
        events_path: Path | None = None
        if isinstance(events_value, str) and events_value:
            candidate = Path(events_value)
            events_path = candidate if candidate.is_absolute() else run_dir / candidate

        workspace_input = _report_hash(report, "workspace_input_sha256", workspace_local_sha256)
        workspace_output = _report_hash(report, "workspace_output_sha256", workspace_input)
        output_sha256 = sha256_json(
            {
                "report": _hashable_report(report),
                "exports": dict(sorted(export_sha256.items())),
            }
        )
        exit_code = report.get("exit_code")
        return ArtifactRunResult(
            status=status,
            run_dir=run_dir,
            report_path=report_path,
            report=report,
            exports=exports,
            export_sha256=export_sha256,
            agent_config_sha256=_report_hash(report, "agent_config_sha256", agent_config_sha256),
            workspace_input_sha256=workspace_input,
            workspace_output_sha256=workspace_output,
            prompt_sha256=_report_hash(report, "prompt_sha256", prompt_sha256),
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            ghostlab_version=version,
            exit_code=int(exit_code) if isinstance(exit_code, int) else outcome.exit_code,
            timed_out=bool(report.get("timed_out")) or outcome.timed_out,
            duration_ms=outcome.duration_ms,
            model=report.get("model") if isinstance(report.get("model"), str) else None,
            events_path=events_path,
            stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr),
        )

    def scorer_run(self, request: ScorerRunRequest) -> ScorerRunResult:
        """Score one candidate repository state in a fresh scorer sandbox."""
        version = self.version()
        _require_file(request.task_path, "public task.json")
        _require_file(request.scorer_path, "scorer.json")
        _require_exists(request.candidate_path, "candidate state")
        if request.trace_path is not None:
            _require_file(request.trace_path, "candidate trace")
        if request.resource_usage_path is not None:
            _require_file(request.resource_usage_path, "resource usage")
        if not isinstance(request.attempt_id, str) or not request.attempt_id.strip():
            raise GhostlabContractError(
                "scorer-run attempt_id must be a non-empty string",
                hint="pass the Retro-generated attempt id through --attempt-id",
            )

        output_path = request.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        run_dir = request.run_dir or output_path.parent
        run_dir.mkdir(parents=True, exist_ok=True)
        run_report_path = run_dir / SCORER_RUN_REPORT_NAME

        task_sha256 = sha256_file(request.task_path)
        scorer_sha256 = sha256_path(request.scorer_path.parent, excludes=())
        candidate_sha256 = sha256_path(request.candidate_path)
        trace_sha256 = sha256_file(request.trace_path) if request.trace_path else None
        resource_usage_sha256 = (
            sha256_file(request.resource_usage_path) if request.resource_usage_path else None
        )
        attempt_id = request.attempt_id

        argv = [
            self.binary,
            "scorer-run",
            "--task",
            str(request.task_path),
            "--scorer",
            str(request.scorer_path),
            "--candidate",
            str(request.candidate_path),
            "--output",
            str(output_path),
            "--attempt-id",
            attempt_id,
        ]
        if request.trace_path is not None:
            argv.extend(["--trace", str(request.trace_path)])
        if request.resource_usage_path is not None:
            argv.extend(["--resources", str(request.resource_usage_path)])
        argv.extend(["--seed", str(request.seed)])
        argv.extend(["--run-dir", str(run_dir)])
        argv.extend(request.extra_args)

        timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.default_timeout_seconds
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise GhostlabContractError(
                "scorer-run timeout must be a positive finite number",
                argv=argv,
                run_dir=run_dir,
            )
        input_sha256 = sha256_json(
            {
                "command": "scorer-run",
                "ghostlab": version.fingerprint(),
                "task_sha256": task_sha256,
                "scorer_sha256": scorer_sha256,
                "candidate_sha256": candidate_sha256,
                "trace_sha256": trace_sha256,
                "resource_usage_sha256": resource_usage_sha256,
                "attempt_id": attempt_id,
                "seed": request.seed,
                "extra_args": list(request.extra_args),
                "timeout_seconds": timeout,
            }
        )

        _clear_expected_outputs(
            (output_path, run_report_path),
            argv=argv,
            run_dir=run_dir,
        )

        merged_env = {**self.env, **dict(request.env)}
        outcome = self._runner(argv, timeout, merged_env, self.cwd)
        _persist_streams(run_dir, request.label, outcome)

        if not output_path.exists():
            if outcome.timed_out:
                raise GhostlabTimeoutError(
                    f"ghostlab scorer-run exceeded the {timeout:.0f}s adapter deadline",
                    argv=argv,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    run_dir=run_dir,
                    hint="raise timeout_seconds or lower scorer runtime.timeout_seconds",
                )
            raise GhostlabInvocationError(
                "ghostlab scorer-run wrote no score report",
                argv=argv,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                run_dir=run_dir,
                hint="a crashing scorer must still be reported as status=scorer_error",
            )

        _require_unchanged_input(
            request.task_path, task_sha256, "public task", argv, run_dir, excludes=()
        )
        _require_unchanged_input(
            request.scorer_path.parent,
            scorer_sha256,
            "scorer package",
            argv,
            run_dir,
            excludes=(),
        )
        _require_unchanged_input(
            request.candidate_path, candidate_sha256, "candidate state", argv, run_dir
        )
        if request.trace_path is not None and trace_sha256 is not None:
            _require_unchanged_input(
                request.trace_path, trace_sha256, "candidate trace", argv, run_dir, excludes=()
            )
        if request.resource_usage_path is not None and resource_usage_sha256 is not None:
            _require_unchanged_input(
                request.resource_usage_path,
                resource_usage_sha256,
                "resource usage",
                argv,
                run_dir,
                excludes=(),
            )
        report = read_json(output_path, label="score report")
        if not isinstance(report, dict):
            raise GhostlabContractError(
                "score report is not a JSON object", argv=argv, run_dir=run_dir
            )
        _require_schema(report, SCORE_REPORT_SCHEMA, output_path, argv, run_dir)
        status = validate_score_report_contract(report, output_path, argv=argv, run_dir=run_dir)

        return ScorerRunResult(
            status=status,
            report_path=output_path,
            run_report_path=run_report_path,
            report=report,
            task_sha256=task_sha256,
            scorer_sha256=scorer_sha256,
            candidate_sha256=candidate_sha256,
            input_sha256=input_sha256,
            output_sha256=sha256_file(output_path),
            ghostlab_version=version,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            seed=request.seed,
            stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr),
        )

    def _collect_exports(
        self,
        request: ArtifactRunRequest,
        report: Mapping[str, Any],
        run_dir: Path,
        argv: Sequence[str],
        status: str,
    ) -> tuple[dict[str, Path], dict[str, str]]:
        declared = set(request.declared_exports())
        reported: dict[str, str | None] = {}
        raw_exports = report.get("exports")
        if raw_exports is not None:
            if not isinstance(raw_exports, list):
                raise GhostlabContractError(
                    "artifact-run report exports must be a list",
                    argv=argv,
                    run_dir=run_dir,
                )
            for item in raw_exports:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise GhostlabContractError(
                        "artifact-run report export entries need a string path",
                        argv=argv,
                        run_dir=run_dir,
                    )
                name = Path(item["path"]).name
                digest = item.get("sha256")
                reported[name] = digest if isinstance(digest, str) else None

        undeclared = sorted(set(reported) - declared)
        if undeclared:
            raise GhostlabContractError(
                f"artifact-run exported undeclared artifacts: {', '.join(undeclared)}",
                argv=argv,
                run_dir=run_dir,
                hint="only artifacts named by --export/--export-workspace may leave the sandbox",
            )

        exports: dict[str, Path] = {}
        export_sha256: dict[str, str] = {}
        for name in sorted(declared):
            path = _run_output_path(run_dir, name, argv)
            if not path.exists():
                if name in request.required_exports() and status == "completed":
                    raise GhostlabContractError(
                        f"artifact-run did not produce required export {name!r}",
                        argv=argv,
                        run_dir=run_dir,
                        hint=f"expected {path}",
                    )
                continue
            digest = sha256_path(path, excludes=())
            declared_digest = reported.get(name)
            if declared_digest and declared_digest != digest and path.is_file():
                raise GhostlabContractError(
                    f"export {name!r} hash mismatch: report {declared_digest} vs file {digest}",
                    argv=argv,
                    run_dir=run_dir,
                    hint="the exported artifact changed after Ghostlab hashed it",
                )
            exports[name] = path
            export_sha256[name] = digest
        workspace_exports = request.workspace_exports()
        if (
            workspace_exports
            and status == "completed"
            and not any(name in exports for name in workspace_exports)
        ):
            raise GhostlabContractError(
                "artifact-run did not produce a requested workspace export "
                f"({', '.join(workspace_exports)})",
                argv=argv,
                run_dir=run_dir,
            )
        return exports, export_sha256


def validate_scorer_run_attestation(
    path: Path,
    *,
    task_id: str,
    attempt_id: str,
    status: str,
    task_sha256: str,
    scorer_package_sha256: str,
    mode: str,
    argv: Sequence[str] = (),
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate Ghostlab's private proof that a scorer run was isolated."""
    context_dir = run_dir or path.parent
    if path.is_symlink() or not path.is_file():
        raise GhostlabContractError(
            f"ghostlab scorer-run wrote no trusted {SCORER_RUN_REPORT_NAME}",
            argv=argv,
            run_dir=context_dir,
            hint="scores require Ghostlab's private scorer isolation attestation",
        )
    report = read_json(path, label="Ghostlab scorer-run attestation")
    if not isinstance(report, Mapping):
        raise GhostlabContractError(
            "Ghostlab scorer-run attestation is not a JSON object",
            argv=argv,
            run_dir=context_dir,
        )
    if report.get("schema_version") != GHOSTLAB_SCORER_RUN_SCHEMA:
        raise GhostlabContractError(
            "Ghostlab scorer-run attestation does not declare "
            f"{GHOSTLAB_SCORER_RUN_SCHEMA!r}",
            argv=argv,
            run_dir=context_dir,
        )
    for key, expected in (
        ("task_id", task_id),
        ("attempt_id", attempt_id),
        ("status", status),
    ):
        actual = report.get(key)
        if actual != expected:
            raise GhostlabContractError(
                f"Ghostlab scorer-run attestation {key}={actual!r} does not match "
                f"the current run {expected!r}",
                argv=argv,
                run_dir=context_dir,
            )

    hashes = report.get("hashes")
    if not isinstance(hashes, Mapping):
        raise GhostlabContractError(
            "Ghostlab scorer-run attestation has no input hashes",
            argv=argv,
            run_dir=context_dir,
        )
    for key, expected in (
        ("task_sha256", task_sha256),
        ("scorer_package_sha256", scorer_package_sha256),
    ):
        actual = hashes.get(key)
        if actual != expected:
            raise GhostlabContractError(
                f"Ghostlab scorer-run attestation {key}={actual!r} does not match "
                f"the current input hash {expected}",
                argv=argv,
                run_dir=context_dir,
            )

    if mode not in SCORER_MODES:
        raise GhostlabContractError(
            f"cannot validate scorer isolation for unsupported mode {mode!r}",
            argv=argv,
            run_dir=context_dir,
        )
    isolation = report.get("isolation")
    required = {
        "schema_version": GHOSTLAB_SCORER_ISOLATION_SCHEMA,
        "scorer_launcher": "landlock",
        "candidate_mount": "read_only",
        "secure_exec_available": True,
    }
    expected_keys = set(required) | {"judge_launcher"}
    if mode == "deterministic":
        allowed_judge_launchers = {"not_run"}
    elif mode == "judge" or status == "scored":
        allowed_judge_launchers = {"landlock"}
    else:
        allowed_judge_launchers = {"landlock", "not_run"}
    if (
        not isinstance(isolation, Mapping)
        or set(isolation) != expected_keys
        or any(isolation.get(key) != value for key, value in required.items())
        or isolation.get("judge_launcher") not in allowed_judge_launchers
    ):
        raise GhostlabContractError(
            "Ghostlab scorer-run lacks the exact GHOSTLAB_SECURE_EXEC isolation "
            f"attestation required for mode {mode!r} and status {status!r}",
            argv=argv,
            run_dir=context_dir,
        )
    return dict(isolation)


def validate_score_report_contract(
    report: Mapping[str, Any],
    path: Path,
    *,
    argv: Sequence[str] = (),
    run_dir: Path | None = None,
) -> str:
    """Enforce ``schemas/score-report.schema.json`` and return the report status."""
    contract: Path | None
    try:
        contract = schema_path(SCORE_REPORT_CONTRACT)
        errors = packaged_contract_errors(report, SCORE_REPORT_CONTRACT, where=path.name)
    except GhostlabContractError:
        contract, errors = None, []
    if errors:
        raise GhostlabContractError(
            f"{path.name} violates {SCORE_REPORT_SCHEMA}: " + "; ".join(errors[:6]),
            argv=argv,
            run_dir=run_dir,
            hint=f"contract: {contract}",
        )
    status = report.get("status")
    if not isinstance(status, str) or status not in SCORE_REPORT_STATUSES:
        raise GhostlabContractError(
            f"score report has unsupported status {status!r}",
            argv=argv,
            run_dir=run_dir,
            hint=f"expected one of {sorted(SCORE_REPORT_STATUSES)}",
        )
    valid = report.get("valid")
    if not isinstance(valid, bool):
        raise GhostlabContractError(
            "score report valid must be a boolean",
            argv=argv,
            run_dir=run_dir,
        )
    for key in ("score_total", "pass_threshold", "unscored_weight"):
        value = report.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise GhostlabContractError(
                f"score report {key} must be a finite number within [0, 1] or null",
                argv=argv,
                run_dir=run_dir,
            )
    if status != "scored" and report.get("score_total") is not None:
        raise GhostlabContractError(
            f"score report status={status} must not carry score_total",
            argv=argv,
            run_dir=run_dir,
            hint="harness and scorer failures are never converted to a numeric zero",
        )
    if status != "scored" and valid:
        raise GhostlabContractError(
            f"score report status={status} must carry valid=false",
            argv=argv,
            run_dir=run_dir,
        )
    return status


def _hashable_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Drop wall-clock fields so identical work hashes identically."""
    volatile = {"started_at", "finished_at", "duration_ms", "run_dir"}
    return {key: value for key, value in report.items() if key not in volatile}


def _report_hash(report: Mapping[str, Any], key: str, fallback: str) -> str:
    value = report.get(key)
    return value if isinstance(value, str) and value else fallback


def _require_schema(
    payload: Mapping[str, Any],
    expected: str,
    path: Path,
    argv: Sequence[str],
    run_dir: Path | None,
) -> None:
    actual = payload.get("schema_version")
    if actual != expected:
        raise GhostlabContractError(
            f"{path.name} declares schema_version={actual!r}, expected {expected!r}",
            argv=argv,
            run_dir=run_dir,
            hint="Retro and Ghostlab must agree on the versioned JSON contract",
        )


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise GhostlabContractError(
            f"{label} is missing or not a file: {path}",
            hint="every Ghostlab invocation input must exist before the run starts",
        )


def _require_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise GhostlabContractError(
            f"{label} does not exist: {path}",
            hint="every Ghostlab invocation input must exist before the run starts",
        )


def _persist_streams(run_dir: Path, label: str, outcome: CommandOutcome) -> None:
    from ...utils import atomic_write_text

    safe = label.replace("/", "-")
    run_dir.mkdir(parents=True, exist_ok=True)
    for suffix, stream in (("stdout", outcome.stdout), ("stderr", outcome.stderr)):
        if stream:
            atomic_write_text(run_dir / f"{safe}.{suffix}.log", stream)


def _run_output_path(run_dir: Path, name: str, argv: Sequence[str]) -> Path:
    relative = Path(name)
    if not name or relative.is_absolute() or ".." in relative.parts:
        raise GhostlabContractError(
            f"Ghostlab output name must stay within the run directory: {name!r}",
            argv=argv,
            run_dir=run_dir,
        )
    path = run_dir / relative
    try:
        root = run_dir.resolve()
        resolved_parent = path.parent.resolve(strict=False)
    except OSError as exc:
        raise GhostlabContractError(
            f"could not resolve Ghostlab output path {path}: {exc}",
            argv=argv,
            run_dir=run_dir,
        ) from exc
    if path == run_dir or (resolved_parent != root and root not in resolved_parent.parents):
        raise GhostlabContractError(
            f"Ghostlab output name escapes the run directory: {name!r}",
            argv=argv,
            run_dir=run_dir,
        )
    return path


def _clear_expected_outputs(
    paths: Iterable[Path], *, argv: Sequence[str], run_dir: Path
) -> None:
    for path in dict.fromkeys(paths):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            raise GhostlabInvocationError(
                f"could not clear previous Ghostlab output {path}: {exc}",
                argv=argv,
                run_dir=run_dir,
                hint="remove the stale output or choose a writable run directory",
            ) from exc


def _require_unchanged_input(
    path: Path,
    expected_sha256: str,
    label: str,
    argv: Sequence[str],
    run_dir: Path,
    *,
    excludes: Sequence[str] = DEFAULT_TREE_EXCLUDES,
) -> None:
    try:
        actual_sha256 = sha256_path(path, excludes=excludes)
    except (GhostlabError, OSError) as exc:
        raise GhostlabContractError(
            f"Ghostlab {label} became unreadable during invocation: {path}",
            argv=argv,
            run_dir=run_dir,
        ) from exc
    if actual_sha256 != expected_sha256:
        raise GhostlabContractError(
            f"Ghostlab {label} changed during invocation; refusing output not bound "
            "to the hashed inputs",
            argv=argv,
            run_dir=run_dir,
        )


def iter_export_specs(values: Iterable[str]) -> tuple[ExportSpec, ...]:
    return tuple(ExportSpec.parse(value) for value in values)


__all__ = [
    "ARTIFACT_RUN_REPORT_NAME",
    "ARTIFACT_RUN_SCHEMA",
    "ARTIFACT_RUN_STATUSES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TREE_EXCLUDES",
    "GHOSTLAB_SCORER_ISOLATION_SCHEMA",
    "GHOSTLAB_SCORER_RUN_SCHEMA",
    "SCORER_AUDIT_CONTRACT",
    "SCORER_RUN_REPORT_NAME",
    "SCORE_INPUT_SCHEMA",
    "SCORE_REPORT_CONTRACT",
    "SCORE_REPORT_NAME",
    "SCORE_REPORT_SCHEMA",
    "SCORE_REPORT_STATUSES",
    "TASK_DEFINITIONS_CONTRACT",
    "ArtifactRunRequest",
    "ArtifactRunResult",
    "CommandOutcome",
    "CommandRunner",
    "ExportSpec",
    "GhostlabBinaryError",
    "GhostlabCli",
    "GhostlabContractError",
    "GhostlabError",
    "GhostlabInvocationError",
    "GhostlabTimeoutError",
    "GhostlabVersion",
    "ScorerRunRequest",
    "ScorerRunResult",
    "canonical_json",
    "iter_export_specs",
    "packaged_contract_errors",
    "read_json",
    "resolve_ghostlab_binary",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "sha256_path",
    "sha256_text",
    "schema_path",
    "tree_manifest",
    "validate_score_report_contract",
    "validate_scorer_run_attestation",
    "write_json",
]
