from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_retro_user_data(monkeypatch, tmp_path: Path):
    data_dir = tmp_path / "retro-user-data"
    monkeypatch.setenv("RETRO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("RETRO_CONFIG_PATH", str(data_dir / "config.json"))
    monkeypatch.setenv("RETRO_LOG_DIR", str(data_dir / "logs"))
