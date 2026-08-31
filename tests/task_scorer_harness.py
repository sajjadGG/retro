"""Fixtures for the rollout task/scorer pipeline tests.

Everything here is deliberately model-free: a fake ``ghostlab`` executable
implements the two public commands, and the "agent-generated" scorer package is
a real ``score.py`` that the fake actually executes, so validation is driven by
scorer artifacts rather than hard-coded numbers.
"""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from retro.benchmarks.task_scorer.bundle import compute_content_hash

FAKE_GHOSTLAB = r'''#!/usr/bin/env python3
"""Minimal stand-in for the public ghostlab CLI used by Retro."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

EXCLUDES = {
    ".git", ".venv", "node_modules", "target", "dist", "build",
    "__pycache__", ".pytest_cache",
}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root):
    root = Path(root)
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDES for part in relative.parts):
            continue
        if path.is_file():
            records.append([relative.as_posix(), sha256_file(path)])
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def scorer_package_hash(root):
    root = Path(root)
    manifest = json.loads((root / "scorer.json").read_text())
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "scorer.json":
            payload = {
                key: value for key, value in manifest.items() if key != "package_sha256"
            }
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        else:
            digest = sha256_file(path)
        entries.append({"path": relative, "kind": "file", "sha256": digest})
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def pack(source, destination):
    source = Path(source)
    with tarfile.open(destination, "w") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in EXCLUDES for part in relative.parts):
                continue
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if info.isfile():
                with open(path, "rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)


def load_plan():
    path = os.environ.get("FAKE_GHOSTLAB_PLAN")
    if not path or not Path(path).is_file():
        return {}
    return json.loads(Path(path).read_text())


def artifact_run(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--export", action="append", default=[])
    parser.add_argument("--optional-export", action="append", default=[])
    parser.add_argument("--export-workspace")
    parser.add_argument("--output-contract")
    parser.add_argument("--sandbox-image")
    parser.add_argument("--setup-command", action="append", default=[])
    args = parser.parse_args(argv)

    plan = load_plan()
    agent = json.loads(Path(args.agent).read_text())
    key = agent.get("id") or Path(args.agent).stem
    entry = plan.get("artifact_runs", {}).get(key, {})
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    Path(run_dir / "invocations.log").open("a").write(key + "\n")

    if entry.get("no_report"):
        sys.stderr.write("fake ghostlab crashed before writing artifact-run.json\n")
        return 3

    workspace_input = tree_hash(args.workspace)
    scratch = Path(tempfile.mkdtemp(prefix="fake-ghostlab-"))
    sandbox = scratch / "ws"
    shutil.copytree(args.workspace, sandbox)
    if entry.get("mutate_host"):
        (Path(args.workspace) / "GHOSTLAB-MUTATED-HOST.txt").write_text("unsafe\n")
    overlay = entry.get("workspace_overlay")
    if overlay:
        shutil.copytree(overlay, sandbox, dirs_exist_ok=True)
    if entry.get("mutate_source"):
        (sandbox / "AGENT-WROTE-HERE.txt").write_text("input tree mutated\n")
    workspace_output = tree_hash(sandbox)

    exports = []
    outputs = entry.get("outputs")
    for spec in args.export + args.optional_export:
        sandbox_path, _, local = spec.partition("=")
        if not outputs:
            continue
        prefix = "/sandbox/output/"
        relative = (
            sandbox_path[len(prefix):] if sandbox_path.startswith(prefix)
            else Path(sandbox_path).name
        )
        source = Path(outputs) / relative
        if not source.exists():
            continue
        destination = run_dir / local
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination)
            exports.append({"path": local, "sha256": None})
        else:
            shutil.copyfile(source, destination)
            exports.append({"path": local, "sha256": sha256_file(destination)})

    if args.export_workspace:
        workspace_export_name = entry.get("workspace_export_name", args.export_workspace)
        target = run_dir / workspace_export_name
        pack(sandbox, target)
        exports.append({"path": workspace_export_name, "sha256": sha256_file(target)})

    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "user.message", "text": Path(args.prompt_file).read_text()}) + "\n"
    )

    report = {
        "schema_version": entry.get("schema_version", "ghostlab-artifact-run-v1"),
        "status": entry.get("status", "completed"),
        "agent_config_sha256": sha256_file(args.agent),
        "workspace_input_sha256": workspace_input,
        "workspace_output_sha256": workspace_output,
        "prompt_sha256": sha256_file(args.prompt_file),
        "runner": {"kind": "fake"},
        "model": agent.get("runtime", {}).get("model", "fake-model"),
        "sandbox_image": args.sandbox_image,
        "setup_commands": [json.loads(item) for item in args.setup_command],
        "started_at": "2026-08-29T00:00:00Z",
        "finished_at": "2026-08-29T00:01:00Z",
        "exit_code": entry.get("exit_code", 0),
        "timed_out": bool(entry.get("timed_out")),
        "exports": exports + list(entry.get("extra_exports", [])),
        "events_path": "events.jsonl",
        "tokens": entry.get("tokens", {"input": 1000, "output": 250, "cached": 0}),
        "cost_usd": entry.get("cost_usd", 0.02),
    }
    for field in entry.get("omit_report_fields", []):
        report.pop(field, None)
    report.update(entry.get("report_overrides", {}))
    (run_dir / "artifact-run.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return int(entry.get("process_exit", 0))


def scorer_run(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--scorer", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trace")
    parser.add_argument("--resources")
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--seed", default="0")
    parser.add_argument("--run-dir")
    args = parser.parse_args(argv)

    plan = load_plan()
    entry = plan.get("scorer", {})
    if entry.get("no_report"):
        sys.stderr.write("fake ghostlab scorer-run crashed\n")
        return 4

    scorer_json = Path(args.scorer)
    manifest = json.loads(scorer_json.read_text())
    package_sha256 = scorer_package_hash(scorer_json.parent)
    attempt_id = args.attempt_id
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def write_run_record(report):
        if not args.run_dir or entry.get("no_run_report"):
            return
        run_dir = Path(args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        isolation = {
            "schema_version": "ghostlab-scorer-isolation-v1",
            "scorer_launcher": "landlock",
            "candidate_mount": "read_only",
            "secure_exec_available": True,
            "judge_launcher": (
                "landlock"
                if manifest.get("mode") in ("judge", "hybrid", "agentic")
                else "not_run"
            ),
        }
        isolation.update(entry.get("isolation_overrides", {}))
        record = {
            "schema_version": "ghostlab-scorer-run-v1",
            "task_id": manifest["task_id"],
            "attempt_id": attempt_id,
            "status": report["status"],
            "hashes": {
                "scorer_package_sha256": package_sha256,
                "task_sha256": sha256_file(Path(args.task)),
            },
            "isolation": isolation,
        }
        record.update(entry.get("run_report_overrides", {}))
        (run_dir / "scorer-run.json").write_text(
            json.dumps(record, indent=2, sort_keys=True)
        )

    if entry.get("force_status"):
        report = {
            "schema_version": "retro-score-report-v1",
            "task_id": manifest["task_id"],
            "attempt_id": attempt_id,
            "status": entry["force_status"],
            "valid": False,
            "components": [],
            "hard_gate_failures": [],
            "commands": [],
            "judge": None,
            "warnings": [],
            "scorer_package_sha256": package_sha256,
            "duration_ms": 5,
        }
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
        write_run_record(report)
        return 0

    scratch = Path(tempfile.mkdtemp(prefix="fake-scorer-"))
    repo = scratch / "repo"
    candidate = Path(args.candidate)
    if candidate.is_dir():
        shutil.copytree(candidate, repo)
    else:
        repo.mkdir(parents=True)
        with tarfile.open(candidate, "r:*") as archive:
            archive.extractall(repo)

    inputs = scratch / "input"
    outputs = scratch / "output"
    inputs.mkdir()
    outputs.mkdir()
    shutil.copyfile(args.task, inputs / "task.json")
    (inputs / "score-input.json").write_text(json.dumps({
        "schema_version": "retro-score-input-v1",
        "task_id": manifest["task_id"],
        "attempt_id": attempt_id,
        "repo_path": str(repo),
        "task_path": str(inputs / "task.json"),
        "trace_path": args.trace,
        "resource_usage_path": args.resources,
        "seed": int(args.seed),
    }, indent=2, sort_keys=True))

    mapping = {
        "/scorer": str(scorer_json.parent),
        "/input": str(inputs),
        "/output": str(outputs),
        "/candidate/repo": str(repo),
    }
    command = []
    for token in manifest["entrypoint"]:
        for key, value in mapping.items():
            token = token.replace(key, value)
        command.append(token)
    if command and command[0] == "python3":
        command[0] = sys.executable

    result = subprocess.run(command, capture_output=True, text=True)
    report_path = outputs / "score-report.json"
    if result.returncode != 0 or not report_path.is_file():
        report = {
            "schema_version": "retro-score-report-v1",
            "task_id": manifest["task_id"],
            "attempt_id": attempt_id,
            "status": "scorer_error",
            "valid": False,
            "components": [],
            "hard_gate_failures": [],
            "commands": [{"argv": command, "exit_code": result.returncode, "duration_ms": 1}],
            "judge": None,
            "warnings": [(result.stderr or "")[-500:]],
            "scorer_package_sha256": package_sha256,
            "duration_ms": 5,
        }
    else:
        report = json.loads(report_path.read_text())
    report["scorer_package_sha256"] = package_sha256
    if report.get("status") == "scored":
        threshold = float(manifest.get("pass_threshold", 0.8))
        unscored_weight = sum(
            float(component.get("weight", 0.0))
            for component in report.get("components", [])
            if component.get("value") is None
        )
        report["valid"] = unscored_weight <= 0.2 + 1e-9
        report["pass_threshold"] = threshold
        report["unscored_weight"] = round(unscored_weight, 6)
        report["passed"] = bool(
            report["valid"]
            and not report.get("hard_gate_failures")
            and float(report.get("score_total", 0.0)) + 1e-9 >= threshold
        )
    report.update(entry.get("report_overrides", {}))
    output.write_text(json.dumps(report, indent=2, sort_keys=True))
    write_run_record(report)
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: ghostlab <command>\n")
        return 2
    if argv[0] in ("--version", "-V", "version"):
        sys.stdout.write("ghostlab 9.9.9-fake\n")
        return 0
    if argv[0] == "artifact-run":
        return artifact_run(argv[1:])
    if argv[0] == "scorer-run":
        return scorer_run(argv[1:])
    sys.stderr.write("unknown command %s\n" % argv[0])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

SCORE_PY = r'''#!/usr/bin/env python3
"""Deterministic behavior/regression scorer produced by the ScorerBuilder run."""
import argparse
import hashlib
import json
from pathlib import Path


def package_sha256():
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(p for p in here.rglob("*") if p.is_file()):
        digest.update(path.relative_to(here).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text())
    repo = Path(payload["repo_path"])

    feature = repo / "src" / "feature.py"
    feature_text = feature.read_text() if feature.is_file() else ""
    behavior = 1.0 if ("def greet(" in feature_text and "hello, world" in feature_text) else 0.0

    legacy = repo / "src" / "legacy.py"
    legacy_text = legacy.read_text() if legacy.is_file() else ""
    regression = 1.0 if "LEGACY_CONSTANT = 1" in legacy_text else 0.0

    components = [
        {
            "id": "requested_behavior",
            "value": behavior,
            "weight": 0.7,
            "hard_gate": True,
            "gate_passed": behavior >= 1.0,
            "evidence": [
                {"kind": "file", "ref": "src/feature.py", "summary": "greet contract"}
            ],
        },
        {
            "id": "regression_suite",
            "value": regression,
            "weight": 0.3,
            "hard_gate": True,
            "gate_passed": regression >= 1.0,
            "evidence": [
                {"kind": "file", "ref": "src/legacy.py", "summary": "legacy constant"}
            ],
        },
    ]
    failures = [c["id"] for c in components if c["hard_gate"] and not c["gate_passed"]]
    total = 0.0 if failures else sum(c["value"] * c["weight"] for c in components)
    report = {
        "schema_version": "retro-score-report-v1",
        "task_id": payload["task_id"],
        "attempt_id": payload.get("attempt_id") or "unknown",
        "status": "scored",
        "score_total": round(total, 6),
        "passed": bool(not failures and total >= 0.8),
        "valid": True,
        "pass_threshold": 0.8,
        "unscored_weight": 0.0,
        "components": components,
        "hard_gate_failures": failures,
        "commands": [],
        "judge": None,
        "warnings": [],
        "scorer_package_sha256": package_sha256(),
        "duration_ms": 3,
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''

BASE_LEGACY = "LEGACY_CONSTANT = 1\n\n\ndef legacy():\n    return LEGACY_CONSTANT\n"
OUTCOME_FEATURE = 'def greet(name):\n    return f"hello, world from {name}"\n'
PRESERVING_FEATURE = (
    "def _render(name):\n"
    '    return f"hello, world from {name}"\n'
    "\n\n"
    "def greet(name):\n"
    "    return _render(name)\n"
)
CHANGING_FEATURE = 'def greet(name):\n    return f"goodbye {name}"\n'

BASE_SHA = "1" * 40
BASE_TREE = "2" * 40
OUTCOME_SHA = "3" * 40
OUTCOME_TREE = "4" * 40


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fake_ghostlab(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "ghostlab"
    binary.write_text(FAKE_GHOSTLAB)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True))
    return path


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


#: Agent id -> the packaged instruction file its runtime must load.
ROLE_INSTRUCTIONS = {
    "retro-task-definer-v1": "instructions/task-definer.md",
    "retro-scorer-builder-v1": "instructions/scorer-builder.md",
    "retro-scorer-auditor-v1": "instructions/scorer-auditor.md",
}


def make_agent_config(
    path: Path, agent_id: str, model: str, instructions: list[str] | None = None
) -> Path:
    declared = instructions
    if declared is None:
        packaged = ROLE_INSTRUCTIONS.get(agent_id)
        declared = [packaged] if packaged else []
    return write_json(
        path,
        {
            "id": agent_id,
            "name": agent_id,
            "runtime": {
                "backend": "opencode",
                "model": model,
                "instructions": declared,
                "tools": {"webfetch": False},
            },
            "inputs": {"skills": [], "mcps": [], "assets": []},
            "sandbox": {"backend": "openshell", "network": "disabled"},
        },
    )


def make_source_bundle(sources_dir: Path, source_id: str = "codex__019abc") -> Path:
    """Create a SourceBundle whose outcome adds a greet feature to the base."""
    root = sources_dir / source_id
    session_id = source_id.split("__")[-1]
    events = [
        {
            "event_id": f"{source_id}:1",
            "session_id": session_id,
            "host": "codex",
            "sequence": 1,
            "actor": "user",
            "event_type": "message",
            "summary": "user asks for a greet helper",
            "raw_ref": {"path": "rollout.jsonl", "line": 1},
            "timestamp": "2026-08-01T18:12:03Z",
            "parent_event_id": None,
            "payload": {"text": "Add a greet() helper that returns a hello, world message."},
        },
        {
            "event_id": f"{source_id}:2",
            "session_id": session_id,
            "host": "codex",
            "sequence": 2,
            "actor": "assistant",
            "event_type": "message",
            "summary": "assistant reports the edit",
            "raw_ref": {"path": "rollout.jsonl", "line": 2},
            "timestamp": "2026-08-01T18:20:00Z",
            "parent_event_id": f"{source_id}:1",
            "payload": {"text": "Added src/feature.py."},
        },
    ]
    events_path = root / "rollout" / "events.jsonl"
    write_text(events_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in events))
    write_text(root / "rollout" / "transcript.md", "# transcript\n")

    write_text(root / "repo" / "base" / "src" / "legacy.py", BASE_LEGACY)
    write_text(root / "repo" / "base" / "README.md", "# demo project\n")
    write_text(root / "repo" / "outcome" / "src" / "legacy.py", BASE_LEGACY)
    write_text(root / "repo" / "outcome" / "src" / "feature.py", OUTCOME_FEATURE)
    write_text(root / "repo" / "outcome" / "README.md", "# demo project\n")
    write_text(
        root / "repo" / "change.patch",
        "--- /dev/null\n+++ b/src/feature.py\n@@\n+" + OUTCOME_FEATURE.replace("\n", "\n+"),
    )
    write_text(root / "repo" / "git-log.jsonl", json.dumps({"sha": OUTCOME_SHA}) + "\n")
    write_json(root / "context" / "project-files.json", {"files": ["README.md", "src/legacy.py"]})
    write_json(
        root / "context" / "test-commands.json",
        {"setup": [["python3", "-m", "venv", ".venv"]], "smoke": [], "test": [["pytest", "-q"]]},
    )
    environment_id = "sha256:" + "e" * 64
    write_json(
        root / "context" / "environment.json",
        {
            "schema_version": "retro-project-environment-v1",
            "environment_id": environment_id,
            "source": "explicit",
            "base_sha": BASE_SHA,
            "image": "demo@sha256:" + "d" * 64,
            "workdir": "/workspace/repo",
            "setup": [["python3", "-m", "venv", ".venv"]],
            "smoke": [],
            "test": [["pytest", "-q"]],
            "env": {},
            "network_during_build": "allowlisted",
            "network_during_run": "disabled",
            "workspace_excludes": [".venv", "__pycache__"],
            "validated": {"base": True, "outcome": True, "runs": 2},
        },
    )
    write_json(
        root / "selection.json",
        {
            "schema_version": "retro-taskset-selection-v1",
            "status": "selected",
            "selected": True,
            "source_id": source_id,
            "environment_id": environment_id,
            "environment_validated": True,
        },
    )
    manifest = {
        "schema_version": "retro-source-bundle-v1",
        "source_id": source_id,
        "host": "codex",
        "session_id": session_id,
        "started_at": "2026-08-01T18:12:03Z",
        "ended_at": "2026-08-01T19:05:47Z",
        "rollout_events_sha256": _sha256_file(events_path),
        "repo": {
            "root_at_capture": "/Users/example/private-project",
            "repo_id": "sha256:" + "a" * 64,
            "base_sha": BASE_SHA,
            "base_tree": BASE_TREE,
            "outcome_sha": OUTCOME_SHA,
            "outcome_tree": OUTCOME_TREE,
            "base_resolution": "captured_start",
            "state_confidence": "exact_clean_commit",
            "subdir": ".",
            "environment_id": environment_id,
        },
        "task_limits": {"max_replay_tasks": 3, "adjacent_per_replay": 0},
        "content_sha256": None,
    }
    write_json(root / "manifest.json", manifest)
    manifest["content_sha256"] = compute_content_hash(root)
    write_json(root / "manifest.json", manifest)
    return root


TASK_PROMPT = (
    "Add a greet(name) helper in src/feature.py that returns a hello, world message "
    "including the supplied name."
)


def make_task_definitions(source_id: str, candidate_id: str = "goal-1-replay") -> dict[str, Any]:
    return {
        "schema_version": "retro-task-definitions-v1",
        "source_id": source_id,
        "tasks": [
            {
                "candidate_id": candidate_id,
                "kind": "replay",
                "prompt": TASK_PROMPT,
                "prompt_provenance": {
                    "user_event_ids": [f"{source_id}:1"],
                    "mode": "resolved_user_messages",
                },
                "goal_segment": {
                    "introduced_event_id": f"{source_id}:1",
                    "closed_event_id": f"{source_id}:2",
                    "summary": "add greet helper",
                },
                "repo_evidence": [
                    {"state": "base", "path": "src/legacy.py", "reason": "existing module"},
                    {"state": "outcome", "path": "src/feature.py", "reason": "new helper"},
                ],
                "scorer_brief": {
                    "observables": [
                        {
                            "id": "requested-behavior",
                            "description": "greet returns a hello, world message",
                            "importance": "gate",
                            "evidence": [f"{source_id}:1"],
                        }
                    ],
                    "regressions_to_protect": ["legacy constant"],
                    "performance": [],
                    "residual_judgment": [],
                    "forbidden_shortcuts": ["reference patch equality"],
                },
                "base_failure_claim": "src/feature.py does not exist at base",
                "outcome_success_claim": "greet returns the hello, world message",
                "adjacency": None,
                "confidence": {"goal": 0.96, "state": 1.0, "scorability": 0.9},
            }
        ],
        "rejections": [
            {"goal_event_ids": [f"{source_id}:9"], "code": "NO_OBSERVABLE_OUTCOME", "detail": "chat only"}
        ],
    }


def make_definer_outputs(directory: Path, source_id: str) -> Path:
    write_json(directory / "task-definitions.json", make_task_definitions(source_id))
    return directory


def make_scorer_package(directory: Path, task_id: str, *, mode: str = "deterministic") -> Path:
    write_text(directory / "score.py", SCORE_PY)
    write_text(
        directory / "tests" / "test_scorer.py",
        "def test_placeholder():\n    assert True\n",
    )
    write_json(
        directory / "scorer.json",
        {
            "schema_version": "retro-scorer-v1",
            "task_id": task_id,
            "mode": mode,
            "entrypoint": [
                "python3",
                "/scorer/score.py",
                "--input",
                "/input/score-input.json",
                "--output",
                "/output/score-report.json",
            ],
            "runtime": {
                "image": "sha256:" + "c" * 64,
                "network": "disabled",
                "timeout_seconds": 900,
                "cpu": 2,
                "memory_mb": 4096,
                "candidate_mount": "read_only",
            },
            "components": [
                {
                    "id": "requested_behavior",
                    "kind": "deterministic",
                    "weight": 0.7,
                    "hard_gate": True,
                    "range": [0.0, 1.0],
                },
                {
                    "id": "regression_suite",
                    "kind": "deterministic",
                    "weight": 0.3,
                    "hard_gate": True,
                    "range": [0.0, 1.0],
                },
            ],
            "pass_threshold": 0.8,
            "judge": None,
            "required_artifacts": ["repo", "task"],
        },
    )
    return directory


def make_builder_outputs(directory: Path, task_id: str) -> Path:
    """Emit the exact ScorerBuilder export tree: scorer, reference, cases, manifest."""
    make_scorer_package(directory / "scorer", task_id)
    write_text(directory / "reference" / "reference.patch", "")

    cases = directory / "cases"
    for name, feature in (
        ("construct-changing", CHANGING_FEATURE),
        ("construct-preserving", PRESERVING_FEATURE),
    ):
        write_text(cases / name / "src" / "legacy.py", BASE_LEGACY)
        write_text(cases / name / "src" / "feature.py", feature)
        write_text(cases / name / "README.md", "# demo project\n")
    write_text(cases / "regression" / "src" / "legacy.py", "def legacy():\n    return 0\n")
    write_text(cases / "regression" / "src" / "feature.py", OUTCOME_FEATURE)
    write_text(cases / "regression" / "README.md", "# demo project\n")

    write_json(
        directory / "validation-cases.json",
        {
            "schema_version": "retro-scorer-validation-cases-v1",
            "task_id": task_id,
            "cases": [
                {"id": "base", "kind": "base"},
                {"id": "oracle", "kind": "oracle"},
                {"id": "noop", "kind": "no_op"},
                {
                    "id": "construct-changing",
                    "kind": "construct_changing",
                    "candidate": "cases/construct-changing",
                    "component": "requested_behavior",
                },
                {
                    "id": "construct-preserving",
                    "kind": "construct_preserving",
                    "candidate": "cases/construct-preserving",
                },
                {
                    "id": "regression",
                    "kind": "regression",
                    "candidate": "cases/regression",
                    "component": "regression_suite",
                },
            ],
        },
    )
    return directory


def make_audit_outputs(directory: Path, decision: str = "accept") -> Path:
    write_json(
        directory / "audit.json",
        {
            "decision": decision,
            "leakage": [],
            "overfit_checks": ["construct-preserving mutant scored identically"],
            "missing_observables": [],
            "mutants": [],
            "evidence": ["scorer.json", "score.py"],
        },
    )
    write_text(directory / "mutants" / "notes.md", "no additional mutants\n")
    return directory


def make_candidate_overlay(directory: Path, *, solved: bool = True) -> Path:
    """Files the evaluated agent writes into its sandbox workspace copy."""
    if solved:
        write_text(directory / "src" / "feature.py", OUTCOME_FEATURE)
    else:
        write_text(directory / "NOTES.md", "I could not find the module.\n")
    return directory


def accept_all_lint(request: Any) -> dict[str, Any]:
    """Stand-in for the foundation ``task_lint`` module."""
    definitions = request.task_definitions
    return {
        "accepted": list(definitions.get("tasks") or []),
        "findings": [],
    }


def reject_all_lint(request: Any) -> dict[str, Any]:
    definitions = request.task_definitions
    return {
        "accepted": [],
        "findings": [
            {
                "code": "PROMPT_ORACLE_LEAKAGE",
                "detail": "prompt repeats an added line from change.patch",
                "candidate_id": task.get("candidate_id"),
            }
            for task in definitions.get("tasks") or []
        ],
    }
