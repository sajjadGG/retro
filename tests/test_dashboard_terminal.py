"""Tests for the terminal dashboard bootstrap path."""

from __future__ import annotations

import json
from pathlib import Path

from retro.dashboard_terminal import load_dashboard_data


def test_load_dashboard_data_builds_with_packaged_module(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls: dict[str, object] = {}

    def fake_build(mode: str, artifact_root: Path | None = None, out_dir: Path | None = None) -> Path:
        calls["mode"] = mode
        calls["artifact_root"] = artifact_root
        calls["out_dir"] = out_dir
        out_dir = out_dir or tmp_path / "dashboard"
        data_dir = out_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "rollouts.json").write_text(json.dumps({"generated_at": "now"}), encoding="utf-8")
        return out_dir / "index.html"

    monkeypatch.setattr("retro.dashboard_terminal.build_dashboard_data", fake_build)

    data = load_dashboard_data(mode="auto")

    assert data["generated_at"] == "now"
    assert calls["mode"] == "auto"
    assert calls["artifact_root"] is None
    assert calls["out_dir"] == tmp_path / "dashboard"
