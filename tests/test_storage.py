"""Tests for retro.storage — Layout helpers."""
from __future__ import annotations

from pathlib import Path

from retro.storage import Layout, default_layout


def test_layout_paths():
    lay = Layout(Path("/tmp/rm"))
    assert lay.raw_dir("claude-code", "s1") == Path("/tmp/rm/raw/claude-code/s1")
    assert lay.normalized_path("codex", "t1") == Path("/tmp/rm/normalized/codex/t1.events.jsonl")
    assert lay.rendered_path("claude-code", "s1") == Path("/tmp/rm/rendered/claude-code/s1.md")
    assert lay.mined_json_path("codex", "t1", "skill_pro") == Path(
        "/tmp/rm/mined/skill_pro/codex/t1.json"
    )
    assert lay.mined_prompt_path("codex", "t1", "skill_pro") == Path(
        "/tmp/rm/mined/skill_pro/codex/t1.prompt.md"
    )


def test_ensure_creates_dirs(tmp_path: Path):
    lay = Layout(tmp_path / "rollout-memory")
    lay.ensure()
    for sub in ("raw", "normalized", "rendered", "mined"):
        assert (tmp_path / "rollout-memory" / sub).is_dir()


def test_list_imported_empty(tmp_path: Path):
    lay = Layout(tmp_path / "rollout-memory")
    lay.ensure()
    assert lay.list_imported("claude-code") == []
    assert lay.list_imported("codex") == []


def test_list_imported(tmp_path: Path):
    lay = Layout(tmp_path / "rollout-memory")
    lay.ensure()
    (tmp_path / "rollout-memory" / "raw" / "codex" / "thread-1").mkdir(parents=True)
    (tmp_path / "rollout-memory" / "raw" / "codex" / "thread-2").mkdir(parents=True)
    imported = lay.list_imported("codex")
    assert imported == ["thread-1", "thread-2"]


def test_default_layout():
    lay = default_layout("/tmp/test-rm")
    assert lay.root == Path("/tmp/test-rm").resolve()


def test_sft_layout_paths():
    lay = Layout(Path("/tmp/rm"))
    assert lay.sft_dataset_path("distill", "train") == Path("/tmp/rm/sft/datasets/distill/train.jsonl")
    assert lay.sft_manifest_path("distill") == Path("/tmp/rm/sft/datasets/distill/manifest.json")
    assert lay.sft_modal_script_path("run-1") == Path("/tmp/rm/sft/runs/run-1/modal_train.py")
    assert lay.sft_eval_report_path("run-1") == Path("/tmp/rm/sft/runs/run-1/eval/comparison.json")
