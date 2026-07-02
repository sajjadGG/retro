"""Filesystem layout helpers for rollout-memory/."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .schema import Host


@dataclass(frozen=True)
class Layout:
    root: Path

    def raw_dir(self, host: Host, session_id: str) -> Path:
        return self.root / "raw" / host / session_id

    def normalized_path(self, host: Host, session_id: str) -> Path:
        return self.root / "normalized" / host / f"{session_id}.events.jsonl"

    def rendered_path(self, host: Host, session_id: str) -> Path:
        return self.root / "rendered" / host / f"{session_id}.md"

    def mined_json_path(self, host: Host, session_id: str, method: str) -> Path:
        return self.root / "mined" / method / host / f"{session_id}.json"

    def mined_prompt_path(self, host: Host, session_id: str, method: str) -> Path:
        return self.root / "mined" / method / host / f"{session_id}.prompt.md"

    def memories_dir(self) -> Path:
        return self.root / "memories"

    def memory_items_path(self) -> Path:
        return self.memories_dir() / "items.jsonl"

    def memory_events_path(self) -> Path:
        return self.memories_dir() / "events.jsonl"

    def memory_index_path(self) -> Path:
        return self.memories_dir() / "index.sqlite"

    def sft_dir(self) -> Path:
        return self.root / "sft"

    def sft_dataset_dir(self, dataset_name: str) -> Path:
        return self.sft_dir() / "datasets" / dataset_name

    def sft_dataset_path(self, dataset_name: str, split: str) -> Path:
        return self.sft_dataset_dir(dataset_name) / f"{split}.jsonl"

    def sft_manifest_path(self, dataset_name: str) -> Path:
        return self.sft_dataset_dir(dataset_name) / "manifest.json"

    def sft_run_dir(self, run_name: str) -> Path:
        return self.sft_dir() / "runs" / run_name

    def sft_training_config_path(self, run_name: str) -> Path:
        return self.sft_run_dir(run_name) / "training-config.json"

    def sft_modal_script_path(self, run_name: str) -> Path:
        return self.sft_run_dir(run_name) / "modal_train.py"

    def sft_weights_dir(self, run_name: str) -> Path:
        return self.sft_run_dir(run_name) / "weights"

    def sft_eval_dir(self, run_name: str) -> Path:
        return self.sft_run_dir(run_name) / "eval"

    def sft_eval_plan_path(self, run_name: str) -> Path:
        return self.sft_eval_dir(run_name) / "plan.json"

    def sft_eval_report_path(self, run_name: str) -> Path:
        return self.sft_eval_dir(run_name) / "comparison.json"

    def ensure(self) -> None:
        for sub in ("raw", "normalized", "rendered", "mined", "memories", "sft"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def list_imported(self, host: Host) -> list[str]:
        host_dir = self.root / "raw" / host
        if not host_dir.exists():
            return []
        return sorted(p.name for p in host_dir.iterdir() if p.is_dir())

    def list_normalized(self, host: Host) -> list[str]:
        host_dir = self.root / "normalized" / host
        if not host_dir.exists():
            return []
        suffix = ".events.jsonl"
        return sorted(p.name[: -len(suffix)] for p in host_dir.glob(f"*{suffix}") if p.is_file())


def default_layout(root: Path | str = "rollout-memory") -> Layout:
    return Layout(Path(root).resolve())
