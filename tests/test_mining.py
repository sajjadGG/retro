"""Tests for the mining subsystem."""
from __future__ import annotations

from retro.mining import (
    FILTER_REGISTRY,
    METHOD_REGISTRY,
    mine_with_method,
)
from retro.mining.base import MemoryCandidate, MiningResult
from retro.schema import NormalizedEvent, RawRef


def _ev(
    event_id="x",
    actor="user",
    event_type="message",
    summary="",
    payload=None,
    timestamp="2025-06-01T10:00:00Z",
):
    return NormalizedEvent(
        event_id=event_id,
        session_id="s",
        host="claude-code",
        sequence=1,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="p", line=1),
        timestamp=timestamp,
        payload=payload or {},
    )


class TestMethodRegistry:
    def test_registry_not_empty(self):
        assert len(METHOD_REGISTRY) > 0

    def test_expected_methods_registered(self):
        assert "reme_refine_poc" in METHOD_REGISTRY
        assert "skill_pro" in METHOD_REGISTRY
        assert "memp_procedural" in METHOD_REGISTRY

    def test_filter_registry(self):
        assert "risk_aware" in FILTER_REGISTRY


class TestMineWithMethod:
    def test_reme_refine_on_fixture(self, claude_imported):
        layout, session_id = claude_imported
        normalized = layout.normalized_path("claude-code", session_id)
        result = mine_with_method(normalized, method="reme_refine_poc")
        assert result.method == "reme_refine_poc"
        assert result.session_id == session_id
        assert len(result.candidates) >= 1
        for c in result.candidates:
            assert c.id
            assert c.method == "reme_refine_poc"
            assert c.text
            assert c.kind

    def test_skill_pro_on_fixture(self, claude_imported):
        layout, session_id = claude_imported
        normalized = layout.normalized_path("claude-code", session_id)
        result = mine_with_method(normalized, method="skill_pro")
        assert result.method == "skill_pro"
        assert len(result.candidates) >= 1

    def test_memp_procedural_on_fixture(self, claude_imported):
        layout, session_id = claude_imported
        normalized = layout.normalized_path("claude-code", session_id)
        result = mine_with_method(normalized, method="memp_procedural")
        assert result.method == "memp_procedural"
        assert len(result.candidates) >= 1
        first = result.candidates[0]
        assert first.kind == "procedure"

    def test_risk_aware_filter(self, claude_imported):
        layout, session_id = claude_imported
        normalized = layout.normalized_path("claude-code", session_id)
        result = mine_with_method(
            normalized, method="reme_refine_poc", filters=["risk_aware"]
        )
        assert "risk_aware" in result.filters_applied
        for c in result.candidates:
            assert c.risk != "high"
            assert c.confidence >= 0.40


class TestRiskAwareFilter:
    def test_drops_high_risk(self):
        from retro.mining.filters.risk_aware import filter_risk_aware

        result = MiningResult(
            session_id="s",
            host="claude-code",
            method="test",
            task_summary="test",
            candidates=[
                MemoryCandidate(
                    id="1", method="test", kind="procedure",
                    text="safe", when_to_use="always",
                    risk="low", confidence=0.8,
                ),
                MemoryCandidate(
                    id="2", method="test", kind="procedure",
                    text="risky", when_to_use="never",
                    risk="high", confidence=0.9,
                ),
            ],
        )
        filtered = filter_risk_aware(result)
        assert len(filtered.candidates) == 1
        assert filtered.candidates[0].id == "1"

    def test_drops_low_confidence(self):
        from retro.mining.filters.risk_aware import filter_risk_aware

        result = MiningResult(
            session_id="s",
            host="claude-code",
            method="test",
            task_summary="test",
            candidates=[
                MemoryCandidate(
                    id="1", method="test", kind="procedure",
                    text="confident", when_to_use="always",
                    confidence=0.8,
                ),
                MemoryCandidate(
                    id="2", method="test", kind="procedure",
                    text="uncertain", when_to_use="maybe",
                    confidence=0.1,
                ),
            ],
        )
        filtered = filter_risk_aware(result)
        assert len(filtered.candidates) == 1
        assert filtered.candidates[0].id == "1"
