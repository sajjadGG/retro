"""Published benchmark projections and their file:// UI, without task runtime."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import statistics
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from retro.dashboard.benchmarks import VALIDATION_KINDS, read_benchmarks
from retro.dashboard.build import ASSETS_DIR, build
from retro.dashboard.inputs import read_catalog
from retro.dashboard.publish import build_and_publish
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive, read_shard  # noqa: F401

NAME = "parser"
SUITE = "curated-v1"
EVAL = "test-eval"
TASK_A, TASK_B, TASK_C = "a" * 20, "b" * 20, "c" * 20
SOURCE_A = "codex__019d0001-0000-7000-8000-000000000001"
SOURCE_C = "claude-code__other-source"
PROMPT = "Make the parser handle <img src=x onerror=alert(1)> without regressions."
PRIVATE_MARKERS = (
    "HIDDEN_SCORER_SOURCE",
    "ACCEPTED_PATCH_BODY",
    "ASSISTANT_TRAJECTORY",
    "CANDIDATE_CODE_BODY",
    "ENV_CREDENTIAL_VALUE",
    "JUDGE_CREDENTIAL_VALUE",
    "PRIVATE_ORACLE_BYTES",
    "PRIVATE_SOURCE_DIRECTORY",
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_suite(path: Path, payload: dict) -> dict:
    payload = dict(payload)
    payload.pop("content_sha256", None)
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    write_json(path, payload)
    return payload


def published_task(root: Path, task_id: str, source: str, repo: str, *, kind: str = "replay") -> dict:
    public = root / "public"
    private = root / "private"
    task = {
        "schema_version": "retro-benchmark-task-v1",
        "task_id": task_id,
        "kind": kind,
        "prompt": PROMPT,
        "repository": {"repo_id": repo, "base_sha": "1" * 40, "base_tree": "2" * 40, "subdir": "."},
        "environment": {"image": "fixture@sha256:" + "3" * 64, "network": "disabled"},
        "limits": {"wall_time_seconds": 1800, "max_output_chars": 20000},
        "scoring": {"score_range": [0, 1], "pass_threshold": 0.8},
        "candidate_code": "CANDIDATE_CODE_BODY",
    }
    write_json(public / "task.json", task)
    (public / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    (public / "base.bundle").write_bytes(b"fixture base bundle")
    write_json(
        public / "environment.json",
        {
            "schema_version": "retro-project-environment-v1",
            "environment_id": "sha256:" + "4" * 64,
            "base_sha": "1" * 40,
            "image": task["environment"]["image"],
            "workdir": "/workspace",
            "network_during_run": "disabled",
            "env": {"API_KEY": "ENV_CREDENTIAL_VALUE"},
            "setup": [["python", "-c", "CANDIDATE_CODE_BODY"]],
            "validated": {"base": True, "outcome": True, "runs": 3},
        },
    )
    hashes = {
        "public_task_sha256": digest_file(public / "task.json"),
        "base_bundle_sha256": digest_file(public / "base.bundle"),
        "scorer_package_sha256": hashlib.sha256(("package:" + task_id).encode()).hexdigest(),
        "oracle_bundle_sha256": "7" * 64,
        "publication_sha256": hashlib.sha256(("publication:" + task_id).encode()).hexdigest(),
    }
    validation = {
        "schema_version": "retro-scorer-validation-v1",
        "task_id": task_id,
        "passed": True,
        "scorer_package_sha256": hashes["scorer_package_sha256"],
        "codes": [],
        "cases": [
            {
                "case_id": kind + "-1",
                "kind": kind,
                "targeted_component": "behavior"
                if kind in ("construct_changing", "construct_preserving", "regression")
                else None,
                "status": "scored",
                "ok": True,
                "score_total": 1.0 if kind == "oracle" else 0.98 if kind == "construct_preserving" else 0.0,
                "passed": kind in ("oracle", "construct_preserving"),
                "runs": 3,
                "hard_gate_failures": [] if kind in ("oracle", "construct_preserving") else ["behavior"],
                "component_values": {
                    "behavior": 1.0 if kind == "oracle" else 0.98 if kind == "construct_preserving" else 0
                },
                "code": None,
                "detail": "expected construct outcome",
                "candidate_code": "CANDIDATE_CODE_BODY",
            }
            for kind in VALIDATION_KINDS
        ],
        "repeatability": {
            "ok": True,
            "deterministic_mismatches": [],
            "performance_spread": 0.01,
            "judge_stdev": {"behavior": 0.02},
            "detail": "three independent runs",
        },
        "isolation_attestation": {
            "scorer_source": "HIDDEN_SCORER_SOURCE",
            "candidate": "CANDIDATE_CODE_BODY",
        },
    }
    write_json(private / "scorer-validation.json", validation)
    write_json(
        private / "scorer" / "scorer.json",
        {
            "schema_version": "retro-scorer-v1",
            "task_id": task_id,
            "mode": "code",
            "entrypoint": ["python", "HIDDEN_SCORER_SOURCE.py"],
            "source": "HIDDEN_SCORER_SOURCE",
            "runtime": {"image": "fixture@sha256:" + "8" * 64, "env": {"TOKEN": "ENV_CREDENTIAL_VALUE"}},
            "judge": {
                "agent_config": "JUDGE_CREDENTIAL_VALUE",
                "criteria": {"behavior": "ACCEPTED_PATCH_BODY"},
            },
            "components": [
                {"id": "behavior", "kind": "deterministic", "weight": 1.0, "hard_gate": True, "range": [0, 1]}
            ],
            "pass_threshold": 0.8,
            "required_artifacts": ["repo", "task"],
        },
    )
    (private / "scorer" / "main.py").write_text("HIDDEN_SCORER_SOURCE", encoding="utf-8")
    (private / "oracle.bundle").write_bytes(b"PRIVATE_ORACLE_BYTES")
    write_json(
        private / "source-link.json",
        {
            "source_id": source,
            "source_dir": "PRIVATE_SOURCE_DIRECTORY",
            "manifest_sha256": "8" * 64,
            "repo": {
                **task["repository"],
                "outcome_sha": "5" * 40,
                "outcome_tree": "6" * 40,
                "state_confidence": "exact",
                "base_resolution": "git_checkpoint",
                "outcome_resolution": "git_checkpoint",
                "root_at_capture": "PRIVATE_SOURCE_DIRECTORY",
            },
        },
    )
    write_json(
        private / "provenance.json",
        {
            "schema_version": "retro-task-provenance-v1",
            "task_id": task_id,
            "source_id": source,
            "build_id": "build-1",
            "kind": kind,
            "published_at": "2026-10-10T15:00:00Z",
            **hashes,
            "validation": validation,
            "task_definition": {
                "task_id": task_id,
                "kind": kind,
                "prompt": PROMPT,
                "prompt_provenance": {
                    "user_event_ids": ["ev-101", "ev-102"],
                    "mode": "resolved_user_messages",
                },
                "goal_segment": {
                    "introduced_event_id": "ev-101",
                    "closed_event_id": "ev-402",
                    "summary": "Robust parser",
                },
                "scorer_brief": {
                    "observables": [
                        {
                            "id": "behavior",
                            "description": "Parser handles malformed input",
                            "importance": "required",
                            "evidence": ["ev-401", "ev-402"],
                        }
                    ],
                    "regressions_to_protect": ["Existing valid input behavior"],
                    "forbidden_shortcuts": ["Do not hardcode fixture responses"],
                    "performance": [{"source": "HIDDEN_SCORER_SOURCE"}],
                    "residual_judgment": ["ASSISTANT_TRAJECTORY"],
                },
                "accepted_patch": "ACCEPTED_PATCH_BODY",
                "assistant_trajectory": "ASSISTANT_TRAJECTORY",
            },
            "audit": {
                "decision": "accept",
                "leakage": [],
                "overfit_checks": ["passed"],
                "missing_observables": [],
                "mutants": [{"code": "CANDIDATE_CODE_BODY"}],
                "evidence": [{"source": "HIDDEN_SCORER_SOURCE", "accepted_patch": "ACCEPTED_PATCH_BODY"}],
            },
            "assets": {"config": "JUDGE_CREDENTIAL_VALUE"},
            "ghostlab": {"trajectory": "ASSISTANT_TRAJECTORY"},
        },
    )
    return hashes


def make_attempt(
    root: Path, task_id: str, agent_id: str, seed: int, status: str, score: float | None, hashes: dict
) -> dict:
    payload = {
        "schema_version": "retro-benchmark-attempt-v1",
        "attempt_id": f"attempt-{task_id}-{agent_id}-{seed}",
        "task_id": task_id,
        "source_id": SOURCE_C if task_id == TASK_C else SOURCE_A,
        "agent_id": agent_id,
        "seed": seed,
        "status": status,
        "score": score,
        "passed": None if score is None else score >= 0.8,
        "pass_threshold": 0.8,
        "agent_config_sha256": hashlib.sha256(agent_id.encode()).hexdigest(),
        "base_bundle_sha256": hashes["base_bundle_sha256"],
        "scorer_package_sha256": hashes["scorer_package_sha256"],
        "candidate_state_sha256": "d" * 64,
        "input_sha256": "e" * 64,
        "components": []
        if score is None
        else [
            {
                "id": "behavior",
                "kind": "deterministic",
                "weight": 1,
                "hard_gate": True,
                "value": score,
                "gate_passed": score > 0,
                "scored": True,
            }
        ],
        "wall_time_ms": 1250,
        "tokens": {"input": 100, "output": 200},
        "cost_usd": 0.01,
        "error": None
        if score is not None
        else "Worker unavailable: api_key=sk-fakecredential123456789\nCANDIDATE_CODE_BODY",
        "warnings": [],
        "created_at": "2026-10-10T16:00:00Z",
        "artifact_run": "ASSISTANT_TRAJECTORY",
        "score_report": "ACCEPTED_PATCH_BODY",
        "candidate_code": "CANDIDATE_CODE_BODY",
        "stdout": "CANDIDATE_CODE_BODY",
    }
    write_json(root / "attempts" / task_id / agent_id / f"seed-{seed}" / "attempt.json", payload)
    return payload


def aggregate(attempts: list[dict], eval_id: str) -> dict:
    agents = []
    for agent_id in sorted({attempt["agent_id"] for attempt in attempts}):
        records = [attempt for attempt in attempts if attempt["agent_id"] == agent_id]
        tasks = []
        for task_id in sorted({attempt["task_id"] for attempt in records}):
            task_records = [attempt for attempt in records if attempt["task_id"] == task_id]
            scores = [attempt["score"] for attempt in task_records if attempt["status"] == "scored"]
            tasks.append(
                {
                    "task_id": task_id,
                    "source_id": task_records[0]["source_id"],
                    "agent_id": agent_id,
                    "mean_score": statistics.mean(scores) if scores else None,
                    "std_score": statistics.pstdev(scores) if scores else None,
                    "pass_rate": statistics.mean(score >= 0.8 for score in scores) if scores else None,
                    "pass_threshold": 0.8,
                    "scores": scores,
                    "requested_attempts": len(task_records),
                    "scored_attempts": len(scores),
                    "status_counts": dict(Counter(attempt["status"] for attempt in task_records)),
                }
            )
        means = [task["mean_score"] for task in tasks if task["mean_score"] is not None]
        passes = [task["pass_rate"] for task in tasks if task["pass_rate"] is not None]
        scored = sum(attempt["status"] == "scored" for attempt in records)
        agents.append(
            {
                "agent_id": agent_id,
                "benchmark_score": statistics.mean(means) if means else None,
                "pass_rate": statistics.mean(passes) if passes else None,
                "valid_coverage": scored / len(records),
                "requested_attempts": len(records),
                "scored_attempts": scored,
                "status_counts": dict(Counter(attempt["status"] for attempt in records)),
                "error_counts": dict(
                    Counter(attempt["status"] for attempt in records if attempt["status"] != "scored")
                ),
                "tasks": tasks,
                "component_means": {"behavior": statistics.mean(means)} if means else {},
                "resources": {
                    "scored_attempts": scored,
                    "tokens_mean": {"input": 100, "output": 200},
                    "tokens_total": {"input": 100 * scored, "output": 200 * scored},
                    "wall_time_ms_mean": 1250,
                    "wall_time_ms_total": 1250 * scored,
                    "cost_usd_mean": 0.01 if scored else None,
                    "cost_usd_total": 0.01 * scored if scored else None,
                    "env": {"TOKEN": "ENV_CREDENTIAL_VALUE"},
                },
            }
        )
    return {
        "schema_version": "retro-benchmark-aggregate-v1",
        "name": NAME,
        "eval_id": eval_id,
        "generated_at": "2026-10-10T17:00:00Z",
        "agents": agents,
        "candidate_code": "CANDIDATE_CODE_BODY",
    }


@pytest.fixture
def benchmark_archive(archive: Layout) -> Layout:  # noqa: F811
    root = archive.root / "benchmarks" / NAME / "task-scorer"
    selected = [
        {
            "source_id": source,
            "host": source.split("__")[0],
            "session_id": source.split("__")[1],
            "base": {"sha": "1" * 40},
            "outcome": {"sha": "5" * 40},
            "environment": {"env": {"TOKEN": "ENV_CREDENTIAL_VALUE"}},
        }
        for source in (SOURCE_A, SOURCE_C)
    ]
    write_json(
        root / "selection.json",
        {
            "schema_version": "retro-taskset-selection-v1",
            "selected": selected,
            "rejections": [
                {
                    "source_id": "codex__ineligible",
                    "code": "NO_GIT_BASE",
                    "detail": "No exact Git checkpoint",
                    "evidence": {"candidate_code": "CANDIDATE_CODE_BODY"},
                }
            ],
            "counts": {"selected": 2, "rejected": 1, "by_code": {"NO_GIT_BASE": 1}},
        },
    )
    write_json(
        root / "bundles.json",
        {
            "schema_version": "retro-taskset-bundles-v1",
            "name": NAME,
            "sources": [
                {"source_id": SOURCE_A, "status": "bundled", "content_sha256": "f" * 64},
                {
                    "source_id": SOURCE_C,
                    "status": "skipped",
                    "code": "BUNDLE_UNAVAILABLE",
                    "detail": "Repository unavailable",
                },
            ],
            "counts": {"bundled": 1, "skipped": 1, "by_status": {"bundled": 1, "skipped": 1}},
        },
    )
    write_json(
        root / "builds" / "build-1" / "build.json",
        {
            "schema_version": "retro-taskset-build-v1",
            "name": NAME,
            "build_id": "build-1",
            "published_task_ids": [TASK_A, TASK_B, TASK_C],
            "rejections": [
                {
                    "source_id": SOURCE_C,
                    "task_id": "rejected-task",
                    "code": "SCORER_OVERFIT",
                    "detail": "Construct-preserving mutant changes score",
                    "stage": "scorer_validated",
                }
            ],
            "sources": [
                {
                    "source_id": SOURCE_A,
                    "stage": "published",
                    "status": "ok",
                    "published_task_ids": [TASK_A, TASK_B],
                    "rejections": [],
                    "warnings": [],
                    "error": None,
                },
                {
                    "source_id": SOURCE_C,
                    "stage": "scorer_validated",
                    "status": "rejected",
                    "published_task_ids": [TASK_C],
                    "warnings": [],
                    "error": {
                        "code": "SCORER_OVERFIT",
                        "detail": "Construct-preserving mismatch",
                        "stdout": "CANDIDATE_CODE_BODY",
                    },
                },
            ],
        },
    )
    write_json(root / "active-tasks.json", {"task_ids": [TASK_A, TASK_B]})
    metadata = []
    hashes = {}
    for task_id in (TASK_A, TASK_B, TASK_C):
        source = SOURCE_C if task_id == TASK_C else SOURCE_A
        repo = "fixture/extra" if task_id == TASK_C else "fixture/core"
        kind = "adjacent" if task_id == TASK_B else "replay"
        hashes[task_id] = published_task(root / "tasks" / task_id, task_id, source, repo, kind=kind)
        metadata.append(
            {
                "task_id": task_id,
                "source_id": source,
                "repo_id": repo,
                "kind": kind,
                "split": "train" if task_id == TASK_C else "test",
                "group_id": "repo-extra" if task_id == TASK_C else "repo-core",
                "family": "parser",
                "tags": ["robustness", kind],
                "priority": 2,
                "provenance_grade": "anchored_reconstruction" if task_id == TASK_C else "exact_replay",
                "goal_summary": "Robust parser",
                "curation_reason": "Passed explicit state and scorer gates",
                "public_environment_sha256": digest_file(
                    root / "tasks" / task_id / "public" / "environment.json"
                ),
                **{
                    key: hashes[task_id][key]
                    for key in (
                        "public_task_sha256",
                        "base_bundle_sha256",
                        "scorer_package_sha256",
                        "publication_sha256",
                    )
                },
                "content_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
            }
        )
    suite_root = root / "suites" / SUITE
    suite_doc = write_suite(
        suite_root / "manifest.json",
        {
            "schema_version": "retro-benchmark-suite-v1",
            "name": NAME,
            "suite_id": SUITE,
            "created_at": "2026-10-10T15:30:00Z",
            "policy": {
                "split_unit": "repository",
                "split_ratios": {"train": 0.6, "validation": 0.2, "test": 0.2},
                "seed": 42,
                "allow_reconstructed": True,
                "include_judged": False,
                "private_agent_config": "JUDGE_CREDENTIAL_VALUE",
            },
            "tasks": metadata,
            "rejections": [
                {
                    "task_id": "rejected-task",
                    "code": "DUPLICATE_CONTENT",
                    "detail": "Duplicate task content",
                    "accepted_patch": "ACCEPTED_PATCH_BODY",
                }
            ],
            "summary": {
                "tasks": 3,
                "sources": 2,
                "repositories": 2,
                "groups": 2,
                "splits": {"train": 1, "validation": 0, "test": 2},
            },
            "warnings": [],
        },
    )
    for task_id in hashes:
        shutil.copytree(root / "tasks" / task_id, suite_root / "tasks" / task_id)
    eval_root = root / "evals" / EVAL
    write_json(
        eval_root / "cohort.json",
        {
            "schema_version": "retro-eval-cohort-v1",
            "name": NAME,
            "suite_id": SUITE,
            "suite_content_sha256": suite_doc["content_sha256"],
            "split": "test",
            "task_ids": [TASK_A, TASK_B],
            "seeds": [0, 1],
            "task_sources": {TASK_A: SOURCE_A, TASK_B: SOURCE_A},
        },
    )
    specs = [
        (TASK_A, "alpha", 0, "scored", 1.0),
        (TASK_A, "alpha", 1, "scored", 0.8),
        (TASK_B, "alpha", 0, "scored", 0.0),
        (TASK_B, "alpha", 1, "scorer_error", None),
        (TASK_A, "beta", 0, "scored", 0.6),
        (TASK_A, "beta", 1, "model_unavailable", None),
        (TASK_B, "beta", 0, "scorer_timeout", None),
        (TASK_B, "beta", 1, "scored", 1.0),
    ]
    attempts = [
        make_attempt(eval_root, task, agent, seed, status, score, hashes[task])
        for task, agent, seed, status, score in specs
    ]
    write_json(eval_root / "results.json", aggregate(attempts, EVAL))
    train_root = root / "evals" / "train-eval"
    write_json(
        train_root / "cohort.json",
        {
            "schema_version": "retro-eval-cohort-v1",
            "name": NAME,
            "suite_id": SUITE,
            "suite_content_sha256": suite_doc["content_sha256"],
            "split": "train",
            "task_ids": [TASK_C],
            "seeds": [0],
            "task_sources": {TASK_C: SOURCE_C},
        },
    )
    train = make_attempt(train_root, TASK_C, "alpha", 0, "scored", 0.99, hashes[TASK_C])
    write_json(train_root / "results.json", aggregate([train], "train-eval"))
    write_json(
        root / "curation.json",
        {
            "schema_version": "retro-task-curation-v1",
            "name": NAME,
            "created_at": "2026-10-10T15:30:00Z",
            "policy": suite_doc["policy"],
            "status": "curated",
            "suite_id": SUITE,
            "candidate_tasks": 3,
            "accepted_task_ids": [TASK_A, TASK_B, TASK_C],
            "rejections": [],
            "error": None,
        },
    )
    return archive


def benchmark_root(layout: Layout) -> Path:
    return layout.root / "benchmarks" / NAME / "task-scorer"


def reader(layout: Layout):
    return read_benchmarks(layout, read_catalog(layout))


def payload_of(
    view, *, task_id: str | None = None, suite_id: str | None = None, eval_id: str | None = None
) -> dict:
    for _, payload in view.payloads:
        if eval_id and payload.get("eval_id") == eval_id:
            return payload
        if task_id and payload.get("task_id") == task_id and payload.get("suite_id") == suite_id:
            return payload
        if task_id is None and eval_id is None and payload.get("name") == NAME and "selection" in payload:
            return payload
    raise AssertionError("payload not found")


def test_empty_benchmark_archive_is_lazy_and_backwards_compatible(empty_archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    report = build(empty_archive, out)
    _, manifest = read_shard(out / "data" / "manifest.js")
    benchmarks = manifest["benchmarks"]
    assert (
        benchmarks["tasksets"]
        == benchmarks["tasks"]
        == benchmarks["suites"]
        == benchmarks["evaluations"]
        == 0
    )
    assert report.benchmark_shards == 1
    assert not any("benchmark artifact warnings" in warning for warning in report.warnings)
    name, index = read_shard(out / benchmarks["index"]["file"])
    assert name == benchmarks["index"]["name"]
    assert index["missing"] and index["tasksets"] == [] and index["warnings"] == []
    html = (out / "index.html").read_text()
    assert 'data-route="tasks"' in html
    assert "data/benchmarks/" not in html


def test_selection_build_yield_and_explicit_rejections(benchmark_archive: Layout):
    view = reader(benchmark_archive)
    assert view.warnings == []
    data = payload_of(view)
    assert data["counts"] == {
        "selected": 2,
        "rejected": 1,
        "bundled": 1,
        "builds": 1,
        "tasks": 3,
        "active_tasks": 2,
        "suites": 1,
        "evaluations": 2,
    }
    assert data["selection"]["rejections"][0]["code"] == "NO_GIT_BASE"
    assert data["bundles"]["sources"][1]["code"] == "BUNDLE_UNAVAILABLE"
    assert data["builds"][0]["rejections"][0]["code"] == "SCORER_OVERFIT"
    assert data["builds"][0]["status_counts"] == {"ok": 1, "rejected": 1}
    assert data["tasks"][2]["active"] is False


def test_task_provenance_rubric_validation_and_repeatability(benchmark_archive: Layout):
    task = payload_of(reader(benchmark_archive), task_id=TASK_A)
    assert task["prompt"] == PROMPT
    assert task["repository"]["base_sha"] == "1" * 40
    assert task["repository"]["outcome_sha"] == "5" * 40
    assert task["environment"]["pinned"]
    assert task["environment"]["image"].endswith("3" * 64)
    assert task["environment"]["validation_runs"] == 3
    assert task["source"]["captured"]
    assert task["source"]["session_key"] == SOURCE_A.replace("__", "/", 1)
    assert task["provenance"]["user_event_ids"] == ["ev-101", "ev-102"]
    assert task["provenance"]["goal"]["closed_event_id"] == "ev-402"
    assert task["rubric"]["regressions_to_protect"] == ["Existing valid input behavior"]
    assert task["rubric"]["observables"][0]["evidence"] == ["ev-401", "ev-402"]
    assert task["audit"]["decision"] == "accept"
    assert task["audit"]["counts"]["mutants"] == 1
    assert task["validation"]["status"] == "passed"
    assert [case["kind"] for case in task["validation"]["cases"]] == list(VALIDATION_KINDS)
    assert task["validation"]["cases"][0]["score_total"] == 0
    assert task["validation"]["cases"][0]["ok"] is True
    assert task["validation"]["repeatability"]["judge_stdev"] == {"behavior": 0.02}
    assert task["warnings"] == []


def test_task_overview_describes_environment_and_grader_without_private_code(benchmark_archive: Layout):
    view = reader(benchmark_archive)
    task = payload_of(view, task_id=TASK_A)
    overview = task["overview"]
    assert overview["title"] == "Robust parser"
    assert overview["description"] == PROMPT
    assert overview["success_criteria"] == ["Parser handles malformed input"]
    assert overview["grader"] == {"label": "Behavioral checks", "criterion_count": 1}
    assert overview["environment"]["runtimes"] == ["Python"]
    assert overview["environment"]["image_label"] == "fixture"
    assert overview["environment"]["workdir"] == "/workspace"
    assert task["limits"]["wall_time_seconds"] == 1800
    assert len(view.ready_tasks) == 3
    assert view.draft_tasks == []
    for row in view.ready_tasks:
        assert row["suite_id"] == SUITE
        assert row["snapshot_valid"]
        assert "latest_evaluation" not in row
        assert row["overview"]["title"] == "Robust parser"
    text = json.dumps(view.ready_tasks)
    assert all(marker not in text for marker in PRIVATE_MARKERS)


def test_attempt_projection_preserves_failure_reason_without_reclassifying_zero_as_error(
    benchmark_archive: Layout
):
    path = benchmark_root(benchmark_archive) / "evals" / EVAL / "attempts" / TASK_A / "alpha" / "seed-0"
    attempt = json.loads((path / "attempt.json").read_text(encoding="utf-8"))
    attempt["score"] = 0
    attempt["passed"] = False
    attempt["components"][0]["value"] = 0
    attempt["components"][0]["gate_passed"] = False
    attempt["components"][0]["evidence"] = [
        {"kind": "state", "ref": "aggregation", "summary": "Failed checks: colliding notes were lost"}
    ]
    write_json(path / "attempt.json", attempt)
    data = payload_of(reader(benchmark_archive), eval_id=EVAL)
    projected = next(item for item in data["attempts"] if item["task_id"] == TASK_A and item["seed"] == 0)
    assert projected["status"] == "scored"
    assert projected["score"] == 0
    assert projected["error"] is None
    assert projected["failure_reason"] == "Failed checks: colliding notes were lost"
    assert projected["components"][0]["evidence"][0]["ref"] == "aggregation"


def test_default_catalog_uses_the_verified_curation_release_and_keeps_old_versions(benchmark_archive: Layout):
    root = benchmark_root(benchmark_archive)
    old = root / "suites" / "older-release"
    shutil.copytree(root / "suites" / SUITE, old)
    document = json.loads((old / "manifest.json").read_text())
    document["suite_id"] = old.name
    document["created_at"] = "2025-01-01T00:00:00Z"
    write_suite(old / "manifest.json", document)
    view = reader(benchmark_archive)
    assert {task["suite_id"] for task in view.ready_tasks} == {SUITE}
    assert len(payload_of(view)["suites"]) == 2
    assert view.tasksets[0]["preferred_suite_id"] == SUITE


def test_existing_foreign_experiment_directories_are_not_failed_job_records(benchmark_archive: Layout):
    foreign = benchmark_archive.root / "experiments" / "Mem"
    foreign.mkdir(parents=True)
    marker = foreign / "model-results.json"
    marker.write_text('{"model": "unrelated-memory-experiment"}', encoding="utf-8")
    view = reader(benchmark_archive)
    assert view.jobs == []
    assert not any("experiments/Mem" in warning for warning in view.warnings)
    assert marker.read_text(encoding="utf-8") == '{"model": "unrelated-memory-experiment"}'

def test_private_code_trajectories_credentials_and_evidence_never_enter_payloads(
    benchmark_archive: Layout, tmp_path: Path
):
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    text = "\n".join(path.read_text() for path in (out / "data" / "benchmarks").glob("*.js"))
    for marker in PRIVATE_MARKERS:
        assert marker not in text
    assert "sk-fakecredential123456789" not in text
    assert "api_key=[redacted]" in text
    assert "</script>" not in text and "<img" not in text
    assert "candidate_state_sha256" in text


def test_build_writes_lazy_scoped_benchmark_shards(benchmark_archive: Layout, tmp_path: Path):
    out = tmp_path / "dashboard"
    report = build(benchmark_archive, out)
    _, manifest = read_shard(out / "data" / "manifest.js")
    assert report.benchmark_tasksets == 1
    assert report.benchmark_tasks == 3
    assert report.benchmark_suites == 1
    assert report.benchmark_evaluations == 2
    _, index = read_shard(out / manifest["benchmarks"]["index"]["file"])
    assert "prompt" not in json.dumps(index)
    _, taskset = read_shard(out / index["tasksets"][0]["detail"]["file"])
    for task in taskset["tasks"] + taskset["suites"][0]["tasks"]:
        channel, detail = read_shard(out / task["detail"]["file"])
        assert channel == task["detail"]["name"]
        assert detail["task_id"] == task["task_id"]
        assert "attempts" not in detail
    for evaluation in taskset["evaluations"]:
        channel, detail = read_shard(out / evaluation["detail"]["file"])
        assert channel == evaluation["detail"]["name"]
        assert detail["eval_id"] == evaluation["eval_id"]
        assert "prompt" not in detail


def test_frozen_suite_counts_and_copies_do_not_follow_live_updates(benchmark_archive: Layout):
    root = benchmark_root(benchmark_archive)
    path = root / "tasks" / TASK_A / "public" / "task.json"
    task = json.loads(path.read_text())
    task["prompt"] = "Changed live task"
    write_json(path, task)
    (path.parent / "prompt.txt").write_text(task["prompt"])
    provenance_path = path.parent.parent / "private" / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["public_task_sha256"] = digest_file(path)
    write_json(provenance_path, provenance)
    view = reader(benchmark_archive)
    suite = payload_of(view)["suites"][0]
    assert suite["valid"]
    assert suite["summary"] == {
        "tasks": 3,
        "sources": 2,
        "repositories": 2,
        "groups": 2,
        "splits": {"train": 1, "validation": 0, "test": 2},
    }
    assert suite["groups"][0]["group_id"] == "repo-core"
    assert suite["groups"][0]["tasks"] == 2
    assert payload_of(view, task_id=TASK_A)["prompt"] == "Changed live task"
    assert payload_of(view, task_id=TASK_A, suite_id=SUITE)["prompt"] == PROMPT
    assert payload_of(view, eval_id=EVAL)["comparison_allowed"]


def test_agents_and_seed_attempts_are_scoped_to_one_cohort(benchmark_archive: Layout):
    view = reader(benchmark_archive)
    evaluation = payload_of(view, eval_id=EVAL)
    assert evaluation["comparison_allowed"] and evaluation["cohort_status"] == "verified"
    assert evaluation["cohort"]["task_ids"] == [TASK_A, TASK_B]
    assert evaluation["cohort"]["seeds"] == [0, 1]
    assert [agent["agent_id"] for agent in evaluation["agents"]] == ["alpha", "beta"]
    alpha, beta = evaluation["agents"]
    assert alpha["benchmark_score"] == pytest.approx(0.45)
    assert alpha["valid_coverage"] == 0.75
    assert beta["benchmark_score"] == pytest.approx(0.8)
    assert beta["valid_coverage"] == 0.5
    assert len(evaluation["attempts"]) == 8
    assert TASK_C not in {attempt["task_id"] for attempt in evaluation["attempts"]}
    assert all(
        attempt["score"] is None for attempt in evaluation["attempts"] if attempt["status"] != "scored"
    )
    assert any(attempt["score"] == 0 for attempt in evaluation["attempts"] if attempt["status"] == "scored")
    train = payload_of(view, eval_id="train-eval")
    assert train["comparison_allowed"] and train["agents"][0]["benchmark_score"] == 0.99


def test_rejected_curation_reports_zero_trusted_tasks_without_implying_release(
    empty_archive: Layout,  # noqa: F811
    tmp_path: Path,
):  # noqa: F811
    root = benchmark_root(empty_archive)
    write_json(
        root / "curation.json",
        {
            "schema_version": "retro-task-curation-v1",
            "name": NAME,
            "created_at": "2026-10-10T17:00:00Z",
            "policy": {
                "split_unit": "repository",
                "split_ratios": {"train": 0, "validation": 0, "test": 1},
                "seed": 0,
            },
            "status": "rejected",
            "suite_id": None,
            "candidate_tasks": 2,
            "accepted_task_ids": [],
            "rejections": [
                {
                    "task_id": TASK_A,
                    "code": "INVALID_SCORER_VALIDATION",
                    "detail": "Oracle validation missing",
                },
                {
                    "task_id": TASK_B,
                    "code": "UNTRUSTED_BASE",
                    "detail": "No exact or anchored repository base",
                },
            ],
            "error": "No tasks passed trusted-suite curation",
        },
    )
    view = reader(empty_archive)
    data = payload_of(view)
    assert data["curation"]["status"] == "rejected"
    assert data["counts"]["suites"] == data["counts"]["tasks"] == 0
    out = tmp_path / "dashboard"
    build(empty_archive, out)
    result = run_ui(out, "benchmarks", params={"name": NAME})
    assert "no successful empty release is implied" in result["text"]
    assert "No tasks passed trusted-suite curation" in result["text"]
    assert "INVALID_SCORER_VALIDATION" in result["text"] and "UNTRUSTED_BASE" in result["text"]
    assert "Latest curation outcome" in result["text"]


def test_claimed_successful_empty_curation_is_explicitly_unverified(
    benchmark_archive: Layout, tmp_path: Path
):
    path = benchmark_root(benchmark_archive) / "curation.json"
    curation = json.loads(path.read_text())
    curation["accepted_task_ids"] = []
    write_json(path, curation)
    view = reader(benchmark_archive)
    assert payload_of(view)["curation"]["status"] == "incomplete"
    assert payload_of(view)["curation"]["reported_status"] == "curated"
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmarks", params={"name": NAME})
    assert "no successful empty release is implied" in result["text"]
    assert "curation claims success without accepted tasks" in result["text"]


def rebind_suite(layout: Layout, suite: dict) -> None:
    for path in (benchmark_root(layout) / "evals").glob("*/cohort.json"):
        cohort = json.loads(path.read_text())
        cohort["suite_id"] = suite["suite_id"]
        cohort["suite_content_sha256"] = suite["content_sha256"]
        write_json(path, cohort)


def test_explicit_test_only_policy_provenance_grade_and_sampling_priority(
    benchmark_archive: Layout, tmp_path: Path
):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "manifest.json"
    suite = json.loads(path.read_text())
    suite["policy"]["split_ratios"] = {"train": 0, "validation": 0, "test": 1}
    for task in suite["tasks"]:
        task["split"] = "test"
        task["priority"] = 100
    suite["summary"]["splits"] = {"train": 0, "validation": 0, "test": 3}
    suite["summary"]["rejections"] = {"DUPLICATE_CONTENT": 1}
    suite = write_suite(path, suite)
    rebind_suite(benchmark_archive, suite)
    view = reader(benchmark_archive)
    curated = payload_of(view)["suites"][0]
    assert curated["valid"] and curated["summary"]["splits"]["test"] == 3
    assert curated["tasks"][2]["provenance_grade"] == "anchored_reconstruction"
    evaluation = payload_of(view, eval_id=EVAL)
    assert evaluation["comparison_allowed"]
    assert evaluation["agents"][0]["benchmark_score"] == pytest.approx(0.45)
    frozen = payload_of(view, task_id=TASK_C, suite_id=SUITE)
    assert frozen["provenance_grade"] == "anchored_reconstruction"
    assert frozen["validation"]["cases"][3]["targeted_component"] == "behavior"
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmarks", params={"name": NAME, "suite": SUITE})
    assert "Explicit test-only release: train 0%, validation 0%, test 100%" in result["text"]
    assert "Training sampling priority NEVER changes benchmark score" in result["text"]


def test_adjacent_tasks_are_labelled_synthesis_not_historical_replay(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "manifest.json"
    suite = json.loads(path.read_text())
    suite["tasks"][0]["provenance_grade"] = "adjacent_synthesis"
    suite = write_suite(path, suite)
    rebind_suite(benchmark_archive, suite)
    view = reader(benchmark_archive)
    frozen = payload_of(view, task_id=TASK_A, suite_id=SUITE)
    assert frozen["provenance_grade"] == "adjacent_synthesis"


def test_evaluation_surfaces_source_uncertainty_and_pass_at_k(benchmark_archive: Layout, tmp_path: Path):
    path = benchmark_root(benchmark_archive) / "evals" / EVAL / "results.json"
    results = json.loads(path.read_text())
    results["agents"][0]["score_interval"] = {
        "method": "source_percentile_bootstrap", "confidence": 0.95,
        "lower": 0.2, "upper": 0.8, "sources": 3, "conditional_on_scored_sources": True,
    }
    results["agents"][0]["pass_at_k"] = {"1": 0.5, "2": 0.75}
    write_json(path, results)
    view = reader(benchmark_archive)
    agent = payload_of(view, eval_id=EVAL)["agents"][0]
    assert agent["score_interval"]["lower"] == pytest.approx(0.2)
    assert agent["pass_at_k"] == {"1": 0.5, "2": 0.75}
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    ui = run_ui(out, "benchmark-eval", id=NAME + "/" + EVAL)
    assert "95% source interval" in ui["text"]
    assert "Pass@k uses scored trials only" in ui["text"]
    detail = run_ui(out, "benchmark-task", id=NAME + "/" + TASK_C, params={"suite": SUITE})
    assert (
        "anchored_reconstruction" in detail["text"]
        and "Passed explicit state and scorer gates" in detail["text"]
    )
    assert "target component" in detail["text"]


def test_source_grouped_positive_splits_warn_about_repository_overlap(
    benchmark_archive: Layout, tmp_path: Path
):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "manifest.json"
    suite = json.loads(path.read_text())
    suite["policy"]["split_unit"] = "source"
    suite = write_suite(path, suite)
    rebind_suite(benchmark_archive, suite)
    view = reader(benchmark_archive)
    assert payload_of(view)["suites"][0]["valid"]
    assert payload_of(view, eval_id=EVAL)["comparison_allowed"]
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmarks", params={"name": NAME, "suite": SUITE})
    assert "Source-grouped splits share repository history" in result["text"]


def publish_cohort_aggregate(root: Path, attempts: list[dict]) -> None:
    cohort = json.loads((root / "cohort.json").read_text())
    results = aggregate(attempts, root.name)
    for agent in results["agents"]:
        agent["requested_attempts"] = len(cohort["task_ids"]) * len(cohort["seeds"])
        agent["valid_coverage"] = agent["scored_attempts"] / agent["requested_attempts"]
        for task in agent["tasks"]:
            task["requested_attempts"] = len(cohort["seeds"])
    results["cohort"] = cohort
    write_json(root / "results.json", results)


def test_missing_requested_cells_reduce_coverage_not_reward_or_comparability(
    benchmark_archive: Layout, tmp_path: Path
):
    root = benchmark_root(benchmark_archive) / "evals" / EVAL
    (root / "attempts" / TASK_A / "alpha" / "seed-1" / "attempt.json").unlink()
    attempts = [json.loads(path.read_text()) for path in root.glob("attempts/*/*/seed-*/attempt.json")]
    publish_cohort_aggregate(root, attempts)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert evaluation["comparison_allowed"]
    alpha = evaluation["agents"][0]
    assert alpha["requested_attempts"] == 4 and alpha["scored_attempts"] == 2
    assert alpha["benchmark_score"] == 0.5 and alpha["valid_coverage"] == 0.5
    assert alpha["missing_attempts"] == 1
    assert evaluation["missing_attempts"] == [
        {
            "task_id": TASK_A,
            "agent_id": "alpha",
            "seed": 1,
            "source_id": SOURCE_A,
            "status": "missing_artifact",
            "score": None,
            "passed": None,
            "synthetic": True,
            "error": "Requested cohort cell has no published attempt.json; reward unavailable.",
        }
    ]
    assert any("coverage is reduced, not zero reward" in warning for warning in evaluation["warnings"])
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmark-eval", id=NAME + "/" + EVAL)
    assert "0.500" in result["text"] and "50.0%" in result["text"]
    assert "1 requested cells have no artifact" in result["text"]
    table = next(
        table for table in result["tables"] if table["rows"][0][:4] == ["task", "agent", "seed", "status"]
    )
    missing = next(row for row in table["rows"][1:] if row[3] == "missing_artifact")
    assert missing[4] == "—" and "no published attempt.json" in missing[8]


def test_all_failures_with_missing_cells_show_no_numeric_score(benchmark_archive: Layout, tmp_path: Path):
    root = benchmark_root(benchmark_archive) / "evals" / EVAL
    shutil.rmtree(root / "attempts")
    provenance = json.loads(
        (benchmark_root(benchmark_archive) / "tasks" / TASK_A / "private" / "provenance.json").read_text()
    )
    attempt = make_attempt(root, TASK_A, "broken", 0, "agent_error", None, provenance)
    publish_cohort_aggregate(root, [attempt])
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert evaluation["comparison_allowed"]
    agent = evaluation["agents"][0]
    assert agent["benchmark_score"] is None and agent["pass_rate"] is None
    assert agent["valid_coverage"] == 0 and agent["requested_attempts"] == 4
    assert len(evaluation["missing_attempts"]) == 3
    assert all(task["mean_score"] is None for task in agent["tasks"])
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmark-eval", id=NAME + "/" + EVAL)
    table = next(table for table in result["tables"] if table["rows"][0][:2] == ["agent", "benchmark score"])
    assert table["rows"][1][1:7] == ["—", "—", "—", "—", "0.0%", "0 / 4"]
    assert "missing_artifact" in result["text"] and "NOT zero reward" in result["text"]


@pytest.mark.parametrize("conflict", [False, True])
def test_optional_aggregate_cohort_must_match_binding(benchmark_archive: Layout, conflict: bool):
    root = benchmark_root(benchmark_archive) / "evals" / EVAL
    results = json.loads((root / "results.json").read_text())
    results["cohort"] = json.loads((root / "cohort.json").read_text())
    if conflict:
        results["cohort"]["suite_content_sha256"] = "0" * 64
    write_json(root / "results.json", results)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert evaluation["comparison_allowed"] is not conflict
    if conflict:
        assert any("aggregate cohort differs" in warning for warning in evaluation["warnings"])


def test_suite_manifest_checksum_and_task_source_binding_are_verified(benchmark_archive: Layout):
    root = benchmark_root(benchmark_archive)
    path = root / "suites" / SUITE / "manifest.json"
    suite = json.loads(path.read_text())
    suite["tasks"][0]["priority"] = 999
    write_json(path, suite)
    view = reader(benchmark_archive)
    assert not payload_of(view)["suites"][0]["valid"]
    assert any("manifest content checksum mismatch" in warning for warning in view.warnings)
    assert not payload_of(view, eval_id=EVAL)["comparison_allowed"]


def test_missing_required_task_content_digest_cannot_verify_a_cohort(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "manifest.json"
    suite = json.loads(path.read_text())
    suite["tasks"][0].pop("content_sha256")
    suite = write_suite(path, suite)
    rebind_suite(benchmark_archive, suite)
    view = reader(benchmark_archive)
    assert not payload_of(view)["suites"][0]["valid"]
    assert not payload_of(view, eval_id=EVAL)["comparison_allowed"]
    assert any("tasks.content_sha256" in warning for warning in view.warnings)


def test_cohort_task_sources_conflicts_are_not_silently_normalized(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json"
    cohort = json.loads(path.read_text())
    cohort["task_sources"][TASK_A] = SOURCE_C
    write_json(path, cohort)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert not evaluation["comparison_allowed"]
    assert any("task_sources differs" in warning for warning in evaluation["warnings"])


def test_suite_named_live_has_a_distinct_frozen_task_channel(benchmark_archive: Layout):
    root = benchmark_root(benchmark_archive)
    (root / "suites" / SUITE).rename(root / "suites" / "live")
    path = root / "suites" / "live" / "manifest.json"
    suite = json.loads(path.read_text())
    suite["suite_id"] = "live"
    suite = write_suite(path, suite)
    rebind_suite(benchmark_archive, suite)
    view = reader(benchmark_archive)
    data = payload_of(view)
    assert data["tasks"][0]["detail"] != data["suites"][0]["tasks"][0]["detail"]
    assert payload_of(view, task_id=TASK_A, suite_id="live")["prompt"] == PROMPT
    assert payload_of(view, eval_id=EVAL)["comparison_allowed"]


@pytest.mark.parametrize("value", [True, "0.5", 10**400, -1, float("inf")])
def test_malformed_aggregate_numbers_are_graceful_unavailable_values(benchmark_archive: Layout, value):
    path = benchmark_root(benchmark_archive) / "evals" / EVAL / "results.json"
    results = json.loads(path.read_text())
    results["agents"][0]["benchmark_score"] = value
    write_json(path, results)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert evaluation["agents"][0]["benchmark_score"] is None
    assert not evaluation["comparison_allowed"]
    assert any("invalid numeric value" in warning for warning in evaluation["warnings"])


@pytest.mark.parametrize(
    "relative",
    [
        "suites/curated-v1/manifest.json",
        f"tasks/{TASK_A}/private/provenance.json",
        f"evals/{EVAL}/results.json",
    ],
)
def test_malformed_unicode_cannot_crash_build_hashing_or_event_links(
    benchmark_archive: Layout,
    tmp_path: Path,
    relative: str,
):
    path = benchmark_root(benchmark_archive) / relative
    payload = json.loads(path.read_text())
    payload["malformed_text"] = "\ud800"
    write_json(path, payload)
    out = tmp_path / "dashboard"
    report = build(benchmark_archive, out)
    assert report.index_path.exists()
    view = reader(benchmark_archive)
    assert any(relative in warning and "malformed Unicode" in warning for warning in view.warnings)
    ui = run_ui(out, "benchmarks", params={"name": NAME})
    assert "malformed Unicode" in ui["text"]


@pytest.mark.parametrize(
    "change",
    [
        {"suite_content_sha256": "0" * 64},
        {"suite_id": "missing-suite"},
        {"split": "validation"},
        {"task_ids": [TASK_A, TASK_C]},
        {"seeds": [0, 0]},
        {"name": "another-taskset"},
    ],
)
def test_incompatible_cohorts_never_enable_comparison(benchmark_archive: Layout, change: dict):
    path = benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json"
    doc = json.loads(path.read_text())
    doc.update(change)
    write_json(path, doc)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert evaluation["cohort_status"] == "incompatible"
    assert not evaluation["comparison_allowed"]
    assert any("cohort does not match" in warning for warning in evaluation["warnings"])


@pytest.mark.parametrize(
    "relative",
    [
        "selection.json",
        "builds/build-1/build.json",
        "suites/curated-v1/manifest.json",
        f"tasks/{TASK_A}/private/provenance.json",
        f"tasks/{TASK_A}/private/scorer-validation.json",
        f"tasks/{TASK_A}/private/scorer/scorer.json",
        f"evals/{EVAL}/results.json",
    ],
)
def test_malformed_artifacts_warn_without_breaking_build(
    benchmark_archive: Layout, tmp_path: Path, relative: str
):
    path = benchmark_root(benchmark_archive) / relative
    path.write_text('{"broken":', encoding="utf-8")
    out = tmp_path / "dashboard"
    report = build(benchmark_archive, out)
    assert report.index_path.exists()
    assert any("benchmark artifact warnings" in warning for warning in report.warnings)
    _, manifest = read_shard(out / "data" / "manifest.js")
    _, index = read_shard(out / manifest["benchmarks"]["index"]["file"])
    assert any(relative in warning and "malformed JSON" in warning for warning in index["warnings"])


def test_missing_suite_task_is_not_replaced_by_live_task(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "tasks" / TASK_A
    shutil.rmtree(path)
    view = reader(benchmark_archive)
    frozen = payload_of(view, task_id=TASK_A, suite_id=SUITE)
    assert frozen["prompt"] is None
    assert any("published artifact missing" in warning for warning in frozen["warnings"])
    assert payload_of(view, task_id=TASK_A)["prompt"] == PROMPT
    assert not payload_of(view)["suites"][0]["valid"]
    assert not payload_of(view, eval_id=EVAL)["comparison_allowed"]


def test_missing_mandatory_construct_and_repeatability_are_explicit(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "tasks" / TASK_A / "private" / "scorer-validation.json"
    doc = json.loads(path.read_text())
    doc["cases"] = doc["cases"][:-1]
    doc["repeatability"] = {}
    write_json(path, doc)
    task = payload_of(reader(benchmark_archive), task_id=TASK_A)
    assert task["validation"]["status"] == "incomplete"
    assert task["validation"]["missing_kinds"] == ["regression"]
    assert any("regression" in warning for warning in task["warnings"])
    assert any("repeatability decision missing" in warning for warning in task["warnings"])


def test_cross_split_groups_and_misreported_counts_disable_suite(benchmark_archive: Layout):
    path = benchmark_root(benchmark_archive) / "suites" / SUITE / "manifest.json"
    doc = json.loads(path.read_text())
    doc["tasks"][1]["split"] = "train"
    write_json(path, doc)
    view = reader(benchmark_archive)
    suite = payload_of(view)["suites"][0]
    assert not suite["valid"]
    assert any("crosses suite splits" in warning for warning in suite["warnings"])
    assert any("declared suite summary differs" in warning for warning in suite["warnings"])
    assert not payload_of(view, eval_id=EVAL)["comparison_allowed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", 3),
        ("task_id", TASK_C),
        ("base_bundle_sha256", "0" * 64),
        ("scorer_package_sha256", "0" * 64),
        ("agent_config_sha256", "0" * 64),
    ],
)
def test_attempt_identity_and_frozen_hash_mismatches_disable_comparison(
    benchmark_archive: Layout, field: str, value
):
    path = (
        benchmark_root(benchmark_archive)
        / "evals"
        / EVAL
        / "attempts"
        / TASK_A
        / "alpha"
        / "seed-0"
        / "attempt.json"
    )
    doc = json.loads(path.read_text())
    doc[field] = value
    write_json(path, doc)
    evaluation = payload_of(reader(benchmark_archive), eval_id=EVAL)
    assert not evaluation["comparison_allowed"]
    assert evaluation["warnings"]


def test_infra_reward_is_suppressed_and_malformed_numbers_are_not_js_nan(
    benchmark_archive: Layout, tmp_path: Path
):
    root = benchmark_root(benchmark_archive) / "evals" / EVAL
    path = root / "attempts" / TASK_B / "alpha" / "seed-1" / "attempt.json"
    attempt = json.loads(path.read_text())
    attempt["score"] = 0
    write_json(path, attempt)
    results_path = root / "results.json"
    results = json.loads(results_path.read_text())
    results["agents"][0]["benchmark_score"] = float("nan")
    results["agents"][0]["resources"]["cost_usd_total"] = float("inf")
    write_json(results_path, results)
    view = reader(benchmark_archive)
    evaluation = payload_of(view, eval_id=EVAL)
    assert evaluation["agents"][0]["benchmark_score"] is None
    assert not evaluation["comparison_allowed"]
    assert next(a for a in evaluation["attempts"] if a["status"] == "scorer_error")["score"] is None
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    for shard in (out / "data" / "benchmarks").glob("*.js"):
        assert "NaN" not in shard.read_text() and "Infinity" not in shard.read_text()


def test_symlink_artifacts_are_not_followed(benchmark_archive: Layout, tmp_path: Path):
    private = tmp_path / "unrelated-private.json"
    private.write_text('{"private":"ENV_CREDENTIAL_VALUE"}')
    path = benchmark_root(benchmark_archive) / "tasks" / TASK_A / "private" / "provenance.json"
    path.unlink()
    path.symlink_to(private)
    task = payload_of(reader(benchmark_archive), task_id=TASK_A)
    assert any("symlink artifact skipped" in warning for warning in task["warnings"])
    assert "ENV_CREDENTIAL_VALUE" not in json.dumps(task)


def test_benchmark_reader_does_not_read_bundles_scorer_source_or_score_report(
    benchmark_archive: Layout, monkeypatch
):
    original = Path.read_bytes

    def read_bytes(path):
        assert path.suffix != ".bundle"
        assert path.name not in ("main.py", "score-report.json")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert payload_of(reader(benchmark_archive), eval_id=EVAL)["comparison_allowed"]


def test_benchmark_publication_failure_keeps_previous_complete_generation(
    benchmark_archive: Layout, tmp_path: Path, monkeypatch
):
    out = tmp_path / "dashboard"
    build_and_publish(benchmark_archive, out)
    previous = (out / "current").resolve()
    published = (out / "data" / "benchmarks" / "index.js").read_bytes()
    build_module = importlib.import_module("retro.dashboard.build")
    original = build_module.write_benchmarks

    def fail(view, data_dir):
        original(view, data_dir)
        raise RuntimeError("benchmark publication interrupted")

    monkeypatch.setattr(build_module, "write_benchmarks", fail)
    with pytest.raises(RuntimeError, match="benchmark publication interrupted"):
        build_and_publish(benchmark_archive, out)
    assert (out / "current").resolve() == previous
    assert (out / "data" / "benchmarks" / "index.js").read_bytes() == published
    assert not list((out / ".retro-builds").glob(".staging-*"))


NODE_UI = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
class Element {
  constructor(tag, type=1, text="") {
    this.tagName = tag.toUpperCase(); this.nodeType = type; this.children = [];
    this.attributes = {}; this.listeners = {}; this.parentNode = null;
    this.style = {}; this._text = text; this.value = ""; this.hidden = false;
  }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set textContent(value) {
    this._text = String(value); this.children.forEach(c => c.parentNode = null); this.children = [];
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "value") this.value = String(value);
  }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  appendChild(child) {
    if (!(child instanceof Element)) throw Error("invalid DOM child " + String(child));
    this.children.push(child); child.parentNode = this;
    if (this.tagName === "SELECT" && child.attributes.selected !== undefined) this.value = child.value;
    if (this.tagName === "HEAD" && child.tagName === "SCRIPT") {
      scripts.push(child.src);
      const shard = input.shards[child.src];
      if (shard) RETRO_DATA(shard[0], shard[1]); else if (child.onerror) child.onerror();
    }
    return child;
  }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; return child;
  }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  focus() {}
  insertBefore(child, existing) {
    const index = this.children.indexOf(existing);
    this.children.splice(index < 0 ? this.children.length : index, 0, child); child.parentNode = this;
    return child;
  }
  querySelector(selector) { return walk(this).find(n => n.tagName.toLowerCase() === selector) || null; }
}
function walk(node) { return [node].concat(node.children.flatMap(walk)); }
const scripts = [];
global.window = global;
global.document = {
  createElement: tag => new Element(tag), createTextNode: text => new Element("#text", 3, String(text)),
  createElementNS: (_, tag) => new Element(tag), head: new Element("head")
};
global.location = {hash: "#/overview", protocol: "file:", hostname: ""};
const storage = input.storage || {};
global.localStorage = {
  getItem: key => {
    if (input.storageUnavailable) throw Error("Storage denied");
    return storage[key] || null;
  },
  setItem: (key, value) => {
    if (input.storageUnavailable) throw Error("Storage denied");
    storage[key] = value;
  }
};
global.RETRO = {data: {manifest: input.manifest}, waiters: {}, loading: {}, failed: {}};
global.RETRO_DATA = function(name, payload) {
  RETRO.data[name] = payload;
  const callbacks = RETRO.waiters[name] || [];
  delete RETRO.waiters[name]; callbacks.forEach(callback => callback(payload));
};
["core.js", "views.js", "benchmarks.js", "experiments.js"].forEach(file => vm.runInThisContext(
  fs.readFileSync(process.argv[1] + "/" + file, "utf8"), {filename: file}
));
const root = new Element("main");
RETRO.views[input.route](root, {route: input.route, id: input.id || "", params: input.params || {}});
for (const action of (input.actions || [])) {
  const node = walk(root).find(n => n.attributes["aria-label"] === action.label);
  if (!node) throw Error("control not found: " + action.label);
  if ("value" in action) node.value = action.value;
  node.listeners[action.event || "change"]({target: node, preventDefault: () => {}});
}
const nodes = walk(root);
const tables = nodes.filter(n => n.tagName === "TABLE").map(table => ({
  label: table.attributes["aria-label"] || null,
  caption: (table.children.find(n => n.tagName === "CAPTION") || {}).textContent || null,
  rows: table.children.filter(n => n.tagName === "THEAD" || n.tagName === "TBODY")
    .flatMap(n => n.children).map(row => row.children.map(cell => cell.textContent))
}));
const filtered = input.filterState ?
  RETRO.benchmarks.filterTasks(input.filterTasks, input.filterState).map(t => t.task_id) : [];
console.log(JSON.stringify({
  text: root.textContent, scripts, tables, filtered, storage,
  notices: nodes.filter(n => n.className === "benchmark-warnings").map(n => ({hidden: n.hidden})),
  links: nodes.filter(n => n.tagName === "A").map(n => ({
    text: n.textContent, href: n.attributes.href, label: n.attributes["aria-label"] || null
  })),
  tags: nodes.map(n => n.tagName),
  details: nodes.filter(n => n.tagName === "DETAILS").map(n => ({
    summary: (n.children.find(c => c.tagName === "SUMMARY") || {}).textContent || "",
    open: n.attributes.open !== undefined
  })),
  scores: [RETRO.benchmarks.scoreValue({status:"scorer_error",score:0}),
    RETRO.benchmarks.scoreValue({status:"scored",score:0}),
    RETRO.benchmarks.agentScore({scored_attempts:0,benchmark_score:0})]
}));
"""


def run_ui(
    out: Path,
    route: str,
    *,
    id: str = "",
    params: dict | None = None,
    actions: list[dict] | None = None,
    filter_state: dict | None = None,
    filter_tasks: list | None = None,
    legacy_manifest: bool = False,
    missing_shard: bool = False,
    storage: dict | None = None,
    storage_unavailable: bool = False,
) -> dict:
    if not shutil.which("node"):
        pytest.skip("Node.js is needed for dashboard JavaScript/UI contracts")
    _, manifest = read_shard(out / "data" / "manifest.js")
    if legacy_manifest:
        manifest.pop("benchmarks", None)
    shards = {
        path.relative_to(out).as_posix(): read_shard(path)
        for path in (out / "data" / "benchmarks").glob("*.js")
    }
    if missing_shard:
        shards.pop("data/benchmarks/index.js", None)
    payload = {
        "manifest": manifest,
        "shards": shards,
        "route": route,
        "id": id,
        "params": params or {},
        "actions": actions or [],
        "filterState": filter_state,
        "filterTasks": filter_tasks or [],
        "storage": storage or {},
        "storageUnavailable": storage_unavailable,
    }
    result = subprocess.run(
        ["node", "-e", NODE_UI, str(ASSETS_DIR)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=ASSETS_DIR.parent,
    )
    return json.loads(result.stdout)


@pytest.fixture
def benchmark_dashboard(benchmark_archive: Layout, tmp_path: Path) -> Path:
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    return out


def test_js_empty_archive_and_old_manifest_render_explicit_empty_state(empty_archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build(empty_archive, out)
    result = run_ui(out, "benchmarks")
    assert "No benchmarks in this archive" in result["text"]
    assert result["scripts"] == ["data/benchmarks/index.js"]
    assert run_ui(out, "benchmarks", legacy_manifest=True)["scripts"] == []


def test_js_benchmarks_load_only_index_and_taskset_and_show_yield(benchmark_dashboard: Path):
    result = run_ui(benchmark_dashboard, "benchmarks", params={"name": NAME, "suite": SUITE})
    assert len(result["scripts"]) == 2
    assert result["scripts"][0] == "data/benchmarks/index.js"
    assert "/set-" in result["scripts"][1]
    for expected in (
        "NO_GIT_BASE",
        "BUNDLE_UNAVAILABLE",
        "SCORER_OVERFIT",
        "DUPLICATE_CONTENT",
        "repo-core",
        "train / validation / test",
        "Frozen curated suites",
    ):
        assert expected in result["text"]
    assert "Agent comparison — this cohort only" not in result["text"]
    assert result["scores"] == [None, 0, None]


def test_js_default_catalog_shows_readable_tasks_environments_and_graders(benchmark_dashboard: Path):
    result = run_ui(benchmark_dashboard, "benchmarks")
    assert len(result["scripts"]) == 2
    assert result["scripts"][-1] == "data/benchmarks/catalog.js"
    assert result["tags"].count("ARTICLE") == 3
    assert "Publication" not in result["text"]
    for text in (
        "Tasks & environments", "Robust parser", "Fresh isolated repository copy", "Python",
        "Behavioral checks", "Parser handles malformed input", "Pass score: at least 0.8",
        "Evaluation only", "Training split", "30m 0s time limit",
    ):
        assert text in result["text"]
    assert any(
        link["text"] == "Robust parser" and "/task/parser/" in link["href"]
        and "suite=" + SUITE in link["href"]
        for link in result["links"]
    )
    assert "View agent results" not in result["text"]
    assert not any(
        link["href"].startswith(("#/agent-run/", "#/benchmark-eval/")) for link in result["links"]
    )
    assert any(
        detail["summary"].startswith("Creation history & older versions") and not detail["open"]
        for detail in result["details"]
    )
    assert "source" not in result["scripts"][-1]


def test_js_task_cards_can_be_filtered_by_grader_requirements_and_tooling(benchmark_dashboard: Path):
    result = run_ui(
        benchmark_dashboard, "benchmarks",
        actions=[{"label": "Search tasks", "value": "malformed Python", "event": "input"}],
    )
    assert result["tags"].count("ARTICLE") == 3
    assert "3 of 3 tasks" in result["text"]
    empty = run_ui(
        benchmark_dashboard, "benchmarks",
        actions=[{"label": "Search tasks", "value": "missing runtime", "event": "input"}],
    )
    assert empty["tags"].count("ARTICLE") == 0
    assert "No tasks match these filters" in empty["text"]


def test_js_warning_explains_affected_old_runs_and_retains_collapsed_evidence(
    benchmark_archive: Layout, tmp_path: Path
):
    (benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json").unlink()
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmarks")
    assert "Some older runs have no recorded benchmark version" in result["text"]
    assert "We cannot confirm which exact tasks and grader those runs used" in result["text"]
    assert "not automatically to every task or to the latest verified results" in result["text"]
    assert "legacy/unbound evaluation" in result["text"]
    assert any(
        item["summary"].startswith("Technical evidence") and not item["open"]
        for item in result["details"]
    )
    assert result["tags"].count("ARTICLE") == 3


def test_js_task_details_lead_with_episode_and_grader_contract(benchmark_dashboard: Path):
    result = run_ui(
        benchmark_dashboard, "benchmark-task", id=NAME + "/" + TASK_A, params={"suite": SUITE}
    )
    text = result["text"]
    assert text.index("At a glance") < text.index("public_task_sha256")
    assert text.index("How the grader decides") < text.index("scorer_package_sha256")
    for expected in (
        "Full task instructions", "Environment & episode contract", "Reset", "Work", "Grade",
        "not a running training job or an RL optimizer", "Evaluation only — not training data",
        "A different correct implementation is allowed", "A required check failure makes the reward 0",
        "Rebuilt from a captured clean starting repository state", "Frozen benchmark version",
        "Grader inputs & output", "Inspect grader content", "retro-score-input-v1", "retro-score-report-v1",
    ):
        assert expected in text
    assert any(
        item["summary"] == "Provenance, exact versions & validation evidence" and not item["open"]
        for item in result["details"]
    )


def test_js_reconstructed_task_details_do_not_claim_exact_replay(benchmark_dashboard: Path):
    result = run_ui(
        benchmark_dashboard, "benchmark-task", id=NAME + "/" + TASK_C, params={"suite": SUITE}
    )
    assert "Reconstructed task" in result["text"]
    assert (
        "Rebuilt from a known Git revision because a clean starting state was not captured" in result["text"]
    )
    assert "This is a reconstructed task, not an exact replay" in result["text"]


def test_js_evals_contain_frozen_task_datasets_not_agent_results(benchmark_dashboard: Path):
    overview = run_ui(benchmark_dashboard, "evals")
    assert "Frozen task datasets" in overview["text"]
    assert SUITE in overview["text"]
    assert "View dataset tasks" in overview["text"]
    assert not any(
        link["href"].startswith(("#/agent-run/", "#/benchmark-eval/")) for link in overview["links"]
    )
    detail = run_ui(benchmark_dashboard, "dataset", id=NAME + "/" + SUITE)
    assert "It contains no agent results" in detail["text"]
    assert detail["tags"].count("ARTICLE") == 3
    assert "View agent results" not in detail["text"]
    assert "Agent comparison" not in detail["text"]


def test_js_experiments_own_saved_agent_runs_and_static_control_explanation(benchmark_dashboard: Path):
    result = run_ui(benchmark_dashboard, "experiments")
    assert "Saved agent runs" in result["text"]
    assert "Experiment jobs" in result["text"]
    assert "exported dashboard is read-only" in result["text"]
    assert "retro dashboard serve" in result["text"]
    assert any(link["href"].startswith("#/agent-run/") for link in result["links"])
    assert result["tags"].count("ARTICLE") == 0


def test_js_verification_warning_can_be_dismissed_persisted_and_restored(
    benchmark_archive: Layout, tmp_path: Path
):
    (benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json").unlink()
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    dismissed = run_ui(out, "tasks", actions=[{"label": "Dismiss verification warning", "event": "click"}])
    assert dismissed["notices"] == [{"hidden": True}]
    assert dismissed["storage"]
    later = run_ui(out, "tasks", storage=dismissed["storage"])
    assert later["notices"] == [{"hidden": True}]
    restored = run_ui(
        out, "tasks", storage=dismissed["storage"],
        actions=[{"label": "Show dismissed verification warning", "event": "click"}],
    )
    assert restored["notices"] == [{"hidden": False}]
    assert run_ui(out, "tasks", storage=restored["storage"])["notices"] == [{"hidden": False}]
    results = benchmark_root(benchmark_archive) / "evals" / EVAL / "results.json"
    results.write_text("{", encoding="utf-8")
    build(benchmark_archive, out)
    assert run_ui(out, "tasks", storage=dismissed["storage"])["notices"] == [{"hidden": False}]


def test_js_dismissal_surfaces_browser_storage_failure(benchmark_archive: Layout, tmp_path: Path):
    (benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json").unlink()
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(
        out, "tasks", storage_unavailable=True,
        actions=[{"label": "Dismiss verification warning", "event": "click"}],
    )
    assert result["notices"] == [{"hidden": True}]
    assert "Dismissal lasts for this tab only" in result["text"]


def test_js_suite_task_filters_cover_split_group_kind_tags_and_search(
    benchmark_dashboard: Path, benchmark_archive: Layout
):
    tasks = payload_of(reader(benchmark_archive))["suites"][0]["tasks"]
    result = run_ui(
        benchmark_dashboard,
        "benchmarks",
        params={"name": NAME, "suite": SUITE},
        filter_tasks=tasks,
        filter_state={"split": "test", "group": "repo-core", "kind": "adjacent", "q": "robustness parser"},
        actions=[
            {"label": "Kind", "value": "adjacent"},
            {"label": "Search tasks", "value": "robustness", "event": "input"},
            {"label": "View", "value": "table"},
        ],
    )
    assert result["filtered"] == [TASK_B]
    assert "1 of 3 tasks" in result["text"]
    task_tables = [table for table in result["tables"] if table["rows"][0][0] == "task / prompt"]
    assert len(task_tables[0]["rows"]) == 2
    assert TASK_B in task_tables[0]["rows"][1][0]


def test_js_task_drilldown_escapes_text_links_source_ids_and_shows_mandatory_evidence(
    benchmark_dashboard: Path,
):
    result = run_ui(benchmark_dashboard, "benchmark-task", id=NAME + "/" + TASK_A, params={"suite": SUITE})
    assert len(result["scripts"]) == 3 and "/task-" in result["scripts"][-1]
    assert PROMPT in result["text"]
    assert "IMG" not in result["tags"] and "SCRIPT" not in result["tags"]
    assert "1" * 40 in result["text"] and "5" * 40 in result["text"]
    assert "sha256:" + "3" * 64 in result["text"]
    for kind in VALIDATION_KINDS:
        assert kind in result["text"]
    assert "Repeatability" in result["text"] and "0.02" in result["text"]
    assert any(
        link["text"] == "ev-101" and "event=ev-101" in link["href"] and "#/session/codex/" in link["href"]
        for link in result["links"]
    )
    for marker in PRIVATE_MARKERS:
        assert marker not in result["text"]


def test_js_evaluation_comparison_heatmap_and_attempt_errors_never_pool_cohorts(benchmark_dashboard: Path):
    result = run_ui(benchmark_dashboard, "benchmark-eval", id=NAME + "/" + EVAL)
    assert len(result["scripts"]) == 3 and "/eval-" in result["scripts"][-1]
    assert "Agent comparison — this cohort only" in result["text"]
    assert "0.450" in result["text"] and "0.800" in result["text"]
    assert "75.0%" in result["text"] and "50.0%" in result["text"]
    assert "0.990" not in result["text"] and TASK_C not in result["text"]
    assert "NOT zero reward" in result["text"]
    heatmap = next(
        table for table in result["tables"] if table["label"] == "Mean scored reward by task and agent"
    )
    assert "— means unavailable, not zero" in heatmap["caption"]
    assert any(link["label"] and "attempts scored" in link["label"] for link in result["links"])
    attempt_table = next(
        table for table in result["tables"] if table["rows"][0][:4] == ["task", "agent", "seed", "status"]
    )
    errors = [row for row in attempt_table["rows"][1:] if row[3] != "scored"]
    assert len(errors) == 3
    assert all(row[4] == "—" and row[5] == "unavailable" for row in errors)
    assert all("Worker unavailable" in row[8] and "sk-fake" not in row[8] for row in errors)


def test_js_per_seed_filter_does_not_change_cohort_or_score(benchmark_dashboard: Path):
    result = run_ui(
        benchmark_dashboard,
        "benchmark-eval",
        id=NAME + "/" + EVAL,
        params={"task": TASK_A, "agent": "alpha"},
        actions=[{"label": "Seed", "value": "1"}],
    )
    assert "1 of 8 published per-seed attempts" in result["text"]
    assert "0.450" in result["text"] and "0.800" in result["text"]
    assert "Filters do not change the evaluation cohort" in result["text"]
    table = next(
        table for table in result["tables"] if table["rows"][0][:4] == ["task", "agent", "seed", "status"]
    )
    assert len(table["rows"]) == 2 and table["rows"][1][2] == "1"


def test_js_unbound_cohort_disables_heatmap_with_explicit_warning(benchmark_archive: Layout, tmp_path: Path):
    (benchmark_root(benchmark_archive) / "evals" / EVAL / "cohort.json").unlink()
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmark-eval", id=NAME + "/" + EVAL)
    assert "not a ranking" in result["text"]
    assert "legacy/unbound evaluation" in result["text"]
    assert not any(table["label"] == "Mean scored reward by task and agent" for table in result["tables"])
    assert "cohort.json" in result["text"]


def test_js_validation_preserves_exact_target_component_and_repeatability_values(
    benchmark_archive: Layout,
    tmp_path: Path,
):
    path = benchmark_root(benchmark_archive) / "tasks" / TASK_A / "private" / "scorer-validation.json"
    validation = json.loads(path.read_text())
    validation["cases"][4]["component_values"]["behavior"] = 0.94999
    validation["cases"][4]["score_total"] = 0.94999
    validation["cases"][4]["ok"] = False
    validation["repeatability"]["performance_spread"] = 0.05001
    validation["repeatability"]["ok"] = False
    validation["passed"] = False
    write_json(path, validation)
    out = tmp_path / "dashboard"
    build(benchmark_archive, out)
    result = run_ui(out, "benchmark-task", id=NAME + "/" + TASK_A)
    assert "0.94999" in result["text"] and "0.05001" in result["text"]
    assert "target component" in result["text"] and "behavior" in result["text"]


def test_js_missing_shard_is_reported_not_an_empty_success(benchmark_dashboard: Path):
    result = run_ui(benchmark_dashboard, "benchmarks", missing_shard=True)
    assert "Benchmark shard could not be loaded" in result["text"]
    assert "No benchmarks in this archive" not in result["text"]
