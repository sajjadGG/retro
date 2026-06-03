"""Operator Quest Gamification System."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .storage import Layout


def get_rank_for_xp(xp: int) -> str:
    """Return rank name based on XP thresholds."""
    if xp < 300:
        return "Novice Prompt Mechanic"
    elif xp < 1000:
        return "Context Orchestrator"
    else:
        return "Systems Director"


def load_quest_state(layout: Layout) -> dict[str, Any]:
    """Load quest state from disk or return default state."""
    state_path = layout.root / "quests" / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.setdefault("generation_date", "")
            state.setdefault("user_level", "Novice Prompt Mechanic")
            state.setdefault("streak_count", 0)
            state.setdefault("experience_points", 0)
            state.setdefault("streak_freezes", 0)
            state.setdefault("last_completion_date", "")
            state.setdefault("daily_quests", [])
            state.setdefault("streak_updated_date", "")
            state.setdefault("completion_bonus_date", "")
            state["user_level"] = get_rank_for_xp(state["experience_points"])
            return state
        except json.JSONDecodeError:
            pass

    return {
        "generation_date": "",
        "user_level": "Novice Prompt Mechanic",
        "streak_count": 0,
        "experience_points": 0,
        "streak_freezes": 0,
        "last_completion_date": "",
        "daily_quests": [],
        "streak_updated_date": "",
        "completion_bonus_date": "",
    }


def save_quest_state(layout: Layout, state: dict[str, Any]) -> None:
    """Save quest state to disk."""
    state_path = layout.root / "quests" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ensure_daily_quests(layout: Layout, state: dict[str, Any], force_generate: bool = False) -> bool:
    """Ensure daily quests are generated for today. Returns True if a new generation occurred."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state["generation_date"] == today_str and not force_generate:
        return False

    # Check streak breakage before resetting/generating new quests
    if state["generation_date"]:
        try:
            today_date = date.fromisoformat(today_str)
            
            # Did they complete quests yesterday (or today)?
            completed_recently = False
            if state["last_completion_date"]:
                comp_date = date.fromisoformat(state["last_completion_date"])
                if comp_date >= today_date - timedelta(days=1):
                    completed_recently = True

            if not completed_recently:
                if state.get("streak_freezes", 0) > 0:
                    state["streak_freezes"] -= 1
                    # Protected streak! Mark last completion date as yesterday to preserve next check
                    state["last_completion_date"] = (today_date - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    state["streak_count"] = 0
        except ValueError:
            pass

    # Generate new quests
    quests = []
    try:
        quests = _generate_quests_via_llm(layout)
    except Exception:
        quests = _generate_quests_programmatically(layout)

    # Set activation times
    now_ts = datetime.now().isoformat()
    for q in quests:
        q["activated_at"] = now_ts
        q["status"] = "active"

    state["generation_date"] = today_str
    state["daily_quests"] = quests
    state["user_level"] = get_rank_for_xp(state["experience_points"])
    return True


def verify_quests(layout: Layout, state: dict[str, Any]) -> dict[str, Any]:
    """Run verification checks on active daily quests. Updates quest status, streak, XP."""
    now_date_str = datetime.now().strftime("%Y-%m-%d")
    results = []
    xp_gained = 0
    completed_count = 0
    now_completed_ids = []

    for q in state.get("daily_quests", []):
        if q["status"] == "completed":
            results.append((q, True, "Already completed"))
            completed_count += 1
            continue

        metric = q.get("verification_metric", {})
        m_type = metric.get("type")
        verified = False
        reason = ""
        activated_at_str = q.get("activated_at", "")

        if m_type == "file_exists":
            path = metric.get("path", "")
            if Path(path).exists():
                verified = True
                reason = f"File {path} exists"
            else:
                reason = f"File {path} does not exist"

        elif m_type == "file_modified":
            path = metric.get("path", "")
            if Path(path).exists():
                mtime = Path(path).stat().st_mtime
                if activated_at_str:
                    try:
                        act_time = datetime.fromisoformat(activated_at_str).timestamp()
                        if mtime > act_time:
                            verified = True
                            reason = f"File {path} was modified after activation"
                        else:
                            reason = f"File {path} exists but has not been modified since activation"
                    except ValueError:
                        verified = True
                        reason = f"File {path} exists"
                else:
                    verified = True
                    reason = f"File {path} exists"
            else:
                reason = f"File {path} does not exist"

        elif m_type == "cli_command_run":
            pattern = metric.get("command_pattern", "")
            found_cmd = False
            if activated_at_str:
                from .schema import read_events
                normalized_root = layout.root / "normalized"
                if normalized_root.exists():
                    for jsonl in normalized_root.glob("*/*.events.jsonl"):
                        events = list(read_events(jsonl))
                        if not events:
                            continue
                        first_ts = events[0].timestamp
                        if not first_ts:
                            continue

                        try:
                            from datetime import timezone
                            t_first = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                            if t_first.tzinfo is None:
                                t_first = t_first.replace(tzinfo=timezone.utc)
                            t_act = datetime.fromisoformat(activated_at_str.replace("Z", "+00:00"))
                            if t_act.tzinfo is None:
                                t_act = t_act.replace(tzinfo=timezone.utc)
                            if t_first < t_act:
                                continue
                        except ValueError:
                            pass

                        for e in events:
                            if e.event_type == "command" and e.actor == "assistant":
                                payload = e.payload or {}
                                cmd = (payload.get("command") or payload.get("cmd") or "").lower()
                                if pattern.lower() in cmd:
                                    found_cmd = True
                                    break
                        if found_cmd:
                            break
            if found_cmd:
                verified = True
                reason = f"Command containing '{pattern}' was detected in a recent session"
            else:
                reason = f"No recent command matching '{pattern}' was found in sessions"
        else:
            reason = "Unknown verification type"

        if verified:
            q["status"] = "completed"
            xp_gained += 100
            completed_count += 1
            now_completed_ids.append(q["id"])
            results.append((q, True, reason))
        else:
            results.append((q, False, reason))

    if xp_gained > 0:
        state["experience_points"] += xp_gained
        state["last_completion_date"] = now_date_str

        # Completion bonus (all 3 daily quests completed)
        if completed_count == 3 and state.get("completion_bonus_date") != now_date_str:
            state["experience_points"] += 50
            xp_gained += 50
            state["completion_bonus_date"] = now_date_str

        if state.get("streak_updated_date") != now_date_str:
            state["streak_count"] = state.get("streak_count", 0) + 1
            state["streak_updated_date"] = now_date_str

        state["user_level"] = get_rank_for_xp(state["experience_points"])

    return {
        "results": results,
        "xp_gained": xp_gained,
        "new_xp": state["experience_points"],
        "new_level": state["user_level"],
        "completed_count": completed_count,
        "now_completed_ids": now_completed_ids,
    }


def buy_streak_freeze(state: dict[str, Any]) -> str:
    """Purchase a streak freeze using XP."""
    cost = 200
    if state["experience_points"] < cost:
        return (
            f"Insufficient XP. A streak freeze costs {cost} XP, "
            f"but you only have {state['experience_points']} XP."
        )

    state["experience_points"] -= cost
    state["streak_freezes"] = state.get("streak_freezes", 0) + 1
    state["user_level"] = get_rank_for_xp(state["experience_points"])
    return (
        f"Successfully purchased a Streak Freeze! Consumed {cost} XP. "
        f"Total freezes: {state['streak_freezes']}."
    )


def _generate_quests_via_llm(layout: Layout) -> list[dict[str, Any]]:
    """Synthesize user telemetry and community trends to generate 3 daily quests using Codex."""
    sessions = []
    normalized_root = layout.root / "normalized"
    if normalized_root.exists():
        for jsonl in sorted(normalized_root.glob("*/*.events.jsonl"), reverse=True)[:5]:
            from .schema import read_events
            events = list(read_events(jsonl))
            turns = sum(1 for e in events if e.event_type == "message" and e.actor == "user")
            total_cmds = sum(1 for e in events if e.event_type == "command" and e.actor == "assistant")
            sessions.append(f"Session {jsonl.name}: {turns} user turns, {total_cmds} commands.")

    trajectory_context = "\n".join(sessions) if sessions else "No recent sessions."
    community_trends = (
        "- Initialize an AGENTS.md file in the repo root to document "
        "architecture and prevent redundant scans.\n"
        "- Run parallel agent operations using git worktrees to resolve tickets concurrently.\n"
        "- Curate testing_skill.md skill files to specify clean automated verification procedures.\n"
        "- Limit turns in a single session to 20 to avoid context bloat."
    )

    prompt = (
        "You are a Behavioral Scientist and Gamification HCI Specialist acting "
        "as the \"Quest Master\" for an AI Agent coding tool named retro.\n\n"
        "Your objective is to generate exactly THREE (3) simple, daily "
        "habit-forming quests for the software developer using the tool today.\n\n"
        "Input Data:\n"
        "- User Trajectory (Internal Context):\n"
        f"{trajectory_context}\n"
        "- Community Trends (External Context):\n"
        f"{community_trends}\n\n"
        "Design Principles:\n"
        "- Fogg Behavior Model: Ensure the quests have high \"Ability\" (they must "
        "be simple and take < 5 minutes to complete) to pair with the user's "
        "existing \"Motivation.\"\n"
        "- Cognitive Forcing: At least one quest must force the user to slow down "
        "and evaluate their system setup or AI interactions (e.g., reviewing a "
        "skill file, auditing a prompt).\n"
        "- Flow State: Quests must integrate seamlessly into the developer's "
        "existing terminal workflow. Do not suggest abstract activities; "
        "suggest concrete code, CLI, or configuration tasks.\n\n"
        "Output Requirements:\n"
        "Generate 3 tailored quests in the required JSON format."
    )

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "daily_quests": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "objective": {"type": "string"},
                        "rationale": {"type": "string"},
                        "verification_metric": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["file_exists", "file_modified", "cli_command_run"]
                                },
                                "path": {"type": "string"},
                                "command_pattern": {"type": "string"}
                            },
                            "required": ["type"]
                        },
                        "status": {"type": "string"}
                    },
                    "required": [
                        "id",
                        "name",
                        "objective",
                        "rationale",
                        "verification_metric",
                        "status"
                    ]
                }
            }
        },
        "required": ["daily_quests"]
    }

    from .llm import call_codex_headless
    result = call_codex_headless(prompt, schema=schema, timeout=60)
    if isinstance(result, dict) and "daily_quests" in result:
        return result["daily_quests"]
    raise RuntimeError("Invalid response structure from Codex")


def _generate_quests_programmatically(layout: Layout) -> list[dict[str, Any]]:
    """Programmatic fallback: generates 3 quests based on workspace state and conventions."""
    candidates = []

    # Quest 1: AGENTS.md
    if not Path("AGENTS.md").exists():
        candidates.append({
            "id": "q_agents_md_init",
            "name": "Establish Boundaries",
            "objective": "Create an AGENTS.md file in your repository root.",
            "rationale": (
                "An AGENTS.md file defines roles and boundaries, guiding the "
                "agent to entry points immediately and preventing redundant "
                "scans."
            ),
            "verification_metric": {
                "type": "file_exists",
                "path": "AGENTS.md"
            },
            "status": "active"
        })

    # Quest 2: .cursorrules
    if not Path(".cursorrules").exists():
        candidates.append({
            "id": "q_cursorrules_init",
            "name": "Write Rules",
            "objective": "Create a .cursorrules file in your repository root.",
            "rationale": (
                "Providing explicit guidelines prevents the agent from making "
                "generic assumptions and ensures style consistency."
            ),
            "verification_metric": {
                "type": "file_exists",
                "path": ".cursorrules"
            },
            "status": "active"
        })

    # Pre-defined general quests
    generals = [
        {
            "id": "q_git_worktree_parallel",
            "name": "The Parallel Path",
            "objective": "Add a new git worktree using git worktree add.",
            "rationale": (
                "Using git worktrees allows running multiple agents in parallel "
                "in clean, isolated workspaces without branch-switching conflicts."
            ),
            "verification_metric": {
                "type": "cli_command_run",
                "command_pattern": "git worktree add"
            },
            "status": "active"
        },
        {
            "id": "q_skill_audit",
            "name": "Refining Memory",
            "objective": "Create or update a testing_skill.md memory file in your repository.",
            "rationale": (
                "Documenting testing steps ensures that future agent sessions "
                "can verify their edits autonomously and avoid breaking changes."
            ),
            "verification_metric": {
                "type": "file_modified",
                "path": "testing_skill.md"
            },
            "status": "active"
        },
        {
            "id": "q_mem_retrieve",
            "name": "Consulting History",
            "objective": "Run the retro memory retrieve command in your CLI.",
            "rationale": (
                "Querying your SQLite memory index exposes what lessons were "
                "captured in previous sessions for your current task."
            ),
            "verification_metric": {
                "type": "cli_command_run",
                "command_pattern": "retro memory retrieve"
            },
            "status": "active"
        },
        {
            "id": "q_mem_weave",
            "name": "Weave Prompt Block",
            "objective": "Run the retro memory weave command in your CLI.",
            "rationale": (
                "Weaving compiles your retrieved memories into a compact "
                "markdown block ready to be pasted directly into a new agent "
                "session."
            ),
            "verification_metric": {
                "type": "cli_command_run",
                "command_pattern": "retro memory weave"
            },
            "status": "active"
        },
        {
            "id": "q_mem_doctor",
            "name": "Verify Index Health",
            "objective": "Run the retro memory doctor command in your CLI.",
            "rationale": (
                "Checking index health ensures that there are no dangling "
                "wiki-links or database corruptions in your local cache."
            ),
            "verification_metric": {
                "type": "cli_command_run",
                "command_pattern": "retro memory doctor"
            },
            "status": "active"
        },
        {
            "id": "q_dashboard_build",
            "name": "Visualize Progress",
            "objective": "Run the retro dashboard build command to regenerate your local HTML dashboard.",
            "rationale": (
                "Viewing the dashboard updates your metrics, streaks, cost "
                "charts, and provides visual confirmation of completed quests."
            ),
            "verification_metric": {
                "type": "cli_command_run",
                "command_pattern": "retro dashboard build"
            },
            "status": "active"
        }
    ]

    candidates.extend(generals)

    # Pick the first 3 unique candidates
    picked = []
    seen_ids = set()
    for c in candidates:
        if c["id"] not in seen_ids:
            picked.append(c)
            seen_ids.add(c["id"])
            if len(picked) == 3:
                break
    return picked
