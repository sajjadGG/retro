"""Metrics for repository file-localization benchmarks."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FileLocalizationMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    exact_match: bool

    def to_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)


def file_localization_metrics(
    expected_files: Iterable[str],
    predicted_files: Iterable[str],
) -> FileLocalizationMetrics:
    expected = set(expected_files)
    predicted = set(predicted_files)
    if not expected:
        raise ValueError("file-localization ground truth must contain at least one file")

    true_positives = len(expected & predicted)
    false_positives = len(predicted - expected)
    false_negatives = len(expected - predicted)
    precision = _divide(true_positives, true_positives + false_positives)
    recall = _divide(true_positives, true_positives + false_negatives)
    f1 = _divide(2 * precision * recall, precision + recall)
    return FileLocalizationMetrics(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
        exact_match=expected == predicted,
    )


def aggregate_file_localization(
    metrics: Iterable[FileLocalizationMetrics],
) -> dict[str, int | float]:
    rows = list(metrics)
    if not rows:
        raise ValueError("cannot aggregate an empty task set")

    count = len(rows)
    true_positives = sum(row.true_positives for row in rows)
    false_positives = sum(row.false_positives for row in rows)
    false_negatives = sum(row.false_negatives for row in rows)
    micro_precision = _divide(true_positives, true_positives + false_positives)
    micro_recall = _divide(true_positives, true_positives + false_negatives)
    return {
        "task_count": count,
        "score": _rounded(sum(row.f1 for row in rows) / count),
        "macro_precision": _rounded(sum(row.precision for row in rows) / count),
        "macro_recall": _rounded(sum(row.recall for row in rows) / count),
        "macro_f1": _rounded(sum(row.f1 for row in rows) / count),
        "micro_precision": _rounded(micro_precision),
        "micro_recall": _rounded(micro_recall),
        "micro_f1": _rounded(_divide(2 * micro_precision * micro_recall, micro_precision + micro_recall)),
        "exact_match_rate": _rounded(sum(row.exact_match for row in rows) / count),
        "zero_precision_rate": _rounded(sum(row.precision == 0.0 for row in rows) / count),
        "perfect_recall_rate": _rounded(sum(row.recall == 1.0 for row in rows) / count),
        "zero_recall_rate": _rounded(sum(row.recall == 0.0 for row in rows) / count),
    }


def _divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _rounded(value: float) -> float:
    return round(value, 6)
