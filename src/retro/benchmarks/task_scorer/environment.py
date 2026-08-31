"""Project environment resolution for the rollout-to-task-and-scorer pipeline.

Implements spec §5.6 ("Project environment resolution"). Environment resolution
runs once per ``(repo_id, environment_fingerprint)``, before the TaskDefiner. It
is never left to each candidate agent's ambient developer setup.

Resolution priority (first usable source wins):

1. an explicit Retro project config supplied **outside** the evaluated repository;
2. a project ``Dockerfile``/devcontainer configuration present at ``base_sha``;
3. CI workflow commands extracted from the repository and validated in a
   caller-supplied, pinned container;
4. RepoLaunch as a fallback environment-building agent, invoked only through an
   explicit executable -- never an ambient ``PATH`` lookup.

Every source is verified the same way before it is trusted: the resolver
materializes fresh, clean checkouts of both ``base_sha`` and ``outcome_sha`` and
runs ``setup`` then ``smoke`` twice for each, through a pinned container image,
with network allowlisted only while building and disabled for every validation
run. Commands are always argv arrays; nothing here is ever executed through a
shell string. All effective inputs (source, base SHA, image digest, workdir,
command arrays, env, network policy, workspace excludes) are hashed into
``environment_id`` so identical inputs always produce identical output.

Subprocess execution and container operations are injected through the
:class:`ContainerRuntime` protocol and the ``command_runner``/``materializer``
callables so unit tests can run this entire module against fakes, with no
Docker daemon and no network egress.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import git_state
from .schema import PROJECT_ENVIRONMENT_SCHEMA, ProjectEnvironment, content_sha256

EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA = "retro-environment-config-v1"

#: Official devcontainer.json extensibility point (``customizations.<tool-id>``)
#: used to declare Retro's setup/smoke/test command arrays alongside a project
#: Dockerfile/devcontainer, since neither format has a native notion of "how do
#: I validate this project" on its own.
DEVCONTAINER_CUSTOMIZATION_KEY = "retro-task-scorer"

#: RepoLaunch is only ever invoked through this env var or an explicit path.
#: There is deliberately no ``shutil.which("repolaunch")`` ambient fallback.
REPOLAUNCH_BIN_ENV = "RETRO_REPOLAUNCH_BIN"

_PINNED_IMAGE_RE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
_DIGEST_SUFFIX_RE = re.compile(r"sha256:[0-9a-f]{64}$")
_SECRET_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL)", re.IGNORECASE)

_DEVCONTAINER_CANDIDATES: tuple[str, ...] = (".devcontainer/devcontainer.json", ".devcontainer.json")
_DOCKERFILE_CANDIDATES: tuple[str, ...] = ("Dockerfile", ".devcontainer/Dockerfile")

_TEST_LIKE_RE = re.compile(
    r"\b(pytest|py\.test|go\s+test|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|cargo\s+test|"
    r"mvn\S*\s+(-\S+\s+)*test\S*|gradlew?\s+test|make\s+test|tox\b|unittest\b|rspec\b|dotnet\s+test)\b",
    re.IGNORECASE,
)
_JSONC_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JSONC_LINE_COMMENT_RE = re.compile(r"(?<!:)//.*$", re.MULTILINE)
_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class EnvironmentError(RuntimeError):
    """Base class for every project-environment resolution failure."""


class UnpinnedImageError(EnvironmentError):
    """An image reference was not pinned by a ``sha256`` digest."""


class UnsafeCommandError(EnvironmentError):
    """A command was a shell string (or otherwise not an argv array)."""


class SecretMaterialRejectedError(EnvironmentError):
    """An environment declared a literal secret value instead of a bare name."""


class EnvironmentValidationError(EnvironmentError):
    """Setup/smoke validation failed, or would have to be skipped to "pass"."""


class RepoLaunchError(EnvironmentError):
    """The explicit RepoLaunch executable failed or returned an invalid contract."""


@dataclass(frozen=True)
class ResolutionAttempt:
    """One priority-order source that was tried, for diagnostics only."""

    source: str
    ok: bool
    detail: str


class EnvironmentUnavailableError(EnvironmentError):
    """No resolver source produced a validated environment.

    Mirrors the ``ENVIRONMENT_UNAVAILABLE`` rejection code from spec §14.4;
    callers that need the stable code should catch this (it is a ``RuntimeError``,
    matching the ``EnvironmentResolver`` contract in ``selection.py``) and map it
    themselves.
    """

    def __init__(self, message: str, *, attempts: Sequence[ResolutionAttempt] = ()) -> None:
        self.attempts = tuple(attempts)
        detail = "\n".join(f"  - {a.source}: {a.detail}" for a in self.attempts)
        super().__init__(f"{message}\n{detail}" if detail else message)


# ---------------------------------------------------------------------------
# candidate protocol (structurally compatible with selection.SourceCandidate)
# ---------------------------------------------------------------------------


@runtime_checkable
class EnvironmentCandidate(Protocol):
    """The subset of ``selection.SourceCandidate`` this module needs.

    Declared as a structural protocol (not imported from ``selection``) so this
    module has no hard dependency on the selection layer; anything exposing
    these three attributes -- a real ``SourceCandidate`` or a lightweight test
    double -- can be resolved.
    """

    @property
    def repo_root(self) -> Path: ...

    @property
    def base_sha(self) -> str: ...

    @property
    def outcome_sha(self) -> str: ...


# ---------------------------------------------------------------------------
# argv / secret / string-map validation
# ---------------------------------------------------------------------------


def _require_argv_commands(commands: Any, where: str) -> list[list[str]]:
    """Validate ``commands`` as a list of non-empty argv arrays.

    Rejects bare strings outright: a JSON string is iterable character-by-character
    in Python, which is exactly the "shell string masquerading as a command list"
    mistake this guards against.
    """
    if isinstance(commands, (str, bytes)) or not isinstance(commands, (list, tuple)):
        raise UnsafeCommandError(f"{where} must be a list of argv arrays, got {commands!r}")
    out: list[list[str]] = []
    for index, item in enumerate(commands):
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
            raise UnsafeCommandError(
                f"{where}[{index}] must be an argv array (list[str]), not a shell string: {item!r}"
            )
        if not item:
            raise UnsafeCommandError(f"{where}[{index}] must not be empty")
        argv: list[str] = []
        for position, token in enumerate(item):
            if not isinstance(token, str) or not token:
                raise UnsafeCommandError(f"{where}[{index}][{position}] must be a non-empty string")
            argv.append(token)
        out.append(argv)
    return out


def _require_string_map(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EnvironmentError(f"{where} must be an object")
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise EnvironmentError(f"{where}.{key!r} must map a string key to a string value")
        out[key] = val
    return out


def _require_string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EnvironmentError(f"{where} must be a list of strings")
    return list(value)


def _scrub_secrets(
    env: Mapping[str, str], declared_secret_names: Sequence[str], *, where: str
) -> dict[str, str]:
    """Enforce "secrets are referenced by name but not stored" (spec §5.6).

    Any env var whose name looks secret-shaped, or that was explicitly declared
    in ``secrets``, must carry an empty placeholder value. A non-empty value on
    such a key is rejected outright rather than silently dropped, so a leaked
    credential in a config file surfaces immediately instead of disappearing.
    """
    declared = set(declared_secret_names)
    out: dict[str, str] = dict(env)
    for key, value in env.items():
        looks_secret = key in declared or bool(_SECRET_NAME_RE.search(key))
        if looks_secret and value != "":
            raise SecretMaterialRejectedError(
                f"{where}.{key} looks like a secret; declare it in 'secrets' and leave the value "
                "empty instead of storing the literal value"
            )
    for name in declared:
        out.setdefault(name, "")
    return out


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# container runtime boundary (injectable; fakes never touch Docker/network)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class ImageBuildResult:
    image_ref: str
    digest: str
    logs: str = ""


#: ``"allowlisted"`` only while building an image; ``"disabled"`` for every run.
NetworkPolicy = str


class ContainerRuntime(Protocol):
    """Injectable boundary over the pinned container runtime.

    Unit tests supply a fake implementation; ``DockerContainerRuntime`` below is
    the best-effort default used outside tests when an actual container engine
    is installed.
    """

    def build_image(
        self,
        *,
        dockerfile: Path,
        context: Path,
        tags: Sequence[str],
        network: NetworkPolicy,
        network_allowlist: Sequence[str],
        build_args: Mapping[str, str],
    ) -> ImageBuildResult: ...

    def resolve_digest(self, image_ref: str) -> str:
        """Return the ``sha256:...`` digest the runtime would actually execute."""
        ...

    def run(
        self,
        *,
        image: str,
        argv: Sequence[str],
        workdir: str,
        env: Mapping[str, str],
        network: NetworkPolicy,
        mounts: Sequence[Mount],
        timeout: float | None,
    ) -> CommandResult: ...


CommandRunner = Callable[[Sequence[str], "float | None"], CommandResult]


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def default_command_runner(argv: Sequence[str], timeout: float | None) -> CommandResult:
    """Real subprocess execution. Never used by unit tests; they inject a fake."""
    started = time.monotonic()
    try:
        completed = subprocess.run(list(argv), text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=tuple(argv),
            exit_code=124,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    return CommandResult(
        argv=tuple(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=False,
    )


@dataclass
class DockerContainerRuntime:
    """Best-effort default :class:`ContainerRuntime` backed by a real ``docker``
    (or API-compatible) binary.

    Builds default to ``--network none``. A non-empty destination allowlist is
    accepted only with an explicitly configured, externally egress-filtered
    Docker network; plain bridge networking is never mislabeled as allowlisted.
    """

    binary: str = "docker"
    command_runner: CommandRunner = default_command_runner
    build_timeout: float | None = 1800.0
    allowlisted_network: str | None = None

    def build_image(
        self,
        *,
        dockerfile: Path,
        context: Path,
        tags: Sequence[str],
        network: NetworkPolicy,
        network_allowlist: Sequence[str],
        build_args: Mapping[str, str],
    ) -> ImageBuildResult:
        if network == "disabled" or not network_allowlist:
            docker_network = "none"
        elif network == "allowlisted" and self.allowlisted_network:
            docker_network = self.allowlisted_network
        else:
            raise EnvironmentValidationError(
                "Docker cannot enforce the requested build-network allowlist without "
                "an explicitly configured egress-filtered network"
            )
        argv = [self.binary, "build", "--network", docker_network, "-f", str(dockerfile)]
        for key, value in build_args.items():
            argv += ["--build-arg", f"{key}={value}"]
        for tag in tags:
            argv += ["-t", tag]
        argv.append(str(context))
        result = self.command_runner(argv, self.build_timeout)
        if not result.ok:
            raise EnvironmentValidationError(
                f"container image build failed (exit={result.exit_code}): {result.stderr or result.stdout}"
            )
        digest = self.resolve_digest(tags[0])
        return ImageBuildResult(image_ref=tags[0], digest=digest, logs=result.stdout)

    def resolve_digest(self, image_ref: str) -> str:
        argv = [self.binary, "image", "inspect", image_ref, "--format", "{{index .RepoDigests 0}}"]
        result = self.command_runner(argv, 30.0)
        if not result.ok:
            raise EnvironmentValidationError(
                f"could not resolve a digest for {image_ref!r}: {result.stderr or result.stdout}"
            )
        output = result.stdout.strip()
        match = _DIGEST_SUFFIX_RE.search(output)
        if not match:
            raise EnvironmentValidationError(
                f"docker did not report a sha256 digest for {image_ref!r}: {output!r}"
            )
        return match.group(0)

    def run(
        self,
        *,
        image: str,
        argv: Sequence[str],
        workdir: str,
        env: Mapping[str, str],
        network: NetworkPolicy,
        mounts: Sequence[Mount],
        timeout: float | None,
    ) -> CommandResult:
        if network != "disabled":
            raise EnvironmentValidationError("project validation runs must disable network access")
        full_argv = [
            self.binary,
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "",
            "-w",
            workdir,
        ]
        for mount in mounts:
            suffix = ":ro" if mount.read_only else ""
            full_argv += ["-v", f"{mount.source}:{mount.target}{suffix}"]
        for key, value in env.items():
            full_argv += ["-e", f"{key}={value}"]
        full_argv.append(image)
        full_argv += list(argv)
        return self.command_runner(full_argv, timeout)


# ---------------------------------------------------------------------------
# materialization boundary
# ---------------------------------------------------------------------------

#: Matches ``git_state.materialize_tree(root, sha, dest) -> dest``. Injectable so
#: tests never need real ``git`` archive extraction if they choose not to use it,
#: though the default and the tests in this module both exercise real Git.
Materializer = Callable[[Path, str, Path], Path]


# ---------------------------------------------------------------------------
# environment spec: fully-discovered, not-yet-validated command/image intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildSpec:
    dockerfile: Path
    context: Path
    build_args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvironmentSpec:
    """Resolved-but-not-yet-validated environment intent for one priority source."""

    source: str
    workdir: str
    setup: list[list[str]]
    smoke: list[list[str]]
    test: list[list[str]]
    env: dict[str, str] = field(default_factory=dict)
    workspace_excludes: list[str] = field(default_factory=list)
    image: str | None = None
    build: BuildSpec | None = None


# ---------------------------------------------------------------------------
# shared parsing: explicit config and RepoLaunch output share one contract
# ---------------------------------------------------------------------------

_ENVIRONMENT_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "workdir",
        "image",
        "build",
        "setup",
        "smoke",
        "test",
        "env",
        "workspace_excludes",
        "secrets",
    }
)


def _environment_spec_from_payload(payload: Any, *, source: str, where: str) -> EnvironmentSpec:
    if not isinstance(payload, Mapping):
        raise EnvironmentError(f"{where} must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA:
        raise EnvironmentError(
            f"{where}.schema_version must be {EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA!r}, got {schema_version!r}"
        )
    unknown = sorted(set(payload) - _ENVIRONMENT_CONFIG_KEYS)
    if unknown:
        raise EnvironmentError(f"{where} has unknown keys: {', '.join(unknown)}")

    workdir = payload.get("workdir")
    if not isinstance(workdir, str) or not workdir.strip():
        raise EnvironmentError(f"{where}.workdir must be a non-empty string")

    image_raw = payload.get("image")
    build_raw = payload.get("build")
    if image_raw is not None and build_raw is not None:
        raise EnvironmentError(f"{where} must declare either 'image' or 'build', not both")
    if image_raw is None and build_raw is None:
        raise EnvironmentError(f"{where} must declare an 'image' or a 'build' spec")

    image: str | None = None
    build_spec: BuildSpec | None = None
    if build_raw is not None:
        if not isinstance(build_raw, Mapping):
            raise EnvironmentError(f"{where}.build must be an object")
        dockerfile = build_raw.get("dockerfile")
        if not isinstance(dockerfile, str) or not dockerfile.strip():
            raise EnvironmentError(f"{where}.build.dockerfile must be a non-empty string")
        context = build_raw.get("context", ".")
        if not isinstance(context, str) or not context.strip():
            raise EnvironmentError(f"{where}.build.context must be a non-empty string")
        build_args = _require_string_map(build_raw.get("args", {}), f"{where}.build.args")
        build_spec = BuildSpec(dockerfile=Path(dockerfile), context=Path(context), build_args=build_args)
    else:
        assert image_raw is not None
        if not isinstance(image_raw, str) or not image_raw.strip():
            raise EnvironmentError(f"{where}.image must be a non-empty string")
        if not _PINNED_IMAGE_RE.fullmatch(image_raw):
            raise UnpinnedImageError(f"{where}.image must be pinned by a sha256 digest, got {image_raw!r}")
        image = image_raw

    setup = _require_argv_commands(payload.get("setup", []), f"{where}.setup")
    smoke = _require_argv_commands(payload.get("smoke", []), f"{where}.smoke")
    test = _require_argv_commands(payload.get("test", payload.get("smoke", [])), f"{where}.test")
    env = _require_string_map(payload.get("env", {}), f"{where}.env")
    secrets = _require_string_list(payload.get("secrets", []), f"{where}.secrets")
    env = _scrub_secrets(env, secrets, where=f"{where}.env")
    workspace_excludes = _require_string_list(
        payload.get("workspace_excludes", []), f"{where}.workspace_excludes"
    )

    return EnvironmentSpec(
        source=source,
        workdir=workdir,
        setup=setup,
        smoke=smoke,
        test=test,
        env=env,
        workspace_excludes=workspace_excludes,
        image=image,
        build=build_spec,
    )


def load_explicit_config(path: Path, *, repo_root: Path) -> EnvironmentSpec:
    """Priority 1: an explicit Retro project config supplied *outside* the repo.

    The path is rejected if it lives inside the evaluated repository -- an
    in-repo config could be crafted by the very rollout under evaluation, which
    defeats the point of an "explicit, externally supplied" trust boundary.
    """
    path = Path(path)
    if _is_within(path.resolve(), Path(repo_root).resolve()):
        raise EnvironmentError(
            f"explicit environment config must be supplied outside the evaluated repository, "
            f"got {path} inside {repo_root}"
        )
    if not path.is_file():
        raise EnvironmentError(f"explicit environment config does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EnvironmentError(f"{path}: invalid JSON: {error}") from error
    return _environment_spec_from_payload(payload, source="explicit", where=str(path))


# ---------------------------------------------------------------------------
# priority 2: Dockerfile / devcontainer at base_sha
# ---------------------------------------------------------------------------


def _read_file_at_ref(repo_root: Path, ref: str, path: str) -> str | None:
    process = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        return None
    return process.stdout


def _path_exists_at_ref(repo_root: Path, ref: str, path: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    return process.returncode == 0


def _parse_jsonc(text: str) -> dict[str, Any]:
    """Minimal JSONC support (``//``/``/* */`` comments, trailing commas).

    devcontainer.json is conventionally JSONC. This is a deliberately small
    subset -- it does not tokenize strings, so a literal ``//`` inside a JSON
    string value that is not itself a URL could be mis-stripped -- adequate for
    the well-formed devcontainer files this resolver needs to read.
    """
    without_block = _JSONC_BLOCK_COMMENT_RE.sub("", text)
    without_line = _JSONC_LINE_COMMENT_RE.sub("", without_block)
    without_trailing_commas = _JSONC_TRAILING_COMMA_RE.sub(r"\1", without_line)
    payload = json.loads(without_trailing_commas)
    if not isinstance(payload, dict):
        raise ValueError("devcontainer.json must be a JSON object")
    return payload


def discover_project_container(repo_root: Path, base_sha: str) -> EnvironmentSpec | None:
    """Priority 2: a Dockerfile or devcontainer configuration present at ``base_sha``.

    A bare Dockerfile with no accompanying ``customizations.retro-task-scorer``
    block cannot prove ``setup``/``smoke`` on its own, so discovery returns
    ``None`` (cascade to CI-derived) rather than guessing commands.
    """
    devcontainer_path: str | None = None
    devcontainer_payload: dict[str, Any] | None = None
    for candidate in _DEVCONTAINER_CANDIDATES:
        text = _read_file_at_ref(repo_root, base_sha, candidate)
        if text is None:
            continue
        try:
            devcontainer_payload = _parse_jsonc(text)
        except (ValueError, json.JSONDecodeError):
            devcontainer_payload = None
        devcontainer_path = candidate
        break

    dockerfile_rel: str | None = None
    if devcontainer_payload is not None:
        build_block = devcontainer_payload.get("build")
        declared = None
        if isinstance(build_block, Mapping):
            declared = build_block.get("dockerfile")
        if declared is None:
            declared = devcontainer_payload.get("dockerFile")
        if isinstance(declared, str) and declared.strip():
            base_dir = Path(devcontainer_path or ".").parent
            combined = base_dir / declared if str(base_dir) != "." else Path(declared)
            # normalize so a devcontainer-relative "../Dockerfile" resolves to a
            # plain repo-relative path git can address directly (git does not
            # normalize ".." components itself in `git show ref:path`).
            dockerfile_rel = os.path.normpath(str(combined))
    if dockerfile_rel is None:
        for candidate in _DOCKERFILE_CANDIDATES:
            if _path_exists_at_ref(repo_root, base_sha, candidate):
                dockerfile_rel = candidate
                break
    if dockerfile_rel is None:
        return None

    customization: Mapping[str, Any] | None = None
    if devcontainer_payload is not None:
        customizations = devcontainer_payload.get("customizations")
        if isinstance(customizations, Mapping):
            block = customizations.get(DEVCONTAINER_CUSTOMIZATION_KEY)
            if isinstance(block, Mapping):
                customization = block
    if customization is None:
        return None

    where = f"devcontainer.customizations.{DEVCONTAINER_CUSTOMIZATION_KEY}"
    setup = _require_argv_commands(customization.get("setup", []), f"{where}.setup")
    smoke = _require_argv_commands(customization.get("smoke", []), f"{where}.smoke")
    test = _require_argv_commands(customization.get("test", customization.get("smoke", [])), f"{where}.test")
    workdir = customization.get("workdir", "/workspace/repo")
    if not isinstance(workdir, str) or not workdir.strip():
        raise EnvironmentError(f"{where}.workdir must be a non-empty string")
    env = _require_string_map(customization.get("env", {}), f"{where}.env")
    secrets = _require_string_list(customization.get("secrets", []), f"{where}.secrets")
    env = _scrub_secrets(env, secrets, where=f"{where}.env")
    workspace_excludes = _require_string_list(
        customization.get("workspace_excludes", []), f"{where}.workspace_excludes"
    )

    build_args: dict[str, str] = {}
    if devcontainer_payload is not None:
        build_block = devcontainer_payload.get("build")
        if isinstance(build_block, Mapping) and isinstance(build_block.get("args"), Mapping):
            build_args = _require_string_map(build_block["args"], "devcontainer.build.args")

    return EnvironmentSpec(
        source="project_container",
        workdir=workdir,
        setup=setup,
        smoke=smoke,
        test=test,
        env=env,
        workspace_excludes=workspace_excludes,
        image=None,
        build=BuildSpec(dockerfile=Path(dockerfile_rel), context=Path("."), build_args=build_args),
    )


# ---------------------------------------------------------------------------
# priority 3: CI-derived commands, validated in a caller-pinned container
# ---------------------------------------------------------------------------


def _list_workflow_paths(repo_root: Path, ref: str) -> list[str]:
    process = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", ref, "--", ".github/workflows"],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        return []
    paths = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    return sorted(p for p in paths if p.endswith((".yml", ".yaml")))


def _extract_run_steps(text: str) -> list[str]:
    """Extract GitHub Actions ``run:`` step command text, in file order.

    This is a deliberately small line-oriented extractor (inline ``run: cmd``
    and block-scalar ``run: |``/``run: >`` forms) rather than a full YAML
    parser -- the repository has no YAML dependency, and CI-derived discovery
    is documented as a scope-limited heuristic layer, not a general workflow
    interpreter.
    """
    lines = text.splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        match = re.match(r"^-?\s*run:\s*(.*)$", stripped)
        if not match:
            index += 1
            continue
        rest = match.group(1).strip()
        if rest in ("|", ">", "|-", ">-", "|+", ">+", ""):
            base_indent = len(line) - len(line.lstrip(" "))
            raw_block_lines: list[str] = []
            cursor = index + 1
            while cursor < len(lines):
                candidate_line = lines[cursor]
                if candidate_line.strip() == "":
                    raw_block_lines.append("")
                    cursor += 1
                    continue
                candidate_indent = len(candidate_line) - len(candidate_line.lstrip(" "))
                if candidate_indent <= base_indent:
                    break
                raw_block_lines.append(candidate_line)
                cursor += 1
            # Dedent by the first content line's own indentation, per YAML
            # block-scalar rules -- not the "run:" line's indentation, which is
            # shallower than the block content itself.
            content_indent = next(
                (
                    len(text_line) - len(text_line.lstrip(" "))
                    for text_line in raw_block_lines
                    if text_line.strip()
                ),
                base_indent,
            )
            block_lines = [
                text_line[content_indent:] if text_line.strip() else "" for text_line in raw_block_lines
            ]
            command = "\n".join(block_lines).strip()
            if command:
                commands.append(command)
            index = cursor
            continue
        commands.append(rest.strip("'\""))
        index += 1
    return commands


def _split_setup_and_test(commands: Sequence[str]) -> tuple[list[str], list[str]]:
    setup: list[str] = []
    test: list[str] = []
    for command in commands:
        if _TEST_LIKE_RE.search(command):
            test.append(command)
        elif not test:
            setup.append(command)
    return setup, test


def discover_ci_commands(
    repo_root: Path, base_sha: str, *, ci_base_image: str | None
) -> EnvironmentSpec | None:
    """Priority 3: commands extracted from ``.github/workflows`` at ``base_sha``.

    CI-derived discovery only extracts *commands*; it never guesses the runtime
    image the CI provider used. Callers must supply ``ci_base_image`` already
    pinned by digest, or this source is treated as unavailable (cascade to
    RepoLaunch) rather than silently picking an ambient/default image.
    """
    if ci_base_image is None:
        return None
    if not _PINNED_IMAGE_RE.fullmatch(ci_base_image):
        raise UnpinnedImageError(f"ci_base_image must be pinned by a sha256 digest, got {ci_base_image!r}")
    for workflow_path in _list_workflow_paths(repo_root, base_sha):
        text = _read_file_at_ref(repo_root, base_sha, workflow_path)
        if text is None:
            continue
        commands = _extract_run_steps(text)
        setup_cmds, test_cmds = _split_setup_and_test(commands)
        if not test_cmds:
            continue
        setup = [["bash", "-lc", command] for command in setup_cmds]
        test = [["bash", "-lc", command] for command in test_cmds]
        return EnvironmentSpec(
            source="ci_derived",
            workdir="/workspace/repo",
            setup=setup,
            smoke=test,
            test=test,
            env={},
            workspace_excludes=[],
            image=ci_base_image,
            build=None,
        )
    return None


# ---------------------------------------------------------------------------
# priority 4: RepoLaunch, only through an explicit executable
# ---------------------------------------------------------------------------


def resolve_repolaunch_binary(
    explicit: str | Path | None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Resolve the RepoLaunch executable from an explicit path or dedicated env var.

    Deliberately never searches ``PATH`` for an ambient ``repolaunch`` binary:
    the spec requires this fallback be invoked "via an explicit executable",
    never a silent ambient one. Returns ``None`` (not a raise) when nothing was
    configured, so callers can cleanly treat RepoLaunch as "not attempted".
    """
    environ = os.environ if env is None else env
    candidate = explicit or environ.get(REPOLAUNCH_BIN_ENV)
    if not candidate:
        return None
    path = Path(str(candidate)).expanduser()
    if not path.is_file():
        raise RepoLaunchError(f"repolaunch binary does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise RepoLaunchError(f"repolaunch binary is not executable: {path}")
    return str(path)


def invoke_repolaunch(
    binary: str,
    *,
    repo_root: Path,
    base_sha: str,
    materializer: Materializer,
    command_runner: CommandRunner,
    timeout: float = 900.0,
) -> EnvironmentSpec:
    """Invoke the explicit RepoLaunch executable and schema-validate its output."""
    with tempfile.TemporaryDirectory(prefix="retro-repolaunch-") as scratch:
        scratch_path = Path(scratch)
        repo_copy = scratch_path / "repo"
        materializer(repo_root, base_sha, repo_copy)
        output_path = scratch_path / "environment.json"
        argv = [
            binary,
            "resolve-environment",
            "--repo",
            str(repo_copy),
            "--base-sha",
            base_sha,
            "--output",
            str(output_path),
        ]
        result = command_runner(argv, timeout)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = " (timed out)" if result.timed_out else ""
            raise RepoLaunchError(f"repolaunch failed (exit={result.exit_code}){suffix}: {detail}")
        if not output_path.is_file():
            raise RepoLaunchError("repolaunch did not write an environment contract to --output")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RepoLaunchError(f"repolaunch produced invalid JSON: {error}") from error
        return _environment_spec_from_payload(payload, source="repolaunch", where="repolaunch output")


# ---------------------------------------------------------------------------
# image resolution (verify/record digest; reject unpinned images)
# ---------------------------------------------------------------------------


def _resolve_image(
    spec: EnvironmentSpec,
    candidate: EnvironmentCandidate,
    *,
    runtime: ContainerRuntime,
    network_allowlist: Sequence[str],
    build_args: Mapping[str, str],
    materializer: Materializer,
    scratch_root: Path,
) -> tuple[str, str]:
    if spec.build is None:
        assert spec.image is not None
        resolved = runtime.resolve_digest(spec.image)
        digest_match = _DIGEST_SUFFIX_RE.search(resolved)
        if digest_match is None:
            raise EnvironmentValidationError(
                f"container runtime did not resolve a sha256 digest for {spec.image!r}, got {resolved!r}"
            )
        digest = digest_match.group(0)
        declared_digest = _DIGEST_SUFFIX_RE.search(spec.image)
        if declared_digest is not None and declared_digest.group(0) != digest:
            raise EnvironmentValidationError(
                f"declared image digest does not match what the runtime resolves: "
                f"declared={spec.image!r} resolved={digest!r}"
            )
        return spec.image, digest

    context_root = scratch_root / "build-context"
    materializer(candidate.repo_root, candidate.base_sha, context_root)
    checkout_root = context_root.resolve()
    dockerfile_path = (
        spec.build.dockerfile
        if spec.build.dockerfile.is_absolute()
        else checkout_root / spec.build.dockerfile
    ).resolve()
    build_context = (
        spec.build.context if spec.build.context.is_absolute() else checkout_root / spec.build.context
    ).resolve()
    if not _is_within(dockerfile_path, checkout_root):
        raise EnvironmentValidationError(
            f"Dockerfile resolves outside materialized base checkout: {spec.build.dockerfile}"
        )
    if not _is_within(build_context, checkout_root):
        raise EnvironmentValidationError(
            f"build context resolves outside materialized base checkout: {spec.build.context}"
        )
    if not dockerfile_path.is_file():
        raise EnvironmentValidationError(
            f"Dockerfile not found in materialized base checkout: {spec.build.dockerfile}"
        )
    if not build_context.is_dir():
        raise EnvironmentValidationError(
            f"build context not found in materialized base checkout: {spec.build.context}"
        )
    tag_seed = content_sha256(
        {"base_sha": candidate.base_sha, "dockerfile": str(spec.build.dockerfile), "source": spec.source}
    )
    tag = f"retro-task-scorer:{tag_seed[:16]}"
    merged_build_args = {**dict(build_args), **spec.build.build_args}
    result = runtime.build_image(
        dockerfile=dockerfile_path,
        context=build_context,
        tags=[tag],
        network="allowlisted" if network_allowlist else "disabled",
        network_allowlist=tuple(network_allowlist),
        build_args=merged_build_args,
    )
    digest_match = _DIGEST_SUFFIX_RE.search(result.digest)
    if digest_match is None:
        raise EnvironmentValidationError(f"container build did not report a sha256 digest: {result.digest!r}")
    digest = digest_match.group(0)
    image_digest = _DIGEST_SUFFIX_RE.search(result.image_ref)
    if image_digest is not None and image_digest.group(0) != digest:
        raise EnvironmentValidationError(
            "built image reference digest does not match the reported build digest: "
            f"image={result.image_ref!r} digest={digest!r}"
        )
    canonical = (
        result.image_ref
        if image_digest is not None
        else f"{result.image_ref}@{digest}"
    )
    return canonical, digest


# ---------------------------------------------------------------------------
# validation: two clean runs of setup + smoke, for base and outcome
# ---------------------------------------------------------------------------


def _write_validation_log(
    logs_root: Path,
    *,
    state: str,
    run_index: int,
    phase: str,
    command_index: int,
    argv: Sequence[str],
    result: CommandResult,
) -> None:
    log_path = logs_root / f"{state}-run{run_index + 1}-{phase}-{command_index}.log"
    content = (
        f"argv: {list(argv)!r}\n"
        f"exit_code: {result.exit_code}\n"
        f"timed_out: {result.timed_out}\n"
        f"duration_ms: {result.duration_ms}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
    log_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass


def _validate_source(
    candidate: EnvironmentCandidate,
    spec: EnvironmentSpec,
    image: str,
    *,
    runtime: ContainerRuntime,
    materializer: Materializer,
    logs_root: Path,
    validation_runs: int,
    run_timeout: float | None,
) -> dict[str, Any]:
    """Materialize base/outcome cleanly and run setup+smoke ``validation_runs`` times each.

    Never marks a source valid without actually executing something: a source
    with no smoke command is a design that cannot prove itself, and is rejected
    rather than silently recorded as validated.
    """
    if not spec.smoke:
        raise EnvironmentValidationError(
            "environment declares no smoke command; skipped validation is never recorded as success"
        )
    if validation_runs < 2:
        raise EnvironmentValidationError("validation_runs must be at least 2 (spec §5.6 two-run gate)")

    logs_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(logs_root, 0o700)
    except OSError:
        pass

    for state_name, sha in (("base", candidate.base_sha), ("outcome", candidate.outcome_sha)):
        for run_index in range(validation_runs):
            with tempfile.TemporaryDirectory(prefix=f"retro-env-{state_name}-{run_index}-") as scratch:
                workspace = Path(scratch) / "workspace"
                materializer(candidate.repo_root, sha, workspace)
                mounts = [Mount(source=workspace, target=spec.workdir, read_only=False)]
                for phase, commands in (("setup", spec.setup), ("smoke", spec.smoke)):
                    for command_index, argv in enumerate(commands):
                        result = runtime.run(
                            image=image,
                            argv=argv,
                            workdir=spec.workdir,
                            env=spec.env,
                            network="disabled",
                            mounts=mounts,
                            timeout=run_timeout,
                        )
                        _write_validation_log(
                            logs_root,
                            state=state_name,
                            run_index=run_index,
                            phase=phase,
                            command_index=command_index,
                            argv=argv,
                            result=result,
                        )
                        if not result.ok:
                            timeout_note = " [timed out]" if result.timed_out else ""
                            raise EnvironmentValidationError(
                                f"{phase} command failed during {state_name} validation run "
                                f"{run_index + 1}/{validation_runs}: {' '.join(argv)} "
                                f"(exit={result.exit_code}){timeout_note}"
                            )
    return {"base": True, "outcome": True, "runs": validation_runs}


# ---------------------------------------------------------------------------
# environment_id: hash all effective inputs
# ---------------------------------------------------------------------------


def compute_environment_id(
    *,
    source: str,
    base_sha: str,
    image: str,
    workdir: str,
    setup: Sequence[Sequence[str]],
    smoke: Sequence[Sequence[str]],
    test: Sequence[Sequence[str]],
    env: Mapping[str, str],
    workspace_excludes: Sequence[str],
    network_allowlist: Sequence[str] = (),
    network_during_build: str = "allowlisted",
    network_during_run: str = "disabled",
) -> str:
    material = {
        "schema_version": PROJECT_ENVIRONMENT_SCHEMA,
        "source": source,
        "base_sha": base_sha,
        "image": image,
        "workdir": workdir,
        "setup": [list(argv) for argv in setup],
        "smoke": [list(argv) for argv in smoke],
        "test": [list(argv) for argv in test],
        "env": dict(sorted(env.items())),
        "network_during_build": network_during_build,
        "network_allowlist": sorted(network_allowlist),
        "network_during_run": network_during_run,
        "workspace_excludes": sorted(workspace_excludes),
    }
    return f"sha256:{content_sha256(material)}"


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def resolve_environment(
    candidate: EnvironmentCandidate,
    *,
    runtime: ContainerRuntime,
    explicit_config: Path | str | None = None,
    repolaunch_binary: str | Path | None = None,
    repolaunch_env: Mapping[str, str] | None = None,
    ci_base_image: str | None = None,
    network_allowlist: Sequence[str] = (),
    build_args: Mapping[str, str] | None = None,
    logs_dir: Path | None = None,
    materializer: Materializer = git_state.materialize_tree,
    command_runner: CommandRunner | None = None,
    validation_runs: int = 2,
    run_timeout: float | None = 900.0,
) -> ProjectEnvironment:
    """Resolve, build, and validate a :class:`ProjectEnvironment` for ``candidate``.

    Tries each priority-2 source in order (explicit config, Dockerfile/devcontainer,
    CI-derived, RepoLaunch) and returns the first one that produces a schema-valid
    command set and passes two clean setup+smoke runs against both ``base_sha`` and
    ``outcome_sha``. Raises :class:`EnvironmentUnavailableError` (a ``RuntimeError``)
    if every source is unavailable or fails validation -- callers map that onto the
    ``ENVIRONMENT_UNAVAILABLE`` rejection code themselves; this module never returns
    an unvalidated environment and never silently falls back to ambient developer
    setup.
    """
    runner = command_runner or default_command_runner
    resolved_build_args = dict(build_args or {})
    attempts: list[ResolutionAttempt] = []
    if validation_runs < 2:
        raise EnvironmentValidationError("validation_runs must be at least 2 (spec §5.6 two-run gate)")

    with tempfile.TemporaryDirectory(prefix="retro-environment-scratch-") as scratch_str:
        scratch_root = Path(scratch_str)

        attempt_index = 0

        def validate(spec: EnvironmentSpec) -> ProjectEnvironment | None:
            nonlocal attempt_index
            attempt_root = scratch_root / f"{attempt_index:02d}-{spec.source}"
            attempt_index += 1
            try:
                image, _digest = _resolve_image(
                    spec,
                    candidate,
                    runtime=runtime,
                    network_allowlist=network_allowlist,
                    build_args=resolved_build_args,
                    materializer=materializer,
                    scratch_root=attempt_root,
                )

                logs_root = logs_dir if logs_dir is not None else attempt_root / "logs"
                validated = _validate_source(
                    candidate,
                    spec,
                    image,
                    runtime=runtime,
                    materializer=materializer,
                    logs_root=logs_root,
                    validation_runs=validation_runs,
                    run_timeout=run_timeout,
                )

                build_network_policy = (
                    "allowlisted" if spec.build is not None and network_allowlist else "disabled"
                )
                environment_id = compute_environment_id(
                    source=spec.source,
                    base_sha=candidate.base_sha,
                    image=image,
                    workdir=spec.workdir,
                    setup=spec.setup,
                    smoke=spec.smoke,
                    test=spec.test,
                    env=spec.env,
                    workspace_excludes=spec.workspace_excludes,
                    network_allowlist=network_allowlist,
                    network_during_build=build_network_policy,
                )

                return ProjectEnvironment(
                    environment_id=environment_id,
                    source=spec.source,
                    base_sha=candidate.base_sha,
                    image=image,
                    workdir=spec.workdir,
                    setup=spec.setup,
                    smoke=spec.smoke,
                    test=spec.test,
                    env=spec.env,
                    network_during_build=build_network_policy,
                    network_during_run="disabled",
                    workspace_excludes=spec.workspace_excludes,
                    validated=validated,
                )
            except (OSError, RuntimeError, ValueError) as error:
                attempts.append(ResolutionAttempt(spec.source, False, str(error)))
                return None

        if explicit_config is not None:
            try:
                explicit_spec = load_explicit_config(
                    Path(explicit_config), repo_root=candidate.repo_root
                )
            except EnvironmentError as error:
                attempts.append(ResolutionAttempt("explicit", False, str(error)))
            else:
                resolved = validate(explicit_spec)
                if resolved is not None:
                    return resolved

        try:
            project_spec = discover_project_container(candidate.repo_root, candidate.base_sha)
            if project_spec is None:
                attempts.append(
                    ResolutionAttempt(
                        "project_container",
                        False,
                        "no Dockerfile/devcontainer with Retro customizations found",
                    )
                )
        except EnvironmentError as error:
            attempts.append(ResolutionAttempt("project_container", False, str(error)))
        else:
            if project_spec is not None:
                resolved = validate(project_spec)
                if resolved is not None:
                    return resolved

        try:
            ci_spec = discover_ci_commands(
                candidate.repo_root, candidate.base_sha, ci_base_image=ci_base_image
            )
            if ci_spec is None:
                attempts.append(
                    ResolutionAttempt(
                        "ci_derived", False, "no usable CI workflow test command found"
                    )
                )
        except EnvironmentError as error:
            attempts.append(ResolutionAttempt("ci_derived", False, str(error)))
        else:
            if ci_spec is not None:
                resolved = validate(ci_spec)
                if resolved is not None:
                    return resolved

        try:
            binary = resolve_repolaunch_binary(repolaunch_binary, env=repolaunch_env)
        except EnvironmentError as error:
            attempts.append(ResolutionAttempt("repolaunch", False, str(error)))
        else:
            if binary is None:
                attempts.append(
                    ResolutionAttempt(
                        "repolaunch", False, "no explicit RepoLaunch executable configured"
                    )
                )
            else:
                try:
                    repolaunch_spec = invoke_repolaunch(
                        binary,
                        repo_root=candidate.repo_root,
                        base_sha=candidate.base_sha,
                        materializer=materializer,
                        command_runner=runner,
                    )
                except EnvironmentError as error:
                    attempts.append(ResolutionAttempt("repolaunch", False, str(error)))
                else:
                    resolved = validate(repolaunch_spec)
                    if resolved is not None:
                        return resolved

        raise EnvironmentUnavailableError(
            "no project environment source produced a validated configuration", attempts=attempts
        )


__all__ = [
    "EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA",
    "DEVCONTAINER_CUSTOMIZATION_KEY",
    "REPOLAUNCH_BIN_ENV",
    "EnvironmentError",
    "UnpinnedImageError",
    "UnsafeCommandError",
    "SecretMaterialRejectedError",
    "EnvironmentValidationError",
    "RepoLaunchError",
    "EnvironmentUnavailableError",
    "ResolutionAttempt",
    "EnvironmentCandidate",
    "Mount",
    "CommandResult",
    "ImageBuildResult",
    "NetworkPolicy",
    "ContainerRuntime",
    "CommandRunner",
    "default_command_runner",
    "DockerContainerRuntime",
    "Materializer",
    "BuildSpec",
    "EnvironmentSpec",
    "load_explicit_config",
    "discover_project_container",
    "discover_ci_commands",
    "resolve_repolaunch_binary",
    "invoke_repolaunch",
    "compute_environment_id",
    "resolve_environment",
]
