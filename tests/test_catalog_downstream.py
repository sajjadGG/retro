from __future__ import annotations

import json
from pathlib import Path

from retro.benchmarks.task_scorer.selection import select_source
from retro.catalog import SessionRecord
from retro.cli import _mine_targets
from retro.memory_store import reindex
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout


def _record(key: str, *, excluded: bool) -> SessionRecord:
    host, session_id = key.split("/", 1)
    return SessionRecord(
        schema=1,
        key=key,
        host=host,
        session_id=session_id,
        agent=host,
        client="unknown",
        interactive=None,
        is_subagent=False,
        parent_key=None,
        title=None,
        first_prompt="prompt",
        started_at="2026-10-01T00:00:00Z",
        ended_at="2026-10-01T00:00:01Z",
        duration_s=1,
        active_s=1,
        cwd=None,
        cwd_resolved=None,
        git={
            "remote": None,
            "remote_url": None,
            "branch": None,
            "commit": None,
            "root": None,
            "worktree": None,
        },
        project={
            "id": "unknown",
            "name": "Unknown",
            "source": "unknown",
            "evidence": None,
        },
        models=[],
        counts={
            "events": 1,
            "user_messages": 1,
            "assistant_messages": 0,
            "tool_calls": 0,
            "unknown_events": 0,
        },
        markers=[],
        tags=["ignore"] if excluded else [],
        tag_sources={"ignore": ["manual"]} if excluded else {},
        excluded=excluded,
        fingerprint="fingerprint",
    )


def _write_catalog(layout: Layout, records: list[SessionRecord]) -> None:
    path = layout.root / "catalog" / "sessions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps(record.to_dict()) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_event(layout: Layout, session_id: str) -> None:
    write_events(
        layout.normalized_path("codex", session_id),
        [
            NormalizedEvent(
                event_id=f"{session_id}:1",
                session_id=session_id,
                host="codex",
                sequence=1,
                actor="user",
                event_type="message",
                summary="prompt",
                raw_ref=RawRef(path="source", line=1),
                timestamp="2026-10-01T00:00:00Z",
                payload={"text": "prompt"},
            )
        ],
    )


def test_mining_targets_skip_catalog_exclusions_unless_overridden(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _write_event(layout, "included")
    _write_event(layout, "excluded")
    _write_catalog(
        layout,
        [
            _record("codex/included", excluded=False),
            _record("codex/excluded", excluded=True),
        ],
    )

    assert _mine_targets(
        layout,
        "codex",
        "*",
        all_sessions=True,
    ) == [("codex", "included")]
    assert _mine_targets(
        layout,
        "codex",
        "*",
        all_sessions=True,
        include_excluded=True,
    ) == [("codex", "excluded"), ("codex", "included")]


def test_memory_reindex_skips_mined_candidates_from_excluded_sessions(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _write_catalog(
        layout,
        [
            _record("codex/included", excluded=False),
            _record("codex/excluded", excluded=True),
        ],
    )
    mined = layout.root / "mined" / "method" / "codex"
    mined.mkdir(parents=True)
    for session_id in ("included", "excluded"):
        (mined / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "host": "codex",
                    "session_id": session_id,
                    "method": "method",
                    "candidates": [
                        {
                            "id": session_id,
                            "kind": "case",
                            "scope": "global",
                            "text": f"memory {session_id}",
                            "when_to_use": "always",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    report = reindex(layout)

    assert report.mined_records == 1
    assert report.indexed == 1


def test_task_scorer_rejects_excluded_catalog_source_first(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _write_catalog(layout, [_record("codex/excluded", excluded=True)])

    candidate, rejection = select_source(
        layout=layout,
        host="codex",
        session_id="excluded",
        require_environment=False,
    )

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "EXCLUDED_BY_CATALOG"
    assert rejection.detail == "excluded by catalog"
    assert rejection.evidence["tags"] == ["ignore"]
