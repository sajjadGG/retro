"""Tests for project-environment resolution (spec §5.6, §18.1).

Every test here runs against fakes: :class:`FakeContainerRuntime` stands in for
a real container engine, so nothing needs Docker or network access. A handful
of tests exercise real ``git`` materialization (via ``git_state.materialize_tree``,
already used throughout the pipeline's test suite) to prove the "fresh, clean
checkout per validation run" contract for real, without touching a container
runtime.
"""
from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import environment, git_state
from tests.task_scorer_helpers import git, make_repo

pytestmark = pytest.mark.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """Minimal ``EnvironmentCandidate``-shaped test double."""

    repo_root: Path
    base_sha: str
    outcome_sha: str


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    return root, make_repo(root)


@pytest.fixture
def candidate(repo: tuple[Path, dict[str, str]]) -> _Candidate:
    root, shas = repo
    return _Candidate(repo_root=root, base_sha=shas["base_sha"], outcome_sha=shas["outcome_sha"])


PINNED_IMAGE = "demo@sha256:" + "a" * 64
BUILD_DIGEST = "sha256:" + "b" * 64


class FakeContainerRuntime:
    """Records every call; never touches Docker or the network."""

    def __init__(self, *, run_ok: bool = True, build_digest: str = BUILD_DIGEST) -> None:
        self.build_calls: list[dict] = []
        self.run_calls: list[dict] = []
        self.digest_calls: list[str] = []
        self.run_ok = run_ok
        self.build_digest = build_digest
        self.failing_argv: set[tuple[str, ...]] = set()

    def build_image(self, *, dockerfile, context, tags, network, network_allowlist, build_args):
        self.build_calls.append(
            {
                "dockerfile": dockerfile,
                "context": context,
                "tags": list(tags),
                "network": network,
                "network_allowlist": tuple(network_allowlist),
                "build_args": dict(build_args),
            }
        )
        return environment.ImageBuildResult(image_ref=tags[0], digest=self.build_digest, logs="built")

    def resolve_digest(self, image_ref: str) -> str:
        self.digest_calls.append(image_ref)
        if "@sha256:" in image_ref:
            return image_ref.split("@", 1)[1]
        return self.build_digest

    def run(self, *, image, argv, workdir, env, network, mounts, timeout):
        self.run_calls.append(
            {
                "image": image,
                "argv": list(argv),
                "workdir": workdir,
                "env": dict(env),
                "network": network,
                "mounts": list(mounts),
                "timeout": timeout,
            }
        )
        key = tuple(argv)
        ok = self.run_ok and key not in self.failing_argv
        return environment.CommandResult(
            argv=key,
            exit_code=0 if ok else 7,
            stdout="ok" if ok else "",
            stderr="" if ok else "boom",
            duration_ms=1,
            timed_out=False,
        )


def _counting_materializer(calls: list[tuple[str, str, str]]):
    def _materialize(root: Path, sha: str, dest: Path) -> Path:
        calls.append((str(root), sha, str(dest)))
        return git_state.materialize_tree(root, sha, dest)

    return _materialize


def _explicit_config(tmp_path: Path, *, name: str = "explicit-environment.json", **overrides) -> Path:
    payload = {
        "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
        "workdir": "/workspace/repo",
        "image": PINNED_IMAGE,
        "setup": [["python3", "-m", "venv", ".venv"]],
        "smoke": [["true"]],
        "test": [["true"]],
        "env": {},
        "workspace_excludes": [".venv"],
    }
    payload.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _repolaunch_runner(payload: dict):
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(list(argv))
        output_path = Path(argv[argv.index("--output") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return environment.CommandResult(
            argv=tuple(argv), exit_code=0, stdout="ok", stderr="", duration_ms=2, timed_out=False
        )

    return runner, calls


# ---------------------------------------------------------------------------
# priority ordering
# ---------------------------------------------------------------------------


def test_explicit_config_outranks_every_other_source(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    runtime = FakeContainerRuntime()
    result = environment.resolve_environment(
        candidate,
        runtime=runtime,
        explicit_config=config_path,
        # A functioning devcontainer + CI + RepoLaunch are all configured too;
        # explicit must still win.
        ci_base_image=PINNED_IMAGE,
    )
    assert result.source == "explicit"
    assert result.image == PINNED_IMAGE
    assert result.validated == {"base": True, "outcome": True, "runs": 2}


def test_devcontainer_wins_over_ci_and_repolaunch_when_no_explicit_config(tmp_path, candidate):
    root = candidate.repo_root
    (root / ".devcontainer").mkdir()
    (root / ".devcontainer" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / ".devcontainer" / "devcontainer.json").write_text(
        json.dumps(
            {
                "build": {"dockerfile": "Dockerfile"},
                "customizations": {
                    environment.DEVCONTAINER_CUSTOMIZATION_KEY: {
                        "setup": [["true"]],
                        "smoke": [["true"]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add devcontainer")
    new_base = git(root, "rev-parse", "HEAD")
    updated_candidate = _Candidate(repo_root=root, base_sha=new_base, outcome_sha=candidate.outcome_sha)

    runtime = FakeContainerRuntime()
    result = environment.resolve_environment(
        updated_candidate,
        runtime=runtime,
        ci_base_image=PINNED_IMAGE,
        network_allowlist=("pypi.org",),
    )
    assert result.source == "project_container"
    assert runtime.build_calls, "expected the Dockerfile to be built"
    assert runtime.build_calls[0]["network"] == "allowlisted"


def test_ci_derived_wins_when_no_explicit_config_or_devcontainer(tmp_path, candidate):
    root = candidate.repo_root
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pip install -e .[dev]\n"
        "      - run: pytest -q\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add ci workflow")
    new_base = git(root, "rev-parse", "HEAD")
    updated_candidate = _Candidate(repo_root=root, base_sha=new_base, outcome_sha=candidate.outcome_sha)

    runtime = FakeContainerRuntime()
    result = environment.resolve_environment(updated_candidate, runtime=runtime, ci_base_image=PINNED_IMAGE)
    assert result.source == "ci_derived"
    assert result.setup == [["bash", "-lc", "pip install -e .[dev]"]]
    assert result.smoke == [["bash", "-lc", "pytest -q"]]
    assert result.test == result.smoke


def test_repolaunch_only_wins_as_a_last_resort(tmp_path, candidate):
    binary = tmp_path / "repolaunch"
    _make_executable(binary)
    payload = {
        "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
        "workdir": "/workspace/repo",
        "image": PINNED_IMAGE,
        "setup": [["true"]],
        "smoke": [["true"]],
        "test": [["true"]],
        "env": {},
        "workspace_excludes": [],
    }
    runner, calls = _repolaunch_runner(payload)
    runtime = FakeContainerRuntime()

    result = environment.resolve_environment(
        candidate,
        runtime=runtime,
        repolaunch_binary=binary,
        repolaunch_env={},
        command_runner=runner,
    )
    assert result.source == "repolaunch"
    assert calls, "repolaunch should have been invoked"
    assert "resolve-environment" in calls[0]


def test_priority_cascades_past_a_broken_earlier_source(tmp_path, candidate):
    """An explicit config with invalid commands must not stop the chain; it must be
    recorded as a failed attempt and resolution must fall through to the next source."""
    bad_config = tmp_path / "bad.json"
    bad_config.write_text(
        json.dumps(
            {
                "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
                "workdir": "/workspace/repo",
                "image": PINNED_IMAGE,
                "setup": ["pip install -e .[dev]"],  # shell string instead of argv array -> rejected
                "smoke": [["true"]],
                "test": [["true"]],
                "env": {},
                "workspace_excludes": [],
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "repolaunch"
    _make_executable(binary)
    payload = {
        "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
        "workdir": "/workspace/repo",
        "image": PINNED_IMAGE,
        "setup": [["true"]],
        "smoke": [["true"]],
        "test": [["true"]],
        "env": {},
        "workspace_excludes": [],
    }
    runner, calls = _repolaunch_runner(payload)
    runtime = FakeContainerRuntime()

    result = environment.resolve_environment(
        candidate,
        runtime=runtime,
        explicit_config=bad_config,
        repolaunch_binary=binary,
        repolaunch_env={},
        command_runner=runner,
    )
    assert result.source == "repolaunch"
    assert calls, "repolaunch should still run after the explicit config failed"


def test_priority_cascades_after_an_earlier_source_fails_validation(tmp_path, candidate):
    bad_config = _explicit_config(tmp_path, setup=[], smoke=[["bad-smoke"]])
    binary = tmp_path / "repolaunch"
    _make_executable(binary)
    payload = {
        "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
        "workdir": "/workspace/repo",
        "image": PINNED_IMAGE,
        "setup": [],
        "smoke": [["true"]],
        "test": [["true"]],
        "env": {},
        "workspace_excludes": [],
    }
    runner, calls = _repolaunch_runner(payload)
    runtime = FakeContainerRuntime()
    runtime.failing_argv.add(("bad-smoke",))

    result = environment.resolve_environment(
        candidate,
        runtime=runtime,
        explicit_config=bad_config,
        repolaunch_binary=binary,
        repolaunch_env={},
        command_runner=runner,
    )

    assert result.source == "repolaunch"
    assert calls
    assert any(call["argv"] == ["bad-smoke"] for call in runtime.run_calls)
    assert any(call["argv"] == ["true"] for call in runtime.run_calls)


# ---------------------------------------------------------------------------
# two clean runs of base and outcome
# ---------------------------------------------------------------------------


def test_validation_materializes_two_fresh_copies_of_base_and_outcome(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    runtime = FakeContainerRuntime()
    materialize_calls: list[tuple[str, str, str]] = []

    environment.resolve_environment(
        candidate,
        runtime=runtime,
        explicit_config=config_path,
        materializer=_counting_materializer(materialize_calls),
    )

    # base and outcome, twice each == 4 fresh checkouts for validation.
    shas = [call[1] for call in materialize_calls]
    assert shas.count(candidate.base_sha) == 2
    assert shas.count(candidate.outcome_sha) == 2
    # every materialization target directory must be unique: nothing is reused
    # across runs, which is how the two-run gate proves there is no leftover
    # state from a previous attempt making the second run pass falsely.
    dests = [call[2] for call in materialize_calls]
    assert len(set(dests)) == len(dests)

    # one setup command + one smoke command, run twice per state == 8 run() calls.
    assert len(runtime.run_calls) == 8
    assert all(call["network"] == "disabled" for call in runtime.run_calls)


def test_validation_requires_at_least_two_runs(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    runtime = FakeContainerRuntime()
    with pytest.raises(environment.EnvironmentValidationError, match="at least 2"):
        environment.resolve_environment(
            candidate, runtime=runtime, explicit_config=config_path, validation_runs=1
        )


def test_environment_with_no_smoke_command_is_never_marked_validated(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, smoke=[], test=[["true"]])
    runtime = FakeContainerRuntime()
    with pytest.raises(environment.EnvironmentUnavailableError, match="no smoke command"):
        environment.resolve_environment(candidate, runtime=runtime, explicit_config=config_path)
    assert not runtime.run_calls, "no command should ever be executed for an unvalidatable environment"


def test_failing_smoke_command_raises_and_is_not_silently_recorded_as_success(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    runtime = FakeContainerRuntime()
    runtime.failing_argv.add(("true",))
    with pytest.raises(environment.EnvironmentUnavailableError, match="smoke command failed"):
        environment.resolve_environment(candidate, runtime=runtime, explicit_config=config_path)


def test_failing_setup_command_stops_before_smoke_and_raises(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    runtime = FakeContainerRuntime()
    runtime.failing_argv.add(("python3", "-m", "venv", ".venv"))
    with pytest.raises(environment.EnvironmentUnavailableError, match="setup command failed"):
        environment.resolve_environment(candidate, runtime=runtime, explicit_config=config_path)


def test_validation_writes_private_logs(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    logs_dir = tmp_path / "logs"
    runtime = FakeContainerRuntime()
    environment.resolve_environment(
        candidate, runtime=runtime, explicit_config=config_path, logs_dir=logs_dir
    )
    log_files = sorted(logs_dir.glob("*.log"))
    assert len(log_files) == 8  # setup + smoke commands, twice for base, twice for outcome
    for log_file in log_files:
        mode = stat.S_IMODE(log_file.stat().st_mode)
        assert mode == 0o600
        assert "exit_code: 0" in log_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# command array / schema rejection
# ---------------------------------------------------------------------------


def test_setup_as_a_shell_string_is_rejected_not_silently_wrapped(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, setup=["pip install -e .[dev]"])
    with pytest.raises(environment.UnsafeCommandError, match="argv array"):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_nested_non_list_command_item_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, setup=[["pip", "install"], "not-a-list"])
    with pytest.raises(environment.UnsafeCommandError):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_empty_argv_entry_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, setup=[[]])
    with pytest.raises(environment.UnsafeCommandError, match="must not be empty"):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_non_string_token_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, setup=[["pip", 1]])
    with pytest.raises(environment.UnsafeCommandError):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


# ---------------------------------------------------------------------------
# environment_id digest determinism
# ---------------------------------------------------------------------------


def test_environment_id_is_deterministic_for_identical_effective_inputs(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)

    first = environment.resolve_environment(
        candidate, runtime=FakeContainerRuntime(), explicit_config=config_path
    )
    second = environment.resolve_environment(
        candidate, runtime=FakeContainerRuntime(), explicit_config=config_path
    )
    assert first.environment_id == second.environment_id
    assert first.environment_id.startswith("sha256:")


def test_environment_id_changes_when_effective_inputs_change(tmp_path, candidate):
    config_a = _explicit_config(tmp_path, name="a.json")
    config_b = _explicit_config(tmp_path, name="b.json", workdir="/workspace/other")

    result_a = environment.resolve_environment(
        candidate, runtime=FakeContainerRuntime(), explicit_config=config_a
    )
    result_b = environment.resolve_environment(
        candidate, runtime=FakeContainerRuntime(), explicit_config=config_b
    )
    assert result_a.environment_id != result_b.environment_id


def test_compute_environment_id_is_order_insensitive_for_env_and_excludes():
    common = dict(
        source="explicit",
        base_sha="a" * 40,
        image=PINNED_IMAGE,
        workdir="/w",
        setup=[["x"]],
        smoke=[["y"]],
        test=[["y"]],
    )
    first = environment.compute_environment_id(
        env={"A": "", "B": ""}, workspace_excludes=[".venv", "dist"], **common
    )
    second = environment.compute_environment_id(
        env={"B": "", "A": ""}, workspace_excludes=["dist", ".venv"], **common
    )
    assert first == second
    third = environment.compute_environment_id(
        env={"A": "", "B": ""},
        workspace_excludes=[".venv", "dist"],
        network_allowlist=["pypi.org"],
        **common,
    )
    assert third != first


def test_docker_runtime_never_labels_bridge_network_as_allowlisted(tmp_path):
    calls: list[list[str]] = []

    def runner(argv, _timeout):
        calls.append(list(argv))
        stdout = "demo@sha256:" + "f" * 64 if "inspect" in argv else "built"
        return environment.CommandResult(
            argv=tuple(argv),
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=1,
            timed_out=False,
        )

    runtime = environment.DockerContainerRuntime(command_runner=runner)
    with pytest.raises(environment.EnvironmentValidationError, match="cannot enforce"):
        runtime.build_image(
            dockerfile=tmp_path / "Dockerfile",
            context=tmp_path,
            tags=["demo:test"],
            network="allowlisted",
            network_allowlist=["pypi.org"],
            build_args={},
        )

    runtime.build_image(
        dockerfile=tmp_path / "Dockerfile",
        context=tmp_path,
        tags=["demo:test"],
        network="disabled",
        network_allowlist=[],
        build_args={},
    )
    assert calls[0][calls[0].index("--network") + 1] == "none"

    calls.clear()
    runtime = environment.DockerContainerRuntime(
        command_runner=runner,
        allowlisted_network="retro-egress",
    )
    runtime.build_image(
        dockerfile=tmp_path / "Dockerfile",
        context=tmp_path,
        tags=["demo:test"],
        network="allowlisted",
        network_allowlist=["pypi.org"],
        build_args={},
    )
    assert calls[0][calls[0].index("--network") + 1] == "retro-egress"

    calls.clear()
    runtime.run(
        image=PINNED_IMAGE,
        argv=["node", "--version"],
        workdir="/workspace/repo",
        env={},
        network="disabled",
        mounts=[],
        timeout=10,
    )
    assert calls[0][calls[0].index("--entrypoint") + 1] == ""


# ---------------------------------------------------------------------------
# secrets and unpinned images
# ---------------------------------------------------------------------------


def test_secret_like_env_value_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, env={"GITHUB_TOKEN": "ghp_leaked_value"})
    with pytest.raises(environment.SecretMaterialRejectedError):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_secret_reference_with_empty_value_is_accepted(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, env={"GITHUB_TOKEN": ""}, secrets=["GITHUB_TOKEN"])
    spec = environment.load_explicit_config(config_path, repo_root=candidate.repo_root)
    assert spec.env == {"GITHUB_TOKEN": ""}


def test_declared_secret_name_with_a_value_is_still_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, env={"MY_SECRET": "value"}, secrets=["MY_SECRET"])
    with pytest.raises(environment.SecretMaterialRejectedError):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_unpinned_image_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, image="demo:latest")
    with pytest.raises(environment.UnpinnedImageError):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


def test_unpinned_ci_base_image_is_rejected(candidate):
    with pytest.raises(environment.UnpinnedImageError):
        environment.discover_ci_commands(candidate.repo_root, candidate.base_sha, ci_base_image="demo:latest")


def test_declared_image_digest_mismatch_is_rejected(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, image=PINNED_IMAGE)
    runtime = FakeContainerRuntime()
    # Force resolve_digest to return something other than what the config declared.
    runtime.resolve_digest = lambda image_ref: "sha256:" + "f" * 64  # noqa: E731
    with pytest.raises(environment.EnvironmentUnavailableError, match="does not match"):
        environment.resolve_environment(candidate, runtime=runtime, explicit_config=config_path)


def test_bare_digest_image_reference_is_preserved(tmp_path, candidate):
    digest = "sha256:" + "c" * 64
    config_path = _explicit_config(tmp_path, image=digest)
    runtime = FakeContainerRuntime()
    runtime.resolve_digest = lambda image_ref: image_ref  # noqa: E731

    result = environment.resolve_environment(
        candidate,
        runtime=runtime,
        explicit_config=config_path,
    )

    assert result.image == digest
    assert "@sha256:" not in result.image


def test_image_and_build_are_mutually_exclusive(tmp_path, candidate):
    config_path = _explicit_config(tmp_path, build={"dockerfile": "Dockerfile"})
    with pytest.raises(environment.EnvironmentError, match="either 'image' or 'build'"):
        environment.load_explicit_config(config_path, repo_root=candidate.repo_root)


# ---------------------------------------------------------------------------
# explicit config must live outside the evaluated repository
# ---------------------------------------------------------------------------


def test_explicit_config_inside_the_repo_is_rejected(candidate):
    inside_path = candidate.repo_root / "environment.json"
    inside_path.write_text(
        json.dumps(
            {
                "schema_version": environment.EXPLICIT_ENVIRONMENT_CONFIG_SCHEMA,
                "workdir": "/workspace/repo",
                "image": PINNED_IMAGE,
                "setup": [],
                "smoke": [["true"]],
                "test": [["true"]],
                "env": {},
                "workspace_excludes": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(environment.EnvironmentError, match="outside the evaluated repository"):
        environment.load_explicit_config(inside_path, repo_root=candidate.repo_root)


@pytest.mark.parametrize(
    ("build", "detail"),
    [
        ({"dockerfile": "../Dockerfile"}, "Dockerfile resolves outside"),
        ({"dockerfile": "Dockerfile", "context": ".."}, "build context resolves outside"),
    ],
)
def test_build_paths_cannot_escape_materialized_checkout(tmp_path, candidate, build, detail):
    config_path = _explicit_config(
        tmp_path,
        image=None,
        build=build,
        setup=[],
        smoke=[["true"]],
    )
    runtime = FakeContainerRuntime()

    with pytest.raises(environment.EnvironmentUnavailableError, match=detail):
        environment.resolve_environment(
            candidate,
            runtime=runtime,
            explicit_config=config_path,
            repolaunch_env={},
        )

    assert runtime.build_calls == []


# ---------------------------------------------------------------------------
# no ambient / silent fallback
# ---------------------------------------------------------------------------


def test_no_source_available_raises_environment_unavailable_with_all_attempts(candidate):
    runtime = FakeContainerRuntime()
    with pytest.raises(environment.EnvironmentUnavailableError) as excinfo:
        environment.resolve_environment(candidate, runtime=runtime, repolaunch_env={})
    attempts = {attempt.source for attempt in excinfo.value.attempts}
    assert attempts == {"project_container", "ci_derived", "repolaunch"}
    assert not runtime.run_calls
    assert not runtime.build_calls


def test_repolaunch_never_falls_back_to_an_ambient_path_lookup(monkeypatch, candidate):
    # No explicit binary, no env var: RepoLaunch must be skipped entirely, never
    # searched for on PATH.
    monkeypatch.delenv(environment.REPOLAUNCH_BIN_ENV, raising=False)
    binary = environment.resolve_repolaunch_binary(None, env={})
    assert binary is None


def test_repolaunch_env_var_pointing_at_a_missing_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(environment.RepoLaunchError):
        environment.resolve_repolaunch_binary(None, env={environment.REPOLAUNCH_BIN_ENV: str(missing)})


def test_repolaunch_binary_must_be_executable(tmp_path):
    non_executable = tmp_path / "repolaunch"
    non_executable.write_text("not executable", encoding="utf-8")
    with pytest.raises(environment.RepoLaunchError, match="not executable"):
        environment.resolve_repolaunch_binary(non_executable, env={})


def test_repolaunch_output_is_schema_validated(tmp_path, candidate):
    binary = tmp_path / "repolaunch"
    _make_executable(binary)
    bad_payload = {"schema_version": "not-the-right-schema"}
    runner, _calls = _repolaunch_runner(bad_payload)
    with pytest.raises(environment.EnvironmentError):
        environment.invoke_repolaunch(
            str(binary),
            repo_root=candidate.repo_root,
            base_sha=candidate.base_sha,
            materializer=git_state.materialize_tree,
            command_runner=runner,
        )


def test_repolaunch_process_failure_raises(tmp_path, candidate):
    def failing_runner(argv, timeout):
        return environment.CommandResult(
            argv=tuple(argv),
            exit_code=1,
            stdout="",
            stderr="repolaunch exploded",
            duration_ms=1,
            timed_out=False,
        )

    with pytest.raises(environment.RepoLaunchError, match="repolaunch exploded"):
        environment.invoke_repolaunch(
            "repolaunch",
            repo_root=candidate.repo_root,
            base_sha=candidate.base_sha,
            materializer=git_state.materialize_tree,
            command_runner=failing_runner,
        )


# ---------------------------------------------------------------------------
# Dockerfile / devcontainer discovery
# ---------------------------------------------------------------------------


def test_bare_dockerfile_without_customizations_cascades_instead_of_guessing(candidate):
    root = candidate.repo_root
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "add bare dockerfile")
    new_base = git(root, "rev-parse", "HEAD")
    assert environment.discover_project_container(root, new_base) is None


def test_devcontainer_with_jsonc_comments_is_parsed(candidate):
    root = candidate.repo_root
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / ".devcontainer").mkdir()
    (root / ".devcontainer" / "devcontainer.json").write_text(
        "{\n"
        '  // this is a comment referencing https://example.com\n'
        '  "build": {"dockerfile": "../Dockerfile"},\n'
        '  "customizations": {\n'
        f'    "{environment.DEVCONTAINER_CUSTOMIZATION_KEY}": {{\n'
        '      "setup": [["true"]],\n'
        '      "smoke": [["true"]],\n'
        '    },\n'
        "  },\n"
        "}\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add devcontainer with comments")
    new_base = git(root, "rev-parse", "HEAD")

    spec = environment.discover_project_container(root, new_base)
    assert spec is not None
    assert spec.source == "project_container"
    assert spec.setup == [["true"]]
    assert spec.build is not None
    assert str(spec.build.dockerfile) == "Dockerfile"


def test_devcontainer_customization_rejects_secret_values(candidate):
    root = candidate.repo_root
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / ".devcontainer").mkdir()
    (root / ".devcontainer" / "devcontainer.json").write_text(
        json.dumps(
            {
                "build": {"dockerfile": "../Dockerfile"},
                "customizations": {
                    environment.DEVCONTAINER_CUSTOMIZATION_KEY: {
                        "setup": [["true"]],
                        "smoke": [["true"]],
                        "env": {"API_TOKEN": "leaked"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add devcontainer with secret leak")
    new_base = git(root, "rev-parse", "HEAD")
    with pytest.raises(environment.SecretMaterialRejectedError):
        environment.discover_project_container(root, new_base)


# ---------------------------------------------------------------------------
# CI workflow discovery
# ---------------------------------------------------------------------------


def test_ci_discovery_returns_none_without_a_pinned_base_image(candidate):
    root = candidate.repo_root
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: pytest -q\n", encoding="utf-8"
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add ci")
    new_base = git(root, "rev-parse", "HEAD")
    assert environment.discover_ci_commands(root, new_base, ci_base_image=None) is None


def test_ci_discovery_returns_none_without_a_recognizable_test_command(candidate):
    root = candidate.repo_root
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - run: echo hello\n", encoding="utf-8"
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add ci without tests")
    new_base = git(root, "rev-parse", "HEAD")
    assert environment.discover_ci_commands(root, new_base, ci_base_image=PINNED_IMAGE) is None


def test_ci_discovery_parses_block_scalar_run_steps(candidate):
    root = candidate.repo_root
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: install\n"
        "        run: |\n"
        "          pip install -e .\n"
        "          pip install -e .[dev]\n"
        "      - name: test\n"
        "        run: pytest -q\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "add multi-line ci")
    new_base = git(root, "rev-parse", "HEAD")
    spec = environment.discover_ci_commands(root, new_base, ci_base_image=PINNED_IMAGE)
    assert spec is not None
    assert spec.setup == [["bash", "-lc", "pip install -e .\npip install -e .[dev]"]]
    assert spec.smoke == [["bash", "-lc", "pytest -q"]]


# ---------------------------------------------------------------------------
# full ProjectEnvironment shape
# ---------------------------------------------------------------------------


def test_resolved_environment_matches_the_published_shape(tmp_path, candidate):
    config_path = _explicit_config(tmp_path)
    result = environment.resolve_environment(
        candidate, runtime=FakeContainerRuntime(), explicit_config=config_path
    )
    payload = result.to_dict()
    assert payload["schema_version"] == "retro-project-environment-v1"
    assert payload["network_during_run"] == "disabled"
    assert payload["validated"] == {"base": True, "outcome": True, "runs": 2}
    assert payload["base_sha"] == candidate.base_sha
