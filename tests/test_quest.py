"""Tests for the Operator Quest Gamification and State system."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from retro.quest import (
    buy_streak_freeze,
    ensure_daily_quests,
    get_rank_for_xp,
    load_quest_state,
    save_quest_state,
    verify_quests,
)
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout


@pytest.fixture
def temp_layout(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    return lay


def test_get_rank_for_xp():
    assert get_rank_for_xp(100) == "Novice Prompt Mechanic"
    assert get_rank_for_xp(300) == "Context Orchestrator"
    assert get_rank_for_xp(999) == "Context Orchestrator"
    assert get_rank_for_xp(1000) == "Systems Director"
    assert get_rank_for_xp(5000) == "Systems Director"


def test_load_and_save_quest_state(temp_layout):
    state = load_quest_state(temp_layout)
    assert state["experience_points"] == 0
    assert state["streak_count"] == 0
    
    state["experience_points"] = 350
    state["streak_count"] = 5
    save_quest_state(temp_layout, state)
    
    loaded = load_quest_state(temp_layout)
    assert loaded["experience_points"] == 350
    assert loaded["streak_count"] == 5
    assert loaded["user_level"] == "Context Orchestrator"


def test_buy_streak_freeze():
    state = {
        "experience_points": 100,
        "streak_freezes": 0,
        "user_level": "Novice Prompt Mechanic",
    }
    # Fail due to insufficient XP
    res = buy_streak_freeze(state)
    assert "Insufficient XP" in res
    assert state["streak_freezes"] == 0
    
    # Success
    state["experience_points"] = 250
    res = buy_streak_freeze(state)
    assert "Successfully purchased" in res
    assert state["streak_freezes"] == 1
    assert state["experience_points"] == 50


def test_ensure_daily_quests_programmatic(temp_layout):
    state = load_quest_state(temp_layout)
    
    # Force programmatic generation by passing mock layout (it will trigger fallback when LLM fails)
    gen = ensure_daily_quests(temp_layout, state)
    assert gen is True
    assert len(state["daily_quests"]) == 3
    assert state["generation_date"] == datetime.now().strftime("%Y-%m-%d")
    
    # Second call should not regenerate
    gen2 = ensure_daily_quests(temp_layout, state)
    assert gen2 is False


def test_quest_verification_file_exists(temp_layout):
    state = load_quest_state(temp_layout)
    ensure_daily_quests(temp_layout, state)
    
    # Create a custom file exists quest
    state["daily_quests"] = [{
        "id": "q_test_exists",
        "name": "Test File Exists",
        "objective": "Objective",
        "rationale": "Rationale",
        "verification_metric": {
            "type": "file_exists",
            "path": str(temp_layout.root / "exists.txt")
        },
        "status": "active",
        "activated_at": datetime.now().isoformat()
    }]
    
    # Not verified
    res1 = verify_quests(temp_layout, state)
    assert res1["completed_count"] == 0
    assert state["daily_quests"][0]["status"] == "active"
    
    # Write file
    (temp_layout.root / "exists.txt").write_text("hello", encoding="utf-8")
    
    # Verified
    res2 = verify_quests(temp_layout, state)
    assert res2["completed_count"] == 1
    assert res2["xp_gained"] == 100
    assert state["daily_quests"][0]["status"] == "completed"
    assert state["streak_count"] == 1


def test_quest_verification_file_modified(temp_layout):
    import os
    state = load_quest_state(temp_layout)
    ensure_daily_quests(temp_layout, state)
    
    test_file = temp_layout.root / "modified.txt"
    test_file.write_text("initial", encoding="utf-8")
    past_time = datetime.now().timestamp() - 600
    os.utime(test_file, (past_time, past_time))
    
    # Setup quest activated in future / now
    activated_at = (datetime.now() - timedelta(minutes=5)).isoformat()
    state["daily_quests"] = [{
        "id": "q_test_modified",
        "name": "Test File Modified",
        "objective": "Objective",
        "rationale": "Rationale",
        "verification_metric": {
            "type": "file_modified",
            "path": str(test_file)
        },
        "status": "active",
        "activated_at": activated_at
    }]
    
    # Not verified since modification was before activation
    res1 = verify_quests(temp_layout, state)
    assert res1["completed_count"] == 0
    
    # Modify file now (mtime will be greater than activation time)
    test_file.write_text("updated", encoding="utf-8")
    
    # Verified
    res2 = verify_quests(temp_layout, state)
    assert res2["completed_count"] == 1
    assert state["daily_quests"][0]["status"] == "completed"


def test_quest_verification_cli_command_run(temp_layout):
    state = load_quest_state(temp_layout)
    ensure_daily_quests(temp_layout, state)
    
    activated_at = (datetime.now() - timedelta(minutes=1)).isoformat()
    state["daily_quests"] = [{
        "id": "q_test_cmd",
        "name": "Test CLI Command",
        "objective": "Objective",
        "rationale": "Rationale",
        "verification_metric": {
            "type": "cli_command_run",
            "command_pattern": "git worktree add"
        },
        "status": "active",
        "activated_at": activated_at
    }]
    
    # 1. No sessions - not verified
    res1 = verify_quests(temp_layout, state)
    assert res1["completed_count"] == 0
    
    # 2. Add a session that ran another command - not verified
    events = [
        NormalizedEvent(
            event_id="e1",
            session_id="s1",
            host="codex",
            sequence=1,
            actor="assistant",
            event_type="command",
            summary="run status",
            raw_ref=RawRef("p", 1),
            timestamp=(datetime.now() + timedelta(seconds=1)).isoformat(),
            payload={"command": "git status"}
        )
    ]
    norm_path = temp_layout.normalized_path("codex", "s1")
    norm_path.parent.mkdir(parents=True, exist_ok=True)
    write_events(norm_path, events)
    
    res2 = verify_quests(temp_layout, state)
    assert res2["completed_count"] == 0
    
    # 3. Add a command that matches - verified!
    events.append(
        NormalizedEvent(
            event_id="e2",
            session_id="s1",
            host="codex",
            sequence=2,
            actor="assistant",
            event_type="command",
            summary="add worktree",
            raw_ref=RawRef("p", 2),
            timestamp=(datetime.now() + timedelta(seconds=2)).isoformat(),
            payload={"command": "git worktree add ./new-dir"}
        )
    )
    write_events(norm_path, events)
    
    res3 = verify_quests(temp_layout, state)
    assert res3["completed_count"] == 1
    assert state["daily_quests"][0]["status"] == "completed"


def test_streak_freeze_protection(temp_layout):
    state = load_quest_state(temp_layout)
    
    # Simulate generated quests on a past date
    state["generation_date"] = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    state["streak_count"] = 5
    state["streak_freezes"] = 1
    state["last_completion_date"] = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    
    # Daily quests reset triggers
    ensure_daily_quests(temp_layout, state)
    
    # Streak count preserved because freeze was consumed!
    assert state["streak_count"] == 5
    assert state["streak_freezes"] == 0
    
    # Now simulate another day passing without completion or freezes
    state["generation_date"] = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    state["last_completion_date"] = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    
    ensure_daily_quests(temp_layout, state)
    
    # Streak count resets because no freezes are left!
    assert state["streak_count"] == 0
