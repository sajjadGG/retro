from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .schema import Host, NormalizedEvent, read_events
from .storage import Layout
from .utils import event_text, truncate_summary

SFTRole = Literal["system", "user", "assistant", "tool_call", "tool"]
SUPPORTED_BENCHMARKS = {"swe-bench-verified", "terminal-bench-2.1"}
SUPPORTED_RUNNERS = {"agentarena", "touchstone"}

_FAILURE_PATTERNS = (
    re.compile(r"process exited with code\s+[1-9]\d*", re.IGNORECASE),
    re.compile(r"\b[1-9]\d*\s+failed\b", re.IGNORECASE),
    re.compile(r"syntaxerror", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\bexception:\b", re.IGNORECASE),
    re.compile(r"\berror:\b", re.IGNORECASE),
    re.compile(r"\baborted\b", re.IGNORECASE),
    re.compile(r"\bcancelled\b", re.IGNORECASE),
)


@dataclass
class RejectedSession:
    host: Host
    session_id: str
    reason: str
    stats: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "session_id": self.session_id,
            "reason": self.reason,
            "stats": self.stats,
        }


@dataclass
class CuratedDataset:
    dataset_name: str
    train_examples: list[dict[str, Any]]
    eval_examples: list[dict[str, Any]]
    rejected: list[RejectedSession]
    manifest: dict[str, Any]


def export_curated_dataset(
    layout: Layout,
    *,
    dataset_name: str,
    hosts: list[Host] | None = None,
    max_sessions: int | None = None,
    eval_fraction: float = 0.1,
    include_reasoning: bool = False,
) -> CuratedDataset:
    selected: list[dict[str, Any]] = []
    rejected: list[RejectedSession] = []
    for host, session_id, path in _iter_normalized_paths(layout, hosts):
        example, rejection = build_sft_example(
            path,
            host=host,
            session_id=session_id,
            include_reasoning=include_reasoning,
        )
        if example is not None:
            selected.append(example)
        elif rejection is not None:
            rejected.append(rejection)

    selected.sort(key=lambda item: (-int(item["quality_score"]), item["host"], item["session_id"]))
    curated = _diverse_sample(selected, max_sessions)
    train_examples, eval_examples = _split_examples(curated, eval_fraction=eval_fraction)

    dataset_dir = layout.sft_dataset_dir(dataset_name)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    train_path = layout.sft_dataset_path(dataset_name, "train")
    eval_path = layout.sft_dataset_path(dataset_name, "eval")
    _write_jsonl(train_path, train_examples)
    _write_jsonl(eval_path, eval_examples)

    manifest = {
        "dataset_name": dataset_name,
        "schema": "retro-sharegpt-v1",
        "selected_sessions": len(curated),
        "rejected_sessions": len(rejected),
        "train_examples": len(train_examples),
        "eval_examples": len(eval_examples),
        "include_reasoning": include_reasoning,
        "max_sessions": max_sessions,
        "eval_fraction": eval_fraction,
        "rejected": [item.to_dict() for item in rejected],
        "included": [
            {
                "id": item["id"],
                "host": item["host"],
                "session_id": item["session_id"],
                "quality_score": item["quality_score"],
                "stats": item["stats"],
            }
            for item in curated
        ],
    }
    layout.sft_manifest_path(dataset_name).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return CuratedDataset(
        dataset_name=dataset_name,
        train_examples=train_examples,
        eval_examples=eval_examples,
        rejected=rejected,
        manifest=manifest,
    )


def build_sft_example(
    normalized_path: Path,
    *,
    host: Host,
    session_id: str,
    include_reasoning: bool = False,
) -> tuple[dict[str, Any] | None, RejectedSession | None]:
    events = list(read_events(normalized_path))
    stats = _session_stats(events)
    messages: list[dict[str, Any]] = []
    for ev in events:
        message = _event_to_message(ev, include_reasoning=include_reasoning)
        if message is None:
            continue
        _append_message(messages, message)

    rejection_reason = _reject_reason(events, messages, stats)
    if rejection_reason is not None:
        return None, RejectedSession(host=host, session_id=session_id, reason=rejection_reason, stats=stats)

    example = {
        "id": f"{host}/{session_id}",
        "host": host,
        "session_id": session_id,
        "schema": "retro-sharegpt-v1",
        "quality_score": _quality_score(stats, messages),
        "messages": messages,
        "stats": stats,
    }
    return example, None


def write_modal_training_bundle(
    layout: Layout,
    *,
    dataset_name: str,
    run_name: str,
    base_model: str,
    output_format: str,
    gpu: str,
    max_seq_length: int,
    per_device_batch_size: int,
    gradient_accumulation_steps: int,
    num_train_epochs: int,
    learning_rate: float,
    modal_volume: str,
) -> dict[str, Path]:
    dataset_path = layout.sft_dataset_path(dataset_name, "train")
    if not dataset_path.exists():
        raise FileNotFoundError(f"No curated SFT dataset at {dataset_path}. Run `retro sft export` first.")

    run_dir = layout.sft_run_dir(run_name)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = layout.sft_training_config_path(run_name)
    script_path = layout.sft_modal_script_path(run_name)
    weights_dir = layout.sft_weights_dir(run_name)
    weights_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "dataset_name": dataset_name,
        "dataset_path": str(dataset_path),
        "run_name": run_name,
        "base_model": base_model,
        "output_format": output_format,
        "gpu": gpu,
        "max_seq_length": max_seq_length,
        "per_device_batch_size": per_device_batch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "num_train_epochs": num_train_epochs,
        "learning_rate": learning_rate,
        "modal_volume": modal_volume,
        "remote_output_dir": f"/vol/{run_name}/artifacts",
        "local_output_dir": str(weights_dir),
    }
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        _modal_training_script(
            config_name=config_path.name,
            app_name=f"retro-sft-{run_name}",
            gpu=gpu,
            volume_name=modal_volume,
        ),
        encoding="utf-8",
    )
    return {"config": config_path, "script": script_path, "weights_dir": weights_dir}


def maybe_run_modal_training(script_path: Path, config_path: Path) -> subprocess.CompletedProcess[str] | None:
    modal = shutil.which("modal")
    if modal is None:
        return None
    return subprocess.run(
        [modal, "run", str(script_path), "--", "--config", str(config_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def write_evaluation_plan(
    layout: Layout,
    *,
    run_name: str,
    benchmark: str,
    runner: str,
    base_model: str,
    tuned_model: str,
    base_results: Path | None = None,
    tuned_results: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if benchmark not in SUPPORTED_BENCHMARKS:
        raise ValueError(
            f"Unsupported benchmark {benchmark!r}; expected one of "
            f"{sorted(SUPPORTED_BENCHMARKS)}"
        )
    if runner not in SUPPORTED_RUNNERS:
        raise ValueError(
            f"Unsupported runner {runner!r}; expected one of "
            f"{sorted(SUPPORTED_RUNNERS)}"
        )

    eval_dir = layout.sft_eval_dir(run_name)
    eval_dir.mkdir(parents=True, exist_ok=True)
    if base_results is None:
        base_results = eval_dir / "base-results.jsonl"
    if tuned_results is None:
        tuned_results = eval_dir / "tuned-results.jsonl"

    plan = {
        "run_name": run_name,
        "benchmark": benchmark,
        "runner": runner,
        "models": {
            "base": base_model,
            "tuned": tuned_model,
        },
        "commands": {
            "base": _benchmark_command(runner, benchmark, base_model, base_results),
            "tuned": _benchmark_command(runner, benchmark, tuned_model, tuned_results),
        },
        "results": {
            "base": str(base_results),
            "tuned": str(tuned_results),
        },
    }
    layout.sft_eval_plan_path(run_name).write_text(
        json.dumps(plan, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = None
    if base_results.exists() and tuned_results.exists():
        summary = compare_benchmark_results(base_results, tuned_results)
        layout.sft_eval_report_path(run_name).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return plan, summary


def compare_benchmark_results(base_results: Path, tuned_results: Path) -> dict[str, Any]:
    base = summarize_benchmark_results(base_results)
    tuned = summarize_benchmark_results(tuned_results)
    metrics = {}
    for key in ("pass_at_1", "trajectory_success_rate", "tool_format_adherence"):
        metrics[key] = {
            "base": base[key],
            "tuned": tuned[key],
            "delta": round(tuned[key] - base[key], 4),
        }
    return {
        "benchmark": tuned.get("benchmark") or base.get("benchmark"),
        "counts": {
            "base_cases": base["total_cases"],
            "tuned_cases": tuned["total_cases"],
        },
        "metrics": metrics,
    }


def summarize_benchmark_results(results_path: Path) -> dict[str, Any]:
    rows = _read_result_rows(results_path)
    if not rows:
        raise ValueError(f"No benchmark rows found in {results_path}")

    total = len(rows)
    passed = sum(
        1
        for row in rows
        if _coerce_bool(row.get("pass") or row.get("passed") or row.get("resolved"))
    )
    trajectory = sum(
        1
        for row in rows
        if _coerce_bool(
            row.get("trajectory_success") or row.get("trajectory_success_rate") or row.get("resolved")
        )
    )
    tool_ok = sum(
        1
        for row in rows
        if _coerce_bool(
            row.get("tool_format_adherence")
            or row.get("tool_adherence")
            or row.get("tool_format_ok")
            or row.get("tool_calls_valid")
        )
    )
    benchmark = next((row.get("benchmark") for row in rows if row.get("benchmark")), None)
    return {
        "benchmark": benchmark,
        "total_cases": total,
        "pass_at_1": round(passed / total, 4),
        "trajectory_success_rate": round(trajectory / total, 4),
        "tool_format_adherence": round(tool_ok / total, 4),
    }


def _read_result_rows(results_path: Path) -> list[dict[str, Any]]:
    text = results_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if results_path.suffix == ".jsonl":
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    data = json.loads(text)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return [row for row in results if isinstance(row, dict)]
        return [data]
    return []


def _iter_normalized_paths(layout: Layout, hosts: list[Host] | None) -> list[tuple[Host, str, Path]]:
    selected_hosts = hosts or ["claude-code", "codex"]
    out: list[tuple[Host, str, Path]] = []
    for host in selected_hosts:
        for session_id in layout.list_normalized(host):
            out.append((host, session_id, layout.normalized_path(host, session_id)))
    return out


def _session_stats(events: list[NormalizedEvent]) -> dict[str, Any]:
    total = len(events)
    unknown = sum(1 for ev in events if ev.event_type == "unknown")
    errors = sum(1 for ev in events if ev.event_type == "error")
    assistant_messages = sum(
        1 for ev in events if ev.actor == "assistant" and ev.event_type == "message"
    )
    tool_calls = sum(
        1
        for ev in events
        if ev.actor == "assistant" and ev.event_type in {"tool_call", "command", "file_edit", "file_read"}
    )
    tool_results = sum(1 for ev in events if ev.actor == "tool")
    failures = sum(1 for ev in events if _is_failure_event(ev))
    return {
        "total_events": total,
        "unknown_events": unknown,
        "unknown_ratio": round(unknown / total, 4) if total else 0.0,
        "error_events": errors,
        "assistant_messages": assistant_messages,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "failure_events": failures,
    }


def _reject_reason(
    events: list[NormalizedEvent], messages: list[dict[str, Any]], stats: dict[str, Any]
) -> str | None:
    if not messages:
        return "no conversational content"
    if stats["assistant_messages"] == 0:
        return "no assistant messages"
    if stats["error_events"]:
        return "contains normalized error events"
    if stats["failure_events"]:
        return "contains failed tool output or syntax errors"
    if stats["unknown_ratio"] > 0.2:
        return "too many unknown events"
    if messages[-1]["role"] == "tool_call":
        return "rollout ends with unresolved tool call"
    return None


def _quality_score(stats: dict[str, Any], messages: list[dict[str, Any]]) -> int:
    return (
        stats["assistant_messages"] * 8
        + stats["tool_calls"] * 3
        + len(messages)
        - stats["unknown_events"] * 5
        - stats["failure_events"] * 25
    )


def _event_to_message(ev: NormalizedEvent, *, include_reasoning: bool) -> dict[str, Any] | None:
    if ev.event_type == "reasoning" and not include_reasoning:
        return None
    if ev.actor == "system" and ev.event_type == "message":
        return _message_dict("system", _safe_text(ev), False, ev)
    if ev.actor == "user" and ev.event_type == "message":
        return _message_dict("user", _safe_text(ev), False, ev)
    if ev.actor == "assistant" and ev.event_type == "message":
        return _message_dict("assistant", _safe_text(ev), True, ev)
    if ev.actor == "assistant" and ev.event_type == "reasoning":
        return _message_dict("assistant", _safe_text(ev), False, ev)
    if ev.actor == "assistant" and ev.event_type in {"tool_call", "command", "file_edit", "file_read"}:
        return _message_dict("tool_call", _tool_call_text(ev), False, ev, tool_name=_tool_name(ev))
    if ev.actor == "tool" and ev.event_type in {"tool_result", "command", "file_edit", "file_read"}:
        return _message_dict("tool", _tool_result_text(ev), False, ev, tool_name=_tool_name(ev))
    return None


def _message_dict(
    role: SFTRole,
    content: str,
    loss_mask: bool,
    ev: NormalizedEvent,
    *,
    tool_name: str | None = None,
) -> dict[str, Any]:
    message = {
        "role": role,
        "content": content,
        "loss_mask": loss_mask,
        "event_ids": [ev.event_id],
    }
    if tool_name:
        message["tool_name"] = tool_name
    return message


def _append_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    if not message["content"].strip():
        return
    if not messages:
        messages.append(message)
        return
    previous = messages[-1]
    if (
        previous["role"] == message["role"]
        and previous.get("tool_name") == message.get("tool_name")
        and previous["loss_mask"] == message["loss_mask"]
    ):
        previous["content"] = f"{previous['content']}\n\n{message['content']}".strip()
        previous["event_ids"].extend(message["event_ids"])
        return
    messages.append(message)


def _safe_text(ev: NormalizedEvent) -> str:
    return truncate_summary(event_text(ev), limit=20_000)


def _tool_call_text(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    data = {
        "name": _tool_name(ev),
        "arguments": payload.get("arguments") if "arguments" in payload else payload.get("input"),
        "summary": ev.summary,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _tool_result_text(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    content = payload.get("output")
    if content is None:
        content = payload.get("content")
    if content is None:
        content = payload
    if isinstance(content, str):
        return truncate_summary(content, limit=20_000)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _tool_name(ev: NormalizedEvent) -> str:
    payload = ev.payload or {}
    for key in ("name", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    summary = ev.summary.split("(", 1)[0].split(":", 1)[-1].strip()
    return summary or ev.event_type


def _is_failure_event(ev: NormalizedEvent) -> bool:
    payload = ev.payload or {}
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    if ev.event_type == "error":
        return True
    if ev.actor != "tool":
        return False
    text = _tool_result_text(ev)
    return any(pattern.search(text) for pattern in _FAILURE_PATTERNS)


def _split_examples(
    examples: list[dict[str, Any]], *, eval_fraction: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(examples) < 3 or eval_fraction <= 0:
        return examples, []
    eval_count = max(1, int(round(len(examples) * eval_fraction)))
    eval_count = min(eval_count, len(examples) - 1)
    ordered = sorted(examples, key=lambda item: item["id"])
    return ordered[:-eval_count], ordered[-eval_count:]


def _diverse_sample(examples: list[dict[str, Any]], max_sessions: int | None) -> list[dict[str, Any]]:
    if max_sessions is None or len(examples) <= max_sessions:
        return examples
    buckets: dict[Host, list[dict[str, Any]]] = {"claude-code": [], "codex": []}
    for item in examples:
        buckets[item["host"]].append(item)
    out: list[dict[str, Any]] = []
    while len(out) < max_sessions:
        progress = False
        for host in ("claude-code", "codex"):
            bucket = buckets[host]
            if not bucket or len(out) >= max_sessions:
                continue
            out.append(bucket.pop(0))
            progress = True
        if not progress:
            break
    return out


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _benchmark_command(runner: str, benchmark: str, model: str, output_path: Path) -> list[str]:
    if runner == "agentarena":
        return [
            "agentarena",
            "run",
            "--benchmark",
            benchmark,
            "--model",
            model,
            "--output",
            str(output_path),
            "--headless",
        ]
    return [
        "touchstone",
        "eval",
        "--benchmark",
        benchmark,
        "--model",
        model,
        "--output",
        str(output_path),
        "--headless",
    ]


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed", "resolved"}
    return False


def _modal_training_script(
    *,
    config_name: str,
    app_name: str,
    gpu: str,
    volume_name: str,
) -> str:
    return textwrap.dedent(
        f'''\
        from __future__ import annotations

        import json
        import shutil
        import subprocess
        from pathlib import Path

        import modal

        APP_NAME = {app_name!r}
        GPU = {gpu!r}
        VOLUME_NAME = {volume_name!r}

        app = modal.App(APP_NAME)
        volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
        image = (
            modal.Image.debian_slim(python_version="3.11")
            .pip_install(
                "unsloth",
                "datasets",
                "trl",
                "transformers",
                "accelerate",
                "peft",
                "safetensors",
            )
        )


        def _build_sample(messages, tokenizer, max_seq_length):
            input_ids = []
            labels = []
            for message in messages:
                role = message["role"]
                header = f"<|{{role}}|>\n"
                text = header + message["content"].strip() + tokenizer.eos_token
                tokens = tokenizer(text, add_special_tokens=False)["input_ids"]
                if len(input_ids) + len(tokens) > max_seq_length:
                    break
                input_ids.extend(tokens)
                if message.get("loss_mask"):
                    labels.extend(tokens)
                else:
                    labels.extend([-100] * len(tokens))
            attention_mask = [1] * len(input_ids)
            return {{
                "input_ids": input_ids,
                "labels": labels,
                "attention_mask": attention_mask,
            }}


        def _download_volume(volume_name: str, remote_dir: str, local_dir: str) -> None:
            modal_cli = shutil.which("modal")
            if modal_cli is None:
                raise RuntimeError("modal CLI is required to download weights locally")
            subprocess.run(
                [modal_cli, "volume", "get", volume_name, remote_dir, local_dir],
                check=True,
            )


        def _load_rows(dataset_path: Path):
            return [
                json.loads(line)
                for line in dataset_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]


        def _load_config(config_path: Path) -> dict:
            return json.loads(config_path.read_text(encoding="utf-8"))


        @app.function(
            gpu=GPU,
            timeout=60 * 60 * 24,
            image=image,
            volumes={{"/vol": volume}},
        )
        def train_remote(config: dict, rows: list[dict]):
            from datasets import Dataset
            from transformers import Trainer, TrainingArguments
            from unsloth import FastLanguageModel

            model, tokenizer = FastLanguageModel.from_pretrained(
                model_name=config["base_model"],
                max_seq_length=config["max_seq_length"],
                load_in_4bit=True,
            )
            model = FastLanguageModel.get_peft_model(model)
            samples = [
                _build_sample(row["messages"], tokenizer, config["max_seq_length"])
                for row in rows
            ]
            dataset = Dataset.from_list([sample for sample in samples if sample["input_ids"]])
            training_args = TrainingArguments(
                output_dir=config["remote_output_dir"],
                per_device_train_batch_size=config["per_device_batch_size"],
                gradient_accumulation_steps=config["gradient_accumulation_steps"],
                num_train_epochs=config["num_train_epochs"],
                learning_rate=config["learning_rate"],
                logging_steps=1,
                save_strategy="epoch",
                report_to=[],
            )
            trainer = Trainer(model=model, args=training_args, train_dataset=dataset)
            trainer.train()
            model.save_pretrained(config["remote_output_dir"])
            tokenizer.save_pretrained(config["remote_output_dir"])
            if config["output_format"] == "gguf" and hasattr(model, "save_pretrained_gguf"):
                model.save_pretrained_gguf(config["remote_output_dir"], tokenizer)
            volume.commit()
            return config["remote_output_dir"]


        @app.local_entrypoint()
        def main(config: str = "{config_name}"):
            config_path = Path(config).resolve()
            cfg = _load_config(config_path)
            rows = _load_rows(Path(cfg["dataset_path"]))
            remote_dir = train_remote.remote(cfg, rows)
            _download_volume(VOLUME_NAME, remote_dir, cfg["local_output_dir"])
        '''
    )
