"""Shared fixtures for the dashboard test modules."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from retro.dashboard.build import load_inputs
from retro.storage import Layout

FIXTURE_ARCHIVE = Path(__file__).parent / "fixtures" / "dashboard-archive"


@pytest.fixture
def archive(tmp_path: Path) -> Layout:
    root = tmp_path / "rollout-memory"
    shutil.copytree(FIXTURE_ARCHIVE, root)
    return Layout(root)


@pytest.fixture
def empty_archive(tmp_path: Path) -> Layout:
    root = tmp_path / "empty-archive"
    root.mkdir()
    return Layout(root)


@pytest.fixture
def inputs(archive: Layout):
    return load_inputs(archive)


def read_shard(path: Path) -> tuple[str, dict]:
    """Decode a ``RETRO_DATA("name", {...});`` shard file."""
    text = path.read_text(encoding="utf-8").strip()
    assert text.startswith("RETRO_DATA(")
    assert text.endswith(");")
    body = text[len("RETRO_DATA(") : -2]
    name, _, payload = body.partition(",")
    return json.loads(name), json.loads(payload)
