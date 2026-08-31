"""Tests for candidate-agent evaluation of published benchmark tasks."""
from __future__ import annotations

import json
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer.build import (
    BuildConfig,
    TasksetPaths,
    build_sources,
    compute_task_id,
)
from retro.benchmarks.task_scorer.ghostlab_cli import (
    CommandOutcome,
    GhostlabCli,
    sha256_file,
    sha256_json,
)
from retro.benchmarks.task_scorer.run import (
    AgentSpec,
    RunConfig,
    TaskVerificationError,
    collect_eval_report,
    run_agent,
    run_attempt,
    task_source_index,
    verify_published_task,
)
from retro.benchmarks.task_scorer.schema import ATTEMPT_STATUSES, BenchmarkAttempt
from tests.task_scorer_harness import (
    BASE_TREE,
    TASK_PROMPT,
    accept_all_lint,
    make_agent_config,
    make_audit_outputs,
    make_builder_outputs,
    make_candidate_overlay,
    make_definer_outputs,
    make_source_bundle,
    write_fake_ghostlab,
    write_plan,
)

SOURCE_ID = "codex__019abc"


class _Published:
    """A published task set plus a candidate agent, all backed by the fake ghostlab."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.paths = TasksetPaths(root=tmp_path / "bench" / "pilot" / "task-scorer", name="pilot")
        self.source_root = make_source_bundle(self.paths.sources_dir(), SOURCE_ID)
        self.task_id = compute_task_id(SOURCE_ID, BASE_TREE, "replay", TASK_PROMPT)
        definer_out = make_definer_outputs(tmp_path / "out" / "definer", SOURCE_ID)
        builder_out = make_builder_outputs(tmp_path / "out" / "builder", self.task_id)
        audit_out = make_audit_outputs(tmp_path / "out" / "auditor")
        self.solved = make_candidate_overlay(tmp_path / "out" / "candidate-solved", solved=True)
        self.unsolved = make_candidate_overlay(tmp_path / "out" / "candidate-unsolved", solved=False)

        self.binary = write_fake_ghostlab(tmp_path / "bin")
        self.plan_path = tmp_path / "plan.json"
        self.plan: dict[str, Any] = {
            "artifact_runs": {
                "retro-task-definer-v1": {"outputs": str(definer_out)},
                "retro-scorer-builder-v1": {"outputs": str(builder_out)},
                "retro-scorer-auditor-v1": {"outputs": str(audit_out)},
                "candidate-good": {"workspace_overlay": str(self.solved)},
                "candidate-bad": {"workspace_overlay": str(self.unsolved)},
            }
        }
        self.write_plan()

        agents = tmp_path / "agents"
        definer = make_agent_config(agents / "definer.json", "retro-task-definer-v1", "m-definer")
        builder = make_agent_config(agents / "builder.json", "retro-scorer-builder-v1", "m-builder")
        auditor = make_agent_config(agents / "auditor.json", "retro-scorer-auditor-v1", "m-auditor")
        self.good_agent_path = make_agent_config(
            agents / "candidate-good.json", "candidate-good", "m-candidate"
        )
        self.bad_agent_path = make_agent_config(
            agents / "candidate-bad.json", "candidate-bad", "m-candidate"
        )

        build_sources(
            self.paths,
            BuildConfig(
                name="pilot",
                ghostlab=self.client(),
                task_definer_agent=definer,
                scorer_builder_agent=builder,
                scorer_auditor_agent=auditor,
                lint=accept_all_lint,
                repeatability_runs=1,
            ),
            [SOURCE_ID],
        )

    def write_plan(self) -> None:
        write_plan(self.plan_path, self.plan)

    def client(self) -> GhostlabCli:
        return GhostlabCli(self.binary, env={"FAKE_GHOSTLAB_PLAN": str(self.plan_path)})

    def run_config(self, **overrides: Any) -> RunConfig:
        defaults: dict[str, Any] = {"ghostlab": self.client(), "eval_id": "eval-1", "seeds": (0,)}
        defaults.update(overrides)
        return RunConfig(**defaults)

    def agent(self, name: str = "good") -> AgentSpec:
        path = self.good_agent_path if name == "good" else self.bad_agent_path
        return AgentSpec.from_path(path)


@pytest.fixture()
def published(tmp_path: Path) -> _Published:
    return _Published(tmp_path)


def test_published_task_is_verified_before_any_model_runs(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    assert task.task_id == published.task_id
    assert task.source_id == SOURCE_ID
    assert task.pass_threshold == 0.8
    assert task.base_bundle_sha256 and task.scorer_package_sha256

    (published.paths.task_dir(published.task_id) / "public" / "task.json").write_text("{}")
    with pytest.raises(TaskVerificationError) as excinfo:
        verify_published_task(published.paths, published.task_id)
    assert "schema_version" in str(excinfo.value)


def test_published_task_allows_an_empty_environment_setup(published: _Published) -> None:
    task_dir = published.paths.task_dir(published.task_id)
    environment_path = task_dir / "public" / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    environment["setup"] = []
    environment_path.write_text(json.dumps(environment), encoding="utf-8")

    task_path = task_dir / "public" / "task.json"
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    task_payload["environment"]["setup_command"] = []
    task_path.write_text(json.dumps(task_payload), encoding="utf-8")

    provenance_path = task_dir / "private" / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["public_task_sha256"] = sha256_file(task_path)
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    task = verify_published_task(published.paths, published.task_id)
    assert task.setup_commands == ()


def test_published_task_migrates_a_recomputed_legacy_scorer_hash(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    scorer_dir = task.scorer_manifest_path.parent
    scorer_payload = json.loads(task.scorer_manifest_path.read_text(encoding="utf-8"))
    legacy_files = [
        (
            path.relative_to(scorer_dir).as_posix(),
            sha256_file(path),
        )
        for path in sorted(scorer_dir.rglob("*"))
        if path.is_file() and path.name != "scorer.json"
    ]
    legacy_hash = sha256_json(
        {
            "scorer_json": {
                key: value
                for key, value in scorer_payload.items()
                if key != "package_sha256"
            },
            "files": legacy_files,
        }
    )
    assert legacy_hash != task.scorer_package_sha256

    provenance_path = task.task_dir / "private" / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["scorer_package_sha256"] = legacy_hash
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    migrated = verify_published_task(published.paths, published.task_id)
    assert migrated.scorer_package_sha256 == task.scorer_package_sha256


def test_agent_runtime_layer_must_use_the_published_task_base(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    agent_dir = published.good_agent_path.parent
    dockerfile = agent_dir / "agent-image" / "Dockerfile"
    dockerfile.parent.mkdir()
    dockerfile.write_text(f"FROM {task.environment_image}\nRUN true\n", encoding="utf-8")
    sibling = dockerfile.parent / "runtime.txt"
    sibling.write_text("runtime\n", encoding="utf-8")
    runner = agent_dir / "runner.py"
    runner.write_text("print('run')\n", encoding="utf-8")
    instruction = agent_dir / "instruction.md"
    instruction.write_text("instructions\n", encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["instructions"] = ["instruction.md"]
    payload["sandbox"]["image"] = "agent-image/Dockerfile"
    payload["sandbox"]["uploads"] = [
        {"source": "runner.py", "target": "/sandbox/agent/runner.py"}
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()

    assert agent.sandbox_image_override(task.environment_image) is None
    assert "agent-image/Dockerfile" in hashes
    assert "runner.py" in hashes

    execution_config, override = agent.execution_config(
        task.environment_image,
        published.tmp_path / "runtime-snapshot",
        expected_asset_hashes=hashes,
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    assert override is None
    assert Path(execution["sandbox"]["image"]).is_absolute()
    assert Path(execution["sandbox"]["image"]).is_file()
    assert Path(execution["sandbox"]["uploads"][0]["source"]).is_file()
    assert Path(execution["runtime"]["instructions"][0]).is_absolute()
    assert Path(execution["runtime"]["instructions"][0]).is_file()

    with pytest.raises(TaskVerificationError, match="overlaps source"):
        agent.execution_config(
            task.environment_image,
            dockerfile.parent / "snapshot",
            expected_asset_hashes=hashes,
        )

    dockerfile.write_text("FROM unrelated@sha256:" + "0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(TaskVerificationError, match="single-stage"):
        agent.sandbox_image_override(task.environment_image)

    dockerfile.write_text(
        f"FROM {task.environment_image} AS decoy\n"
        "FROM unrelated@sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TaskVerificationError, match="single-stage"):
        agent.sandbox_image_override(task.environment_image)


def test_agent_fingerprint_covers_every_runtime_input_path(
    published: _Published,
) -> None:
    agent_dir = published.good_agent_path.parent
    instruction = agent_dir / "instruction.md"
    instruction.write_text("instruction\n", encoding="utf-8")
    runtime_skill = agent_dir / "runtime-skill"
    runtime_skill.mkdir()
    (runtime_skill / "SKILL.md").write_text("skill\n", encoding="utf-8")
    subagent_prompt = agent_dir / "subagent.md"
    subagent_prompt.write_text("subagent\n", encoding="utf-8")
    input_skill = agent_dir / "input-skill"
    input_skill.mkdir()
    (input_skill / "SKILL.md").write_text("input skill\n", encoding="utf-8")

    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["instructions"] = ["instruction.md"]
    payload["runtime"]["skills"] = {"paths": ["runtime-skill"]}
    payload["runtime"]["agents"] = {
        "helper": {"prompt": "subagent.md"},
    }
    payload["inputs"]["skills"] = [{"path": "input-skill"}]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")

    hashes = AgentSpec.from_path(published.good_agent_path).referenced_asset_hashes()
    assert set(hashes) >= {
        "instruction.md",
        "runtime-skill",
        "subagent.md",
        "input-skill",
    }


def test_runtime_only_assets_are_snapshotted_and_rewritten(
    published: _Published,
) -> None:
    instruction = published.good_agent_path.parent / "instruction.md"
    instruction.write_text("instruction\n", encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["instructions"] = ["instruction.md"]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()

    execution_config, override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "runtime-only-snapshot",
        expected_asset_hashes=hashes,
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    rewritten = Path(execution["runtime"]["instructions"][0])
    assert execution_config != agent.config_path
    assert override is not None
    assert rewritten.is_absolute()
    assert rewritten.is_file()


def test_runtime_snapshot_rejects_assets_changed_after_fingerprinting(
    published: _Published,
) -> None:
    runner = published.good_agent_path.parent / "runner.py"
    runner.write_text("print('first')\n", encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["sandbox"]["uploads"] = [
        {"source": "runner.py", "target": "/sandbox/agent/runner.py"}
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()
    runner.write_text("print('changed')\n", encoding="utf-8")

    with pytest.raises(TaskVerificationError, match="changed after attempt fingerprinting"):
        agent.execution_config(
            verify_published_task(published.paths, published.task_id).environment_image,
            published.tmp_path / "changed-snapshot",
            expected_asset_hashes=hashes,
        )


def test_shared_instruction_and_upload_use_one_role_aware_snapshot(
    published: _Published,
) -> None:
    shared = published.good_agent_path.parent / "shared.md"
    shared.write_text("shared\n", encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["instructions"] = ["shared.md"]
    payload["sandbox"]["uploads"] = [
        {"source": "shared.md", "target": "/sandbox/agent/shared.md"}
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "shared-snapshot",
        expected_asset_hashes=agent.referenced_asset_hashes(),
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    instruction = Path(execution["runtime"]["instructions"][0])
    upload = Path(execution["sandbox"]["uploads"][0]["source"])
    assert instruction == upload
    assert instruction.is_file()


def test_agent_config_snapshot_rejects_changes_after_loading(
    published: _Published,
) -> None:
    agent = AgentSpec.from_path(published.good_agent_path)
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["model"] = "changed-model"
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TaskVerificationError, match="changed after it was loaded"):
        agent.referenced_asset_hashes()


def test_mcp_fingerprint_covers_local_server_dependencies(
    published: _Published,
) -> None:
    mcp = published.good_agent_path.parent / "mcp"
    server = mcp / "server"
    server.mkdir(parents=True)
    (mcp / "package.json").write_text('{"name":"probe"}\n', encoding="utf-8")
    implementation = server / "index.js"
    implementation.write_text("console.log('first')\n", encoding="utf-8")
    config = mcp / "target.json"
    config.write_text(
        json.dumps(
            {
                "id": "probe",
                "transport": "stdio",
                "connection": {
                    "command": ["node"],
                    "args": [str(implementation)],
                },
            }
        ),
        encoding="utf-8",
    )
    (mcp / "probe").mkdir()
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["inputs"]["mcps"] = [
        {"config_ref": "mcp/target.json", "server": "probe"}
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    first = agent.referenced_asset_hashes()["mcp/target.json"]

    implementation.write_text("console.log('changed')\n", encoding="utf-8")
    second = agent.referenced_asset_hashes()["mcp/target.json"]
    assert first != second

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "mcp-snapshot",
        expected_asset_hashes=agent.referenced_asset_hashes(),
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    config_snapshot = Path(execution["inputs"]["mcps"][0]["config_ref"])
    assert execution["inputs"]["mcps"][0]["server"] == "probe"
    snapshotted_mcp = json.loads(config_snapshot.read_text(encoding="utf-8"))
    assert snapshotted_mcp["id"] == "probe"
    rewritten_program = Path(snapshotted_mcp["connection"]["args"][0])
    assert rewritten_program != implementation
    assert rewritten_program.is_file()


def test_runtime_assets_reject_escaping_symlinks(published: _Published) -> None:
    skill = published.good_agent_path.parent / "skill"
    skill.mkdir()
    outside = published.tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (skill / "escape").symlink_to("../../outside.txt")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["skills"] = {"paths": ["skill"]}
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TaskVerificationError, match="escaping symlink"):
        AgentSpec.from_path(published.good_agent_path).referenced_asset_hashes()


def test_runtime_assets_reject_absolute_internal_symlinks(
    published: _Published,
) -> None:
    skill = published.good_agent_path.parent / "skill"
    skill.mkdir()
    target = skill / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    (skill / "absolute").symlink_to(target.resolve())
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["skills"] = {"paths": ["skill"]}
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TaskVerificationError, match="absolute symlink"):
        AgentSpec.from_path(published.good_agent_path).referenced_asset_hashes()


def test_non_path_mcp_fields_are_never_fingerprinted_or_rewritten(
    published: _Published,
) -> None:
    agent_dir = published.good_agent_path.parent
    misleading = agent_dir / "notes"
    misleading.mkdir()
    server = agent_dir / "server.js"
    server.write_text("console.log('server')\n", encoding="utf-8")
    config = agent_dir / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "notes": {
                        "command": "node",
                        "args": ["server.js", "notes"],
                        "env": {"STATE": "notes"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["skills"] = {"paths": ["notes"]}
    payload["inputs"]["mcps"] = [
        {"config_ref": "mcp.json", "server": "notes"}
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()
    assert "notes" in hashes

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "non-path-snapshot",
        expected_asset_hashes=hashes,
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    assert execution["inputs"]["mcps"][0]["server"] == "notes"
    assert Path(execution["runtime"]["skills"]["paths"][0]).is_dir()
    snapshotted_config = json.loads(
        Path(execution["inputs"]["mcps"][0]["config_ref"]).read_text(
            encoding="utf-8"
        )
    )
    selected = snapshotted_config["mcpServers"]["notes"]
    assert selected["env"]["STATE"] == "notes"
    assert selected["args"][1] == "notes"


def test_inline_mcp_directory_arguments_are_snapshotted(
    published: _Published,
) -> None:
    data = published.good_agent_path.parent / "mcp-data"
    data.mkdir()
    payload_file = data / "state.json"
    payload_file.write_text('{"value":1}\n', encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["inputs"]["mcps"] = [
        {
            "id": "filesystem",
            "transport": "stdio",
            "connection": {
                "command": "node",
                "args": [str(data)],
                "env": {},
            },
        }
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()
    first = hashes[str(data)]
    payload_file.write_text('{"value":2}\n', encoding="utf-8")
    assert agent.referenced_asset_hashes()[str(data)] != first

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "inline-mcp-directory",
        expected_asset_hashes=agent.referenced_asset_hashes(),
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    rewritten = Path(execution["inputs"]["mcps"][0]["connection"]["args"][0])
    assert rewritten != data
    assert rewritten.is_dir()
    assert (rewritten / "state.json").is_file()


def test_inline_mcp_absolute_interpreters_remain_image_paths(
    published: _Published,
) -> None:
    interpreter = Path("/usr/bin/python3")
    if not interpreter.is_file():
        pytest.skip("/usr/bin/python3 is unavailable")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["inputs"]["mcps"] = [
        {
            "id": "python",
            "transport": "stdio",
            "connection": {
                "command": str(interpreter),
                "args": [],
                "env": {},
            },
        }
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()
    assert str(interpreter) not in hashes

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "inline-mcp-interpreter",
        expected_asset_hashes=hashes,
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    assert (
        execution["inputs"]["mcps"][0]["connection"]["command"]
        == str(interpreter)
    )


def test_inline_mcp_root_and_home_arguments_are_not_uploaded(
    published: _Published,
) -> None:
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["inputs"]["mcps"] = [
        {
            "id": "filesystem",
            "transport": "stdio",
            "connection": {
                "command": "node",
                "args": ["/", str(Path.home())],
                "env": {},
            },
        }
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    hashes = agent.referenced_asset_hashes()
    assert "/" not in hashes
    assert str(Path.home()) not in hashes

    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "root-home-mcp",
        expected_asset_hashes=hashes,
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    assert execution["inputs"]["mcps"][0]["connection"]["args"] == [
        "/",
        str(Path.home()),
    ]


def test_equal_non_path_mcp_and_subagent_values_are_not_rewritten(
    published: _Published,
) -> None:
    notes = published.good_agent_path.parent / "notes"
    notes.mkdir()
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["runtime"]["skills"] = {"paths": ["notes"]}
    payload["runtime"]["agents"] = {
        "helper": {"prompt": "notes"},
    }
    payload["inputs"]["mcps"] = [
        {
            "id": "notes",
            "transport": "stdio",
            "connection": {
                "command": "node",
                "args": ["notes"],
                "env": {"STATE": "notes"},
            },
        }
    ]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    execution_config, _override = agent.execution_config(
        verify_published_task(published.paths, published.task_id).environment_image,
        published.tmp_path / "mixed-value-snapshot",
        expected_asset_hashes=agent.referenced_asset_hashes(),
    )
    execution = json.loads(execution_config.read_text(encoding="utf-8"))
    assert Path(execution["runtime"]["skills"]["paths"][0]).is_dir()
    assert execution["runtime"]["agents"]["helper"]["prompt"] == "notes"
    mcp = execution["inputs"]["mcps"][0]
    assert mcp["id"] == "notes"
    assert mcp["connection"]["args"] == ["notes"]
    assert mcp["connection"]["env"]["STATE"] == "notes"


def test_mcp_config_does_not_hash_its_unrelated_parent_directory(
    published: _Published,
) -> None:
    agent_dir = published.good_agent_path.parent
    project = agent_dir / "server-project"
    project.mkdir()
    (project / "package.json").write_text('{"name":"server"}\n', encoding="utf-8")
    server = project / "server.js"
    server.write_text("console.log('server')\n", encoding="utf-8")
    config = agent_dir / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "id": "probe",
                "transport": "stdio",
                "connection": {"command": ["node"], "args": [str(server)]},
            }
        ),
        encoding="utf-8",
    )
    unrelated = agent_dir / "unrelated.bin"
    unrelated.write_text("first\n", encoding="utf-8")
    payload = json.loads(published.good_agent_path.read_text(encoding="utf-8"))
    payload["inputs"]["mcps"] = [{"config_ref": "mcp.json"}]
    published.good_agent_path.write_text(json.dumps(payload), encoding="utf-8")
    agent = AgentSpec.from_path(published.good_agent_path)
    first = agent.referenced_asset_hashes()["mcp.json"]

    unrelated.write_text("changed\n", encoding="utf-8")
    assert agent.referenced_asset_hashes()["mcp.json"] == first


def test_legacy_scorer_hash_rejects_nested_scorer_manifests(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    scorer_dir = task.scorer_manifest_path.parent
    scorer_payload = json.loads(task.scorer_manifest_path.read_text(encoding="utf-8"))
    legacy_files = [
        (path.relative_to(scorer_dir).as_posix(), sha256_file(path))
        for path in sorted(scorer_dir.rglob("*"))
        if path.is_file() and path.name != "scorer.json"
    ]
    legacy_hash = sha256_json(
        {
            "scorer_json": {
                key: value
                for key, value in scorer_payload.items()
                if key != "package_sha256"
            },
            "files": legacy_files,
        }
    )
    provenance_path = task.task_dir / "private" / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["scorer_package_sha256"] = legacy_hash
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    nested = scorer_dir / "nested" / "scorer.json"
    nested.parent.mkdir()
    nested.write_text("{}\n", encoding="utf-8")

    with pytest.raises(TaskVerificationError, match="changed after publication"):
        verify_published_task(published.paths, published.task_id)


def test_missing_public_material_is_reported(published: _Published) -> None:
    (published.paths.task_dir(published.task_id) / "public" / "base.bundle").unlink()
    with pytest.raises(TaskVerificationError) as excinfo:
        verify_published_task(published.paths, published.task_id)
    assert "public/base.bundle" in str(excinfo.value)


def test_solving_agent_scores_and_writes_an_immutable_attempt(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)

    assert attempt.status == "scored"
    assert attempt.score == pytest.approx(1.0)
    assert attempt.passed is True
    assert attempt.source_id == SOURCE_ID
    assert attempt.candidate_state_sha256
    assert attempt.tokens == {"input": 1000, "output": 250, "cached": 0}
    assert attempt.cost_usd == pytest.approx(0.02)
    assert attempt.reused is False

    artifact_run = json.loads(
        (attempt.attempt_dir / "agent" / "artifact-run.json").read_text(encoding="utf-8")
    )
    assert artifact_run["sandbox_image"] == "demo@sha256:" + "d" * 64
    assert artifact_run["setup_commands"] == [["python3", "-m", "venv", ".venv"]]

    payload = json.loads((attempt.attempt_dir / "attempt.json").read_text())
    assert payload["schema_version"] == "retro-benchmark-attempt-v1"
    assert payload["attempt_id"] == attempt.attempt_id
    assert payload["scorer_package_sha256"] == task.scorer_package_sha256
    assert payload["pass_threshold"] == task.pass_threshold
    parsed = BenchmarkAttempt.from_dict(payload)
    assert parsed.attempt_id == attempt.attempt_id
    for status in ATTEMPT_STATUSES:
        variant = dict(payload)
        variant["status"] = status
        if status != "scored":
            variant["score"] = None
            variant["passed"] = None
        assert BenchmarkAttempt.from_dict(variant).status == status
    scorer_resources = json.loads(
        (attempt.attempt_dir / "resources.json").read_text(encoding="utf-8")
    )
    assert "model" not in scorer_resources
    assert "agent_id" not in scorer_resources
    scorer_run = json.loads(
        (attempt.attempt_dir / "scorer" / "scorer-run.json").read_text(encoding="utf-8")
    )
    assert scorer_run["attempt_id"] == attempt.attempt_id
    assert scorer_run["task_id"] == task.task_id
    assert scorer_run["status"] == "scored"
    assert scorer_run["hashes"]["task_sha256"] == task.public_task_sha256
    assert (
        scorer_run["hashes"]["scorer_package_sha256"]
        == task.scorer_package_sha256
    )
    assert scorer_run["isolation"] == {
        "schema_version": "ghostlab-scorer-isolation-v1",
        "scorer_launcher": "landlock",
        "candidate_mount": "read_only",
        "secure_exec_available": True,
        "judge_launcher": "not_run",
    }

    # The attempt worked in a fresh materialization of the published base bundle.
    assert (attempt.attempt_dir / "workspace" / "src" / "legacy.py").is_file()
    assert not (attempt.attempt_dir / "workspace" / "src" / "feature.py").is_file()
    with tarfile.open(attempt.attempt_dir / "agent" / "candidate-state.tar.zst") as archive:
        assert "src/feature.py" in archive.getnames()


def test_failing_agent_is_a_genuine_zero_not_an_error(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent("bad"), 0)
    assert attempt.status == "scored"
    assert attempt.score == pytest.approx(0.0)
    assert attempt.passed is False
    assert attempt.error is None


def test_attempts_are_hash_addressed_and_resume(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    config = published.run_config()
    first = run_attempt(published.paths, config, task, published.agent(), 0)
    second = run_attempt(published.paths, config, task, published.agent(), 0)

    assert second.reused is True
    assert second.attempt_id == first.attempt_id
    assert second.score == first.score

    other_seed = run_attempt(published.paths, config, task, published.agent(), 1)
    assert other_seed.attempt_id != first.attempt_id

    forced = run_attempt(
        published.paths, published.run_config(force=True), task, published.agent(), 0
    )
    assert forced.reused is False
    assert forced.attempt_id == first.attempt_id


def test_cached_score_is_not_reused_without_its_bound_private_attestation(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    config = published.run_config()
    first = run_attempt(published.paths, config, task, published.agent(), 0)
    private_report = first.attempt_dir / "scorer" / "scorer-run.json"
    payload = json.loads(private_report.read_text(encoding="utf-8"))
    payload["attempt_id"] = "stale-attempt"
    private_report.write_text(json.dumps(payload), encoding="utf-8")

    rerun = run_attempt(published.paths, config, task, published.agent(), 0)

    assert rerun.status == "scored"
    assert rerun.reused is False
    assert rerun.attempt_id == first.attempt_id


@pytest.mark.parametrize(
    ("artifact_status", "expected"),
    [
        ("model_unavailable", "model_unavailable"),
        ("agent_error", "agent_error"),
        ("timed_out", "agent_timeout"),
        ("timeout", "agent_timeout"),
        ("export_failed", "harness_error"),
        ("output_contract_failed", "harness_error"),
        ("sandbox_error", "harness_error"),
    ],
)
def test_agent_side_failures_have_distinct_statuses(
    published: _Published, artifact_status: str, expected: str
) -> None:
    published.plan["artifact_runs"]["candidate-good"]["status"] = artifact_status
    published.write_plan()
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)
    assert attempt.status == expected
    assert attempt.score is None
    assert attempt.passed is None
    assert attempt.error


def test_scorer_failure_is_not_an_agent_failure(published: _Published) -> None:
    published.plan["scorer"] = {"force_status": "scorer_error"}
    published.write_plan()
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)
    assert attempt.status == "scorer_error"
    assert attempt.score is None


def test_misattributed_scorer_failure_is_an_invalid_result(
    published: _Published, monkeypatch: pytest.MonkeyPatch
) -> None:
    published.plan["scorer"] = {"force_status": "scorer_error"}
    published.write_plan()
    client = published.client()
    scorer_run = client.scorer_run

    def misattributed(request):  # noqa: ANN001, ANN202
        result = scorer_run(request)
        return replace(result, report={**result.report, "task_id": "wrong-task"})

    monkeypatch.setattr(client, "scorer_run", misattributed)
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(
        published.paths,
        published.run_config(ghostlab=client),
        task,
        published.agent(),
        0,
    )
    assert attempt.status == "invalid_result"
    assert "task_id" in (attempt.error or "")


def test_scorer_harness_crash_is_a_harness_side_error(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    assert run_attempt(
        published.paths, published.run_config(), task, published.agent(), 0
    ).status == "scored"
    published.plan["scorer"] = {"no_report": True}
    published.write_plan()
    attempt = run_attempt(
        published.paths,
        published.run_config(force=True),
        task,
        published.agent(),
        0,
    )
    assert attempt.status == "harness_error"
    assert "wrote no score report" in (attempt.error or "")


def test_artifact_run_invocation_failure_is_a_harness_error(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    assert run_attempt(
        published.paths, published.run_config(), task, published.agent(), 0
    ).status == "scored"
    published.plan["artifact_runs"]["candidate-good"] = {"no_report": True}
    published.write_plan()
    attempt = run_attempt(
        published.paths,
        published.run_config(force=True),
        task,
        published.agent(),
        0,
    )
    assert attempt.status == "harness_error"
    assert "wrote no artifact-run.json" in (attempt.error or "")


def test_ghostlab_binary_failure_is_recorded_as_a_harness_error(
    published: _Published,
) -> None:
    def runner(argv, timeout, env, cwd):  # noqa: ANN001
        return CommandOutcome(tuple(argv), 7, "", "broken binary", 1, False)

    task = verify_published_task(published.paths, published.task_id)
    client = GhostlabCli(published.binary, runner=runner)
    attempt = run_attempt(
        published.paths,
        published.run_config(ghostlab=client),
        task,
        published.agent(),
        0,
    )
    assert attempt.status == "harness_error"
    assert "ghostlab --version failed" in (attempt.error or "")


def test_agent_config_hash_mismatch_is_rejected(published: _Published) -> None:
    spec = AgentSpec.from_path(published.good_agent_path, expected_sha256="0" * 64)
    task = verify_published_task(published.paths, published.task_id)
    with pytest.raises(TaskVerificationError):
        run_attempt(published.paths, published.run_config(), task, spec, 0)


@pytest.mark.parametrize("agent_id", ["../escape", "nested/agent", "", "."])
def test_unsafe_agent_ids_are_rejected_before_path_use(
    published: _Published, agent_id: str
) -> None:
    with pytest.raises(TaskVerificationError, match="agent id"):
        AgentSpec.from_path(published.good_agent_path, agent_id=agent_id)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"task_id": "0" * 20}, "task_id"),
        ({"attempt_id": "wrong-attempt"}, "attempt_id"),
        ({"scorer_package_sha256": "0" * 64}, "scorer_package_sha256"),
    ],
)
def test_score_report_identity_is_cross_checked(
    published: _Published, override: dict[str, Any], message: str
) -> None:
    published.plan["scorer"] = {"report_overrides": override}
    published.write_plan()
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)
    assert attempt.status == "invalid_result"
    assert message in (attempt.error or "")


@pytest.mark.parametrize(
    ("scorer_plan", "message"),
    [
        ({"no_run_report": True}, "no trusted scorer-run.json"),
        (
            {"isolation_overrides": {"secure_exec_available": False}},
            "exact GHOSTLAB_SECURE_EXEC",
        ),
        (
            {"isolation_overrides": {"unexpected": True}},
            "exact GHOSTLAB_SECURE_EXEC",
        ),
        (
            {"run_report_overrides": {"attempt_id": "stale-attempt"}},
            "attempt_id",
        ),
        (
            {
                "run_report_overrides": {
                    "hashes": {
                        "task_sha256": "0" * 64,
                        "scorer_package_sha256": "1" * 64,
                    }
                }
            },
            "task_sha256",
        ),
    ],
)
def test_evaluation_rejects_untrusted_private_scorer_attestation(
    published: _Published,
    scorer_plan: dict[str, Any],
    message: str,
) -> None:
    published.plan["scorer"] = scorer_plan
    published.write_plan()
    task = verify_published_task(published.paths, published.task_id)

    attempt = run_attempt(
        published.paths,
        published.run_config(),
        task,
        published.agent(),
        0,
    )

    assert attempt.status == "invalid_result"
    assert attempt.score is None
    assert message in (attempt.error or "")


def test_evaluation_does_not_accept_a_stale_private_scorer_attestation(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    stale = (
        published.paths.attempt_dir("eval-1", task.task_id, "candidate-good", 0)
        / "scorer"
        / "scorer-run.json"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"stale":true}\n', encoding="utf-8")
    published.plan["scorer"] = {"no_run_report": True}
    published.write_plan()

    attempt = run_attempt(
        published.paths,
        published.run_config(),
        task,
        published.agent(),
        0,
    )

    assert attempt.status == "invalid_result"
    assert "no trusted scorer-run.json" in (attempt.error or "")
    assert not stale.exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"valid": False}, "valid=true"),
        ({"pass_threshold": 0.7}, "pass_threshold"),
        ({"unscored_weight": 0.1}, "unscored_weight"),
    ],
)
def test_evaluation_checks_ghostlab_score_metadata(
    published: _Published,
    override: dict[str, Any],
    message: str,
) -> None:
    published.plan["scorer"] = {"report_overrides": override}
    published.write_plan()
    task = verify_published_task(published.paths, published.task_id)

    attempt = run_attempt(
        published.paths,
        published.run_config(),
        task,
        published.agent(),
        0,
    )

    assert attempt.status == "invalid_result"
    assert message in (attempt.error or "")


def test_score_report_components_must_match_published_manifest(
    published: _Published,
) -> None:
    task = verify_published_task(published.paths, published.task_id)
    first = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)
    report = json.loads((first.attempt_dir / "score-report.json").read_text())
    report["components"][0]["weight"] = 0.6
    published.plan["scorer"] = {"report_overrides": {"components": report["components"]}}
    published.write_plan()

    attempt = run_attempt(
        published.paths, published.run_config(force=True), task, published.agent(), 0
    )
    assert attempt.status == "invalid_result"
    assert "published weight" in (attempt.error or "")


def test_attempt_fingerprint_includes_runtime_inputs_and_agent_assets(
    published: _Published,
) -> None:
    instruction = published.good_agent_path.parent / "candidate.md"
    instruction.write_text("first\n")
    payload = json.loads(published.good_agent_path.read_text())
    payload["runtime"]["instructions"] = ["candidate.md"]
    published.good_agent_path.write_text(json.dumps(payload))
    agent = AgentSpec.from_path(published.good_agent_path)
    task = verify_published_task(published.paths, published.task_id)

    first = run_attempt(published.paths, published.run_config(), task, agent, 0)
    instruction.write_text("second\n")
    asset_changed = run_attempt(published.paths, published.run_config(), task, agent, 0)
    timeout_changed = run_attempt(
        published.paths,
        published.run_config(aut_timeout_seconds=123.0),
        task,
        agent,
        0,
    )
    scorer_timeout_changed = run_attempt(
        published.paths,
        published.run_config(scorer_timeout_seconds=321.0),
        task,
        agent,
        0,
    )
    export_changed = run_attempt(
        published.paths,
        published.run_config(candidate_export_name="other-state.tar.zst"),
        task,
        agent,
        0,
    )

    assert len(
        {
            first.attempt_id,
            asset_changed.attempt_id,
            timeout_changed.attempt_id,
            scorer_timeout_changed.attempt_id,
            export_changed.attempt_id,
        }
    ) == 5
    assert not asset_changed.reused
    assert not timeout_changed.reused
    assert not scorer_timeout_changed.reused
    assert not export_changed.reused


def test_attempt_fingerprint_includes_prompt_and_full_environment(
    published: _Published,
) -> None:
    first_task = verify_published_task(published.paths, published.task_id)
    first = run_attempt(
        published.paths, published.run_config(), first_task, published.agent(), 0
    )

    first_task.prompt_path.write_text(first_task.prompt_path.read_text() + "\nMore context.\n")
    prompt_task = verify_published_task(published.paths, published.task_id)
    prompt_changed = run_attempt(
        published.paths, published.run_config(), prompt_task, published.agent(), 0
    )

    environment = json.loads(prompt_task.public_environment_path.read_text())
    environment["workspace_excludes"].append(".cache")
    prompt_task.public_environment_path.write_text(json.dumps(environment))
    environment_task = verify_published_task(published.paths, published.task_id)
    environment_changed = run_attempt(
        published.paths, published.run_config(), environment_task, published.agent(), 0
    )

    assert first.attempt_id != prompt_changed.attempt_id
    assert prompt_changed.attempt_id != environment_changed.attempt_id


def test_published_environment_requires_pinned_image_and_matching_setup_projection(
    published: _Published,
) -> None:
    environment_path = (
        published.paths.task_dir(published.task_id) / "public" / "environment.json"
    )
    environment = json.loads(environment_path.read_text())
    environment["image"] = "demo:latest"
    environment_path.write_text(json.dumps(environment))
    with pytest.raises(TaskVerificationError, match="pinned"):
        verify_published_task(published.paths, published.task_id)

    environment["image"] = "demo@sha256:" + "d" * 64
    environment["setup"] = []
    environment_path.write_text(json.dumps(environment))
    with pytest.raises(TaskVerificationError, match="setup_command does not match"):
        verify_published_task(published.paths, published.task_id)


def test_run_agent_over_seeds_produces_a_source_normalized_report(published: _Published) -> None:
    result = run_agent(
        published.paths,
        published.run_config(seeds=(0, 1, 2)),
        published.agent(),
        token_budgets=(500.0, 5000.0),
        wall_time_budgets_ms=(1.0,),
    )
    assert len(result.attempts) == 3
    assert {attempt.seed for attempt in result.attempts} == {0, 1, 2}

    agent = result.aggregate.agent("candidate-good")
    assert agent is not None
    assert agent.benchmark_score == pytest.approx(1.0)
    assert agent.pass_rate == pytest.approx(1.0)
    assert agent.coverage == pytest.approx(1.0)
    assert agent.scored_attempts == 3
    assert agent.error_counts == {"agent_error": 0, "scorer_error": 0, "harness_error": 0}
    assert agent.component_means["requested_behavior"] == pytest.approx(1.0)
    assert agent.sources[0].source_id == SOURCE_ID
    assert agent.tasks[0].std_score == pytest.approx(0.0)
    assert agent.resources["tokens_total"]["input"] == 3000

    budgets = {(item.dimension, item.budget): item for item in agent.budget_conditionals}
    assert budgets[("tokens", 500.0)].score == pytest.approx(0.0)
    assert budgets[("tokens", 5000.0)].score == pytest.approx(1.0)
    assert budgets[("wall_time_ms", 1.0)].over_budget == 3

    published_results = json.loads(published.paths.results_path("eval-1").read_text())
    assert published_results["schema_version"] == "retro-benchmark-aggregate-v1"
    assert published_results["agents"][0]["agent_id"] == "candidate-good"


def test_two_agents_are_scored_independently_in_one_eval(published: _Published) -> None:
    run_agent(published.paths, published.run_config(), published.agent("good"))
    run_agent(published.paths, published.run_config(), published.agent("bad"))
    aggregate = collect_eval_report(published.paths, "eval-1")

    good = aggregate.agent("candidate-good")
    bad = aggregate.agent("candidate-bad")
    assert good is not None and bad is not None
    assert good.benchmark_score == pytest.approx(1.0)
    assert bad.benchmark_score == pytest.approx(0.0)
    assert bad.pass_rate == pytest.approx(0.0)


def test_historical_report_uses_the_attempt_threshold(published: _Published) -> None:
    run_agent(published.paths, published.run_config(), published.agent("bad"))
    public_task_path = (
        published.paths.task_dir(published.task_id) / "public" / "task.json"
    )
    public_task = json.loads(public_task_path.read_text())
    public_task["scoring"]["pass_threshold"] = 0.0
    public_task_path.write_text(json.dumps(public_task))

    aggregate = collect_eval_report(published.paths, "eval-1")
    agent = aggregate.agent("candidate-bad")
    assert agent is not None
    assert agent.tasks[0].pass_threshold == pytest.approx(0.8)
    assert agent.tasks[0].pass_rate == pytest.approx(0.0)

    persisted = json.loads(published.paths.results_path("eval-1").read_text())
    assert persisted["agents"][0]["tasks"][0]["pass_threshold"] == pytest.approx(0.8)


def test_task_source_index_maps_published_tasks(published: _Published) -> None:
    assert task_source_index(published.paths) == {published.task_id: SOURCE_ID}


def test_corrupt_base_bundle_is_a_harness_error(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    task.base_bundle.write_bytes(b"not an archive")
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)
    assert attempt.status == "harness_error"
    assert "materialize the base repository" in (attempt.error or "")


def test_tampering_with_the_published_scorer_is_detected(published: _Published) -> None:
    score_py = published.paths.task_dir(published.task_id) / "private" / "scorer" / "score.py"
    score_py.write_text(score_py.read_text() + "\n# tampered\n")
    with pytest.raises(TaskVerificationError) as excinfo:
        verify_published_task(published.paths, published.task_id)
    assert "scorer package changed after publication" in str(excinfo.value)


def test_candidate_artifacts_never_contain_oracle_material(published: _Published) -> None:
    task = verify_published_task(published.paths, published.task_id)
    attempt = run_attempt(published.paths, published.run_config(), task, published.agent(), 0)

    with tarfile.open(attempt.attempt_dir / "agent" / "candidate-state.tar.zst") as archive:
        names = set(archive.getnames())
    assert "src/legacy.py" in names
    assert not any(name.startswith(".git") for name in names)

    blob = b"".join(
        path.read_bytes()
        for path in attempt.attempt_dir.rglob("*")
        if path.is_file() and path.name not in {"attempt.json", "resources.json"}
    )
    assert b"oracle.bundle" not in blob
    assert b"reference.patch" not in blob
    assert str(published.source_root).encode() not in blob


def test_seed_parsing_and_eval_id_helpers(published: _Published) -> None:
    from retro.benchmarks.task_scorer.run import (
        default_eval_id,
        list_evals,
        parse_seeds,
        resolve_eval_id,
    )

    assert parse_seeds("0,1,2") == (0, 1, 2)
    assert parse_seeds(" 3 , 3 , 1 ") == (3, 1)
    assert parse_seeds([0, 1]) == (0, 1)
    with pytest.raises(TaskVerificationError):
        parse_seeds("a")
    with pytest.raises(TaskVerificationError):
        parse_seeds("")

    assert default_eval_id().startswith("eval-")
    with pytest.raises(TaskVerificationError):
        resolve_eval_id(published.paths, "latest")

    run_agent(published.paths, published.run_config(eval_id="eval-a"), published.agent())
    run_agent(published.paths, published.run_config(eval_id="eval-b"), published.agent())
    assert list_evals(published.paths) == ["eval-a", "eval-b"]
    assert resolve_eval_id(published.paths, "latest") == "eval-b"
    assert resolve_eval_id(published.paths, "eval-a") == "eval-a"
    with pytest.raises(TaskVerificationError):
        resolve_eval_id(published.paths, "../escape")


def test_unsafe_eval_id_is_rejected(published: _Published) -> None:
    with pytest.raises(TaskVerificationError):
        run_agent(published.paths, published.run_config(eval_id="../boom"), published.agent())
