"""Tests for the v2 signals layer: profile builder, catalogue, store, summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.schema import NormalizedEvent, RawRef, write_events
from retro.signals import (
    REGISTRY,
    NormalizedSource,
    build_profile,
    classify_outcome,
    classify_work_kind,
    definitions_payload,
    definitions_version,
    evaluate,
    load_signal_store,
    summarize,
    update_signals,
)
from retro.signals.commands import classify_command, get_base_command, is_git_commit
from retro.signals.patterns import is_correction
from retro.signals.pricing import PricingMap, normalize_model_id
from retro.signals.usage import extract_usage
from retro.storage import Layout

HEADLINE_SIGNALS = (
    "usage.cost_usd",
    "usage.tokens_total",
    "usage.tokens_input",
    "usage.tokens_output",
    "usage.tokens_cache_read",
    "usage.duration_s",
    "usage.active_s",
    "activity.user_turns",
    "activity.tool_calls",
    "activity.file_edits",
    "activity.files_touched",
    "activity.commands",
    "outcome.status",
    "outcome.validated",
    "outcome.committed",
    "friction.user_corrections",
    "friction.tool_error_rate",
    "friction.interruptions",
    "risk.secret_exposure",
    "work.kind",
)


def event(seq, actor, event_type, *, payload=None, summary="", timestamp=None, parent=None, host="codex"):
    return NormalizedEvent(
        event_id=f"e{seq}",
        session_id="s1",
        host=host,
        sequence=seq,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="raw.jsonl", line=seq),
        timestamp=timestamp,
        parent_event_id=parent,
        payload=payload or {},
    )


def profile_for(events, *, host="codex", raw_dir=None, record=None):
    return build_profile(
        key=f"{host}/s1",
        host=host,
        session_id="s1",
        events=events,
        raw_dir=raw_dir or Path("/nonexistent"),
        record=record,
    )


class TestRegistry:
    def test_every_headline_signal_is_registered(self):
        for signal_id in HEADLINE_SIGNALS:
            assert signal_id in REGISTRY, signal_id

    def test_definitions_carry_full_metadata(self):
        for item in definitions_payload():
            assert item["id"] and item["title"] and item["description"]
            assert item["category"] and item["kind"] and item["polarity"] and item["method"]
            assert isinstance(item["version"], int)
            if item["kind"] == "category":
                assert item["categories"], item["id"]

    def test_version_is_a_digest_of_the_registered_set(self):
        assert definitions_version() == definitions_version()
        assert len(definitions_version()) == 12

    def test_version_changes_when_the_pricing_snapshot_changes(self, tmp_path: Path, monkeypatch):
        from retro.signals import pricing

        before = definitions_version()
        snapshot = tmp_path / "litellm-pricing.json"
        snapshot.write_text(
            json.dumps({"demo": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "PRICING_SNAPSHOT", snapshot)
        after = definitions_version()
        assert after != before

        snapshot.write_text(
            json.dumps({"demo": {"input_cost_per_token": 9e-06, "output_cost_per_token": 2e-06}}),
            encoding="utf-8",
        )
        assert definitions_version() != after

    def test_missing_pricing_snapshot_still_yields_a_version(self, tmp_path: Path, monkeypatch):
        from retro.signals import pricing

        monkeypatch.setattr(pricing, "PRICING_SNAPSHOT", tmp_path / "absent.json")
        assert pricing.snapshot_digest() == "none"
        assert len(definitions_version()) == 12

    def test_broken_signal_does_not_lose_the_session(self):
        definition = REGISTRY["activity.user_turns"]
        broken = type(definition)(
            **{**definition.__dict__, "fn": lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))}
        )
        REGISTRY["activity.user_turns"] = broken
        try:
            values, evidence, confidence = evaluate(profile_for([event(1, "user", "message")]))
            assert values["activity.user_turns"] is None
            assert confidence["activity.user_turns"] == 0.0
            assert evidence["activity.user_turns"] == ["error:RuntimeError"]
        finally:
            REGISTRY["activity.user_turns"] = definition


class TestProfileCounts:
    def test_human_turns_exclude_injected_messages(self):
        events = [
            event(1, "user", "message", payload={"text": "add a retry to the client"}),
            event(2, "user", "message", payload={"text": "<command-name>/model</command-name>"}),
            event(3, "user", "message", payload={"text": "<local-command-stdout>ok</local-command-stdout>"}),
            event(4, "user", "message", payload={"isMeta": True, "text": "resumed"}),
            event(5, "user", "message", payload={"content": [{"type": "tool_result", "content": "x"}]}),
            event(6, "user", "message", payload={"text": "no, that's wrong, revert it"}),
        ]
        profile = profile_for(events)
        assert profile.user_turns == 2
        assert profile.injected_user_messages == 4
        assert profile.first_prompt == "add a retry to the client"
        assert len(profile.correction_events) == 1

    def test_lifecycle_usage_and_metadata_events_are_not_conversation(self):
        events = [
            event(1, "assistant", "lifecycle", payload={"lifecycle": "turn_start"}),
            event(2, "system", "metadata", payload={"title": "x"}),
            event(3, "assistant", "usage", payload={"type": "token_count", "info": {}}),
            event(4, "tool", "attachment", payload={"hook": "pre"}),
        ]
        profile = profile_for(events)
        assert profile.tool_calls == 0
        assert profile.tool_results == 0
        assert profile.user_turns == 0
        assert profile.assistant_messages == 0

    def test_tool_calls_results_and_errors(self):
        events = [
            event(1, "assistant", "command", payload={"call_id": "c1", "arguments": {"cmd": "pytest -q"}}),
            event(
                2,
                "tool",
                "tool_result",
                payload={"call_id": "c1", "output": "ok\nProcess exited with code 0"},
            ),
            event(
                3, "assistant", "command", payload={"call_id": "c2", "arguments": {"cmd": "ruff check src"}}
            ),
            event(
                4, "tool", "tool_result", payload={"call_id": "c2", "output": "Process exited with code 1"}
            ),
            event(5, "assistant", "file_edit", payload={"call_id": "c3", "file_path": "/w/a.py"}),
            event(6, "tool", "tool_result", payload={"call_id": "c3", "output": "done"}),
        ]
        profile = profile_for(events)
        assert profile.tool_calls == 3
        assert profile.tool_results == 3
        assert profile.tool_errors == 1
        assert profile.unlinked_results == 0
        assert profile.tool_error_rate == pytest.approx(1 / 3, abs=1e-4)
        assert [(run.category, run.ok) for run in profile.validation_runs] == [
            ("test", True),
            ("lint", False),
        ]
        assert profile.validated is False

    def test_claude_style_results_link_by_tool_use_id(self):
        events = [
            event(
                1,
                "assistant",
                "command",
                payload={"tool_id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                host="claude-code",
            ),
            event(
                2,
                "tool",
                "command",
                payload={"tool_use_id": "toolu_1", "is_error": False, "content": "a b"},
                host="claude-code",
            ),
            event(
                3,
                "assistant",
                "file_edit",
                payload={"tool_id": "toolu_2", "name": "Edit", "input": {"new_string": "x\ny"}},
                host="claude-code",
            ),
            event(
                4,
                "tool",
                "file_edit",
                payload={"tool_use_id": "toolu_2", "is_error": True, "content": "Exit code 2"},
                host="claude-code",
            ),
        ]
        profile = profile_for(events, host="claude-code")
        assert profile.unlinked_results == 0
        assert profile.tool_errors == 1
        assert profile.file_edits == 1
        assert profile.edited_lines == 2

    def test_copilot_nested_exit_code_counts_as_failure(self):
        events = [
            event(
                1,
                "assistant",
                "command",
                payload={"call_id": "t1", "input": {"command": "cargo check"}},
                host="vscode-copilot",
            ),
            event(
                2,
                "tool",
                "command",
                parent="e1",
                payload={
                    "call_id": "t1",
                    "success": True,
                    "data": {"toolTelemetry": {"metrics": {"exit_code": 1}}},
                },
                host="vscode-copilot",
            ),
        ]
        profile = profile_for(events, host="vscode-copilot")
        assert profile.tool_errors == 1
        assert profile.validated is False

    def test_unlinked_results_are_counted(self):
        profile = profile_for([event(1, "tool", "tool_result", payload={"output": "orphan"})])
        assert profile.tool_results == 1
        assert profile.unlinked_results == 1

    def test_commits_and_noop_commits(self):
        events = [
            event(
                1,
                "assistant",
                "command",
                payload={"call_id": "c1", "arguments": {"cmd": "git commit -m 'x'"}},
            ),
            event(2, "tool", "tool_result", payload={"call_id": "c1", "output": "1 file changed"}),
            event(
                3,
                "assistant",
                "command",
                payload={"call_id": "c2", "arguments": {"cmd": "git commit -m 'y'"}},
            ),
            event(
                4,
                "tool",
                "tool_result",
                payload={"call_id": "c2", "output": "nothing to commit, working tree clean"},
            ),
        ]
        profile = profile_for(events)
        assert [ok for _, ok in profile.commit_events] == [True, False]
        assert profile.committed is True

    def test_interruptions_per_host(self):
        codex = profile_for(
            [event(1, "system", "unknown", payload={"type": "turn_aborted", "reason": "interrupted"})]
        )
        assert len(codex.interruption_events) == 1
        copilot = profile_for(
            [event(1, "system", "error", payload={"type": "abort", "data": {"reason": "user_initiated"}})],
            host="vscode-copilot",
        )
        assert len(copilot.interruption_events) == 1
        claude = profile_for(
            [event(1, "assistant", "message", payload={"text": "[Request interrupted by user]"})],
            host="claude-code",
        )
        assert len(claude.interruption_events) == 1

    def test_timestamps_duration_and_active_time(self):
        events = [
            event(1, "user", "message", payload={"text": "go"}, timestamp="2026-01-01T00:00:00Z"),
            event(2, "assistant", "message", payload={"text": "ok"}, timestamp="2026-01-01T00:00:30Z"),
            event(
                3, "assistant", "message", payload={"text": "still here"}, timestamp="2026-01-01T02:00:00Z"
            ),
            event(4, "assistant", "message", payload={"text": "done"}, timestamp=None),
        ]
        profile = profile_for(events)
        assert profile.duration_s == pytest.approx(7200.0)
        assert profile.active_s == pytest.approx(30.0)
        assert profile.untimed_events == 1

    def test_active_time_survives_out_of_order_timestamps(self):
        events = [
            event(1, "user", "message", payload={"text": "a"}, timestamp="2026-01-01T00:01:00Z"),
            event(2, "system", "attachment", payload={"x": 1}, timestamp="2026-01-01T00:00:00Z"),
            event(3, "assistant", "message", payload={"text": "b"}, timestamp="2026-01-01T00:01:30Z"),
        ]
        profile = profile_for(events)
        assert profile.duration_s == pytest.approx(90.0)
        assert profile.active_s == pytest.approx(90.0)

    def test_secret_detection_ignores_placeholders(self):
        leaked = profile_for(
            [event(1, "tool", "tool_result", payload={"output": "export GH=ghp_" + "a" * 36})]
        )
        assert leaked.secret_kinds == {"github_token"}
        clean = profile_for(
            [
                event(1, "tool", "tool_result", payload={"output": 'api_key = "your-api-key-here-xxxxxxxx"'}),
                event(
                    2,
                    "tool",
                    "tool_result",
                    payload={"output": 'token = os.environ["LONG_SECRET_TOKEN_NAME"]'},
                ),
            ]
        )
        assert clean.secret_kinds == set()

    def test_destructive_commands(self):
        events = [
            event(1, "assistant", "command", payload={"arguments": {"cmd": "rm -rf build/"}}),
            event(2, "assistant", "command", payload={"arguments": {"cmd": "git push --force origin main"}}),
            event(3, "assistant", "command", payload={"arguments": {"cmd": "ls -la"}}),
        ]
        profile = profile_for(events)
        assert {kind for kind, _ in profile.destructive_events} == {"recursive_delete", "force_push"}

    def test_edits_outside_workspace(self):
        class Record:
            key = "codex/s1"
            host = "codex"
            session_id = "s1"
            fingerprint = "fp"
            cwd = "/w"

        events = [
            event(1, "assistant", "file_edit", payload={"file_path": "/w/app/a.py"}),
            event(2, "assistant", "file_edit", payload={"file_path": "/etc/hosts"}),
        ]
        profile = profile_for(events, record=Record())
        assert profile.edits_outside_workspace == 1

    def test_edit_churn_and_reply_latency(self):
        events = [
            event(1, "user", "message", payload={"text": "do it"}, timestamp="2026-01-01T00:00:00Z"),
            event(
                2,
                "assistant",
                "file_edit",
                payload={"file_path": "/w/a.py"},
                timestamp="2026-01-01T00:00:10Z",
            ),
            event(
                3,
                "assistant",
                "file_edit",
                payload={"file_path": "/w/a.py"},
                timestamp="2026-01-01T00:00:20Z",
            ),
            event(4, "assistant", "message", payload={"text": "done"}, timestamp="2026-01-01T00:00:30Z"),
            event(5, "user", "message", payload={"text": "thanks"}, timestamp="2026-01-01T00:01:30Z"),
        ]
        profile = profile_for(events)
        assert profile.file_edits == 2
        assert len(profile.files_edited) == 1
        assert profile.edit_churn == pytest.approx(0.5)
        assert profile.reply_gaps == [pytest.approx(60.0)]
        assert profile.time_to_first_edit_s == pytest.approx(10.0)


class TestNormalizationV3Payloads:
    """v3 keeps host-native containers instead of duplicated alias keys."""

    def test_claude_v3_tool_call_and_result_through_part(self):
        events = [
            event(
                1,
                "assistant",
                "command",
                payload={
                    "call_id": "toolu_1",
                    "name": "Bash",
                    "part": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "pytest -q", "description": "run tests"},
                    },
                    "message": {"role": "assistant", "id": "msg_1"},
                },
                host="claude-code",
            ),
            event(
                2,
                "tool",
                "tool_result",
                payload={
                    "call_id": "toolu_1",
                    "name": "Bash",
                    "part": {
                        "tool_use_id": "toolu_1",
                        "type": "tool_result",
                        "content": "1 failed, 2 passed\nExit code 1",
                        "is_error": True,
                    },
                    "message": {"role": "user"},
                },
                host="claude-code",
            ),
        ]
        profile = profile_for(events, host="claude-code")
        assert profile.command_calls == 1
        assert profile.base_commands["pytest"] == 1
        assert profile.unlinked_results == 0
        assert profile.tool_errors == 1
        assert [(run.category, run.ok) for run in profile.validation_runs] == [("test", False)]

    def test_claude_v3_edit_through_part_counts_paths_and_lines(self):
        events = [
            event(
                1,
                "assistant",
                "file_edit",
                payload={
                    "call_id": "toolu_2",
                    "name": "Write",
                    "part": {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Write",
                        "input": {"file_path": "/w/a.py", "content": "line1\nline2\nline3"},
                    },
                },
                host="claude-code",
            )
        ]
        profile = profile_for(events, host="claude-code")
        assert profile.file_edits == 1
        assert profile.files_edited == {"/w/a.py"}
        assert profile.edited_lines == 3

    def test_copilot_v3_command_through_request(self):
        events = [
            event(
                1,
                "subagent",
                "command",
                payload={
                    "call_id": "call_1",
                    "name": "bash",
                    "model": "claude-sonnet-5",
                    "request": {
                        "toolCallId": "call_1",
                        "name": "bash",
                        "arguments": {"command": "git commit -m x", "description": "commit"},
                    },
                },
                host="vscode-copilot",
            ),
            event(
                2,
                "tool",
                "tool_result",
                payload={"call_id": "call_1", "name": "bash", "success": True, "result": "1 file changed"},
                host="vscode-copilot",
            ),
        ]
        profile = profile_for(events, host="vscode-copilot")
        assert profile.command_calls == 1
        assert profile.committed is True
        assert profile.unlinked_results == 0

    def test_codex_v3_json_string_arguments(self):
        events = [
            event(
                1,
                "assistant",
                "command",
                payload={
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd": "ruff check src", "workdir": "/w"}',
                    "call_id": "call_1",
                },
            ),
            event(
                2,
                "tool",
                "tool_result",
                payload={
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "name": "exec_command",
                    "output": "Process exited with code 0",
                    "success": True,
                    "is_error": False,
                    "exit_code": 0,
                },
            ),
        ]
        profile = profile_for(events)
        assert profile.base_commands["ruff"] == 1
        assert profile.tool_errors == 0
        assert [(run.category, run.ok) for run in profile.validation_runs] == [("lint", True)]

    def test_codex_v3_json_string_file_read_paths(self):
        events = [
            event(
                1,
                "assistant",
                "file_read",
                payload={
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path": "/w/src/app.py"}',
                    "call_id": "c1",
                },
            )
        ]
        profile = profile_for(events)
        assert profile.files_read == {"/w/src/app.py"}

    def test_codex_v3_patch_input_is_a_bare_string(self):
        patch = "*** Begin Patch\n*** Update File: /w/a.py\n@@\n+added\n-removed\n*** End Patch\n"
        events = [
            event(
                1,
                "assistant",
                "file_edit",
                payload={"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c1"},
            )
        ]
        profile = profile_for(events)
        assert profile.files_edited == {"/w/a.py"}
        assert profile.edited_lines == 2

    def test_repeated_results_for_one_call_are_counted_once(self):
        events = [
            event(1, "assistant", "file_edit", payload={"call_id": "c1", "name": "apply_patch"}),
            event(
                2,
                "tool",
                "tool_result",
                payload={"call_id": "c1", "type": "patch_apply_begin", "output": "start"},
            ),
            event(
                3, "tool", "tool_result", payload={"call_id": "c1", "type": "patch_apply_end", "output": "ok"}
            ),
        ]
        profile = profile_for(events)
        assert profile.tool_results == 1
        assert profile.unlinked_results == 0

    def test_a_late_failure_overturns_an_early_success(self):
        events = [
            event(1, "assistant", "command", payload={"call_id": "c1", "arguments": {"cmd": "make build"}}),
            event(2, "tool", "tool_result", payload={"call_id": "c1", "output": "building"}),
            event(
                3, "tool", "tool_result", payload={"call_id": "c1", "output": "Process exited with code 2"}
            ),
        ]
        profile = profile_for(events)
        assert profile.tool_results == 1
        assert profile.tool_errors == 1
        assert profile.ended_in_error is True

    def test_v3_lifecycle_hooks_are_not_tool_results(self):
        events = [
            event(1, "hook", "lifecycle", payload={"hookType": "preToolUse", "lifecycle": "hook_start"}),
            event(
                2,
                "hook",
                "lifecycle",
                payload={"hookType": "preToolUse", "lifecycle": "hook_end", "success": True},
            ),
        ]
        profile = profile_for(events, host="vscode-copilot")
        assert profile.tool_results == 0
        assert profile.tool_calls == 0

    @pytest.mark.parametrize(
        "text",
        [
            "# AGENTS.md instructions for /w\n\n<INSTRUCTIONS>\nstuff\n</INSTRUCTIONS>",
            '<skill-context name="commit">Base instructions</skill-context>',
            "<task-notification> <task-id>b3q</task-id> </task-notification>",
            "<turn_aborted> The user interrupted the turn",
            '<subagent_notification> {"agent_path": "x"}',
            "<environment_context>\n<cwd>/w</cwd>\n</environment_context>",
        ],
    )
    def test_v3_injected_user_envelopes_are_not_human_turns(self, text):
        profile = profile_for([event(1, "user", "message", payload={"message": {"content": text}})])
        assert profile.user_turns == 0
        assert profile.injected_user_messages == 1

    def test_injected_envelope_followed_by_a_real_request_still_counts(self):
        text = "<environment_context>\n<cwd>/w</cwd>\n</environment_context>\n\nadd a retry to the client"
        profile = profile_for([event(1, "user", "message", payload={"message": {"content": text}})])
        assert profile.user_turns == 1
        assert profile.first_prompt == "add a retry to the client"


class TestUsageExtraction:
    def test_codex_token_count_from_normalized_events(self):
        events = [
            event(1, "system", "attachment", payload={"turn_id": "t1", "model": "gpt-5.5"}),
            event(
                2,
                "system",
                "usage",
                payload={
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 800,
                            "output_tokens": 50,
                        },
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 800,
                            "output_tokens": 50,
                        },
                    },
                },
            ),
        ]
        stats = extract_usage("codex", "s1", events, Path("/nonexistent"))
        assert stats.source == "normalized"
        assert stats.by_model["gpt-5.5"]["input"] == 200
        assert stats.by_model["gpt-5.5"]["cache_read"] == 800
        assert stats.by_model["gpt-5.5"]["output"] == 50
        assert stats.total_tokens == 1050

    def test_codex_cumulative_without_last_usage_is_differenced(self):
        def token_event(seq, total_in, cached, out):
            return event(
                seq,
                "system",
                "attachment",
                payload={
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total_in,
                            "cached_input_tokens": cached,
                            "output_tokens": out,
                        }
                    },
                },
            )

        stats = extract_usage(
            "codex", "s1", [token_event(1, 100, 0, 10), token_event(2, 300, 50, 25)], Path("/x")
        )
        assert stats.totals["input"] == 250
        assert stats.totals["cache_read"] == 50
        assert stats.totals["output"] == 25

    def test_claude_usage_from_raw_transcript(self, tmp_path: Path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        rows = [
            {
                "requestId": "r1",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 6,
                        "cache_creation_input_tokens": 7416,
                        "cache_read_input_tokens": 16526,
                        "output_tokens": 333,
                    },
                },
            },
            {
                "requestId": "r1",
                "message": {"id": "m1", "model": "claude-opus-4-7", "usage": {"input_tokens": 6}},
            },
            {"message": {"id": "m2", "model": "<synthetic>", "usage": {"input_tokens": 99}}},
        ]
        (raw_dir / "transcript.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        stats = extract_usage("claude-code", "s1", [], raw_dir)
        assert stats.source == "raw"
        assert stats.by_model["claude-opus-4-7"] == {
            "input": 6,
            "cache_read": 16526,
            "cache_create": 7416,
            "output": 333,
            "reasoning": 0,
        }

    def test_copilot_agent_usage_and_credits(self, tmp_path: Path):
        raw_dir = tmp_path / "raw"
        (raw_dir / "sidecars").mkdir(parents=True)
        (raw_dir / "sidecars" / "session-store.json").write_text(
            json.dumps(
                {
                    "assistant_usage_events": [
                        {
                            "model": "claude-sonnet-5",
                            "input_tokens": 13164,
                            "cache_read_tokens": 8914,
                            "cache_write_tokens": 4248,
                            "output_tokens": 597,
                            "reasoning_tokens": 11,
                            "request_multiplier": 1.0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        stats = extract_usage("vscode-copilot", "s1", [], raw_dir)
        bucket = stats.by_model["claude-sonnet-5"]
        assert bucket["input"] == 13164 - 8914 - 4248
        assert bucket["cache_read"] == 8914
        assert bucket["cache_create"] == 4248
        assert stats.credits == pytest.approx(1.0)

    def test_vscode_chat_snapshot_usage(self, tmp_path: Path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "session.snapshot.json").write_text(
            json.dumps(
                {
                    "requests": [
                        {
                            "modelId": "copilot/auto",
                            "promptTokens": 18145,
                            "completionTokens": 180,
                            "copilotCredits": 0.98,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        stats = extract_usage("vscode-copilot", "s1", [], raw_dir)
        assert stats.by_model["copilot/auto"]["input"] == 18145
        assert stats.by_model["copilot/auto"]["output"] == 180
        assert stats.credits == pytest.approx(0.98)

    def test_claude_v3_usage_is_deduplicated_across_message_parts(self):
        usage = {
            "input_tokens": 6,
            "cache_creation_input_tokens": 7416,
            "cache_read_input_tokens": 16526,
            "output_tokens": 333,
        }
        message = {"id": "msg_1", "model": "claude-opus-4-7", "usage": usage}
        events = [
            event(
                index,
                "assistant",
                kind,
                payload={"requestId": "req_1", "message": dict(message)},
                host="claude-code",
            )
            for index, kind in enumerate(("message", "command", "file_edit", "reasoning"), start=1)
        ]
        stats = extract_usage("claude-code", "s1", events, Path("/nonexistent"))
        assert stats.source == "normalized"
        assert stats.by_model["claude-opus-4-7"] == {
            "input": 6,
            "cache_read": 16526,
            "cache_create": 7416,
            "output": 333,
            "reasoning": 0,
        }

    def test_distinct_claude_responses_are_both_counted(self):
        def usage_event(index, message_id, request_id):
            return event(
                index,
                "assistant",
                "message",
                payload={
                    "requestId": request_id,
                    "message": {
                        "id": message_id,
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
                host="claude-code",
            )

        stats = extract_usage(
            "claude-code",
            "s1",
            [usage_event(1, "msg_1", "req_1"), usage_event(2, "msg_2", "req_2")],
            Path("/nonexistent"),
        )
        assert stats.by_model["claude-opus-4-7"]["input"] == 20

    def test_codex_v3_usage_event_with_null_info_is_ignored(self):
        events = [
            event(1, "system", "usage", payload={"type": "token_count", "info": None, "rate_limits": {}}),
        ]
        stats = extract_usage("codex", "s1", events, Path("/nonexistent"))
        assert stats.has_usage is False

    def test_no_usage_reports_none(self):
        stats = extract_usage("codex", "s1", [event(1, "user", "message")], Path("/nonexistent"))
        assert stats.source == "none"
        assert stats.has_usage is False


class TestPricing:
    def test_model_normalization(self):
        assert normalize_model_id("copilot/claude-sonnet-4.6") == "claude-sonnet-4-6"
        assert normalize_model_id("gpt-5.4-20260501") == "gpt-5-4"
        assert normalize_model_id(None) == ""

    def test_exact_family_and_unknown_bases(self):
        pricing = PricingMap.load()
        assert pricing.rates_for("claude-opus-4-7").basis == "exact"
        assert pricing.rates_for("copilot/claude-sonnet-4.6").priced
        assert pricing.rates_for("totally-unknown-model-9").basis == "unknown"

    def test_price_uses_normalized_buckets(self):
        pricing = PricingMap(
            {
                "demo": {
                    "input_cost_per_token": 1e-06,
                    "output_cost_per_token": 2e-06,
                    "cache_read_input_token_cost": 1e-07,
                    "cache_creation_input_token_cost": 5e-07,
                }
            }
        )
        cost = pricing.price(
            {"demo": {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000, "cache_create": 0}}
        )
        assert cost.total_usd == pytest.approx(3.1)
        assert cost.basis == "exact"

    def test_unpriced_models_are_reported(self):
        cost = PricingMap({}).price({"mystery": {"input": 10, "output": 10}})
        assert cost.basis == "unpriced"
        assert cost.unpriced_models == ("mystery",)

    def test_embedded_cost_wins(self):
        cost = PricingMap({}).price({"m": {"input": 10}}, embedded_usd=1.25)
        assert cost.basis == "embedded"
        assert cost.total_usd == pytest.approx(1.25)


class TestCommandClassification:
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("python -m pytest tests/ -q", "pytest"),
            (".venv/bin/ruff check src", "ruff"),
            ("npx tsc --noEmit", "tsc"),
            ("uv run pytest", "pytest"),
            ("npm run typecheck", "npm run typecheck"),
            ("FOO=1 BAR=2 cargo test", "cargo"),
            ("make check", "make check"),
        ],
    )
    def test_base_command(self, command, expected):
        assert get_base_command(command) == expected

    @pytest.mark.parametrize(
        "command,category",
        [
            ("pytest -q", "test"),
            ("mypy src", "typecheck"),
            ("ruff check src", "lint"),
            ("cargo build", "build"),
            ("git status", "vcs"),
            ("rg 'pytest' src", "search"),
            ("npm install", "package"),
            ("docker compose up", "ops"),
        ],
    )
    def test_segment_categories(self, command, category):
        assert category in classify_command(command)

    def test_compound_commands_pick_up_every_part(self):
        assert {"lint", "test"} <= classify_command("cd repo && ruff check src && pytest -q")

    def test_search_for_a_test_word_is_not_a_test_run(self):
        assert classify_command("rg 'npm test' .") == {"search"}

    def test_git_commit_detection(self):
        assert is_git_commit("git commit -m 'x'")
        assert is_git_commit("git -C /repo commit -F -")
        assert not is_git_commit("git commit --dry-run")
        assert not is_git_commit("git log --oneline")

    def test_command_text_handles_list_form(self):
        from retro.signals.commands import command_text

        assert command_text(
            event(1, "assistant", "command", payload={"command": ["bash", "-lc", "pytest -q"]})
        ) == ("pytest -q")

    def test_whitespace_only_command_does_not_crash(self):
        assert classify_command("' '") == {"other"}


class TestCorrections:
    @pytest.mark.parametrize(
        "text",
        [
            "no, that's not what I asked for",
            "that's wrong, revert it",
            "you broke the build",
            "still failing after your change",
            "actually, use the other helper",
            "don't touch that file",
        ],
    )
    def test_corrections_detected(self, text):
        assert is_correction(text)

    @pytest.mark.parametrize(
        "text",
        [
            "RESEARCH TASK (read-only; do not modify any files, do not create sandboxes, "
            "do not start or stop Docker, and do not change any configuration anywhere in the repo)",
            "no need to run the tests again",
            "go ahead",
            "add a retry with exponential backoff",
        ],
    )
    def test_non_corrections_ignored(self, text):
        assert not is_correction(text)


class TestClassifiers:
    def test_empty_session_is_unknown(self):
        assert classify_outcome(profile_for([])).value == "unknown"

    def test_commit_means_success(self):
        events = [
            event(1, "user", "message", payload={"text": "ship it"}),
            event(
                2, "assistant", "command", payload={"call_id": "c", "arguments": {"cmd": "git commit -m x"}}
            ),
            event(3, "tool", "tool_result", payload={"call_id": "c", "output": "1 file changed"}),
        ]
        judgement = classify_outcome(profile_for(events))
        assert judgement.value == "success"
        assert judgement.evidence

    def test_failed_validation_with_edits_is_partial(self):
        events = [
            event(1, "user", "message", payload={"text": "fix it"}),
            event(2, "assistant", "file_edit", payload={"file_path": "/w/a.py"}),
            event(3, "assistant", "command", payload={"call_id": "c", "arguments": {"cmd": "pytest"}}),
            event(4, "tool", "tool_result", payload={"call_id": "c", "is_error": True, "output": "1 failed"}),
        ]
        assert classify_outcome(profile_for(events)).value == "partial"

    def test_usage_limit_ends_as_abandoned(self):
        events = [
            event(1, "user", "message", payload={"text": "keep going"}),
            event(2, "assistant", "file_edit", payload={"file_path": "/w/a.py"}),
            event(
                3,
                "assistant",
                "message",
                payload={"text": "You've hit your session limit \u00b7 resets 8:50pm"},
            ),
        ]
        assert classify_outcome(profile_for(events)).value == "abandoned"

    def test_mid_step_final_message_is_partial(self):
        events = [
            event(1, "user", "message", payload={"text": "do the migration"}),
            event(2, "assistant", "file_edit", payload={"file_path": "/w/a.py"}),
            event(
                3, "assistant", "message", payload={"text": "Let me build the baseline inventory precisely."}
            ),
        ]
        assert classify_outcome(profile_for(events)).value == "partial"

    def test_explicit_inability_is_failed(self):
        events = [
            event(1, "user", "message", payload={"text": "use the mcp tool"}),
            event(
                2,
                "assistant",
                "message",
                payload={"text": "I can't complete this request because the tool is unavailable."},
            ),
        ]
        assert classify_outcome(profile_for(events)).value == "failed"

    def test_one_turn_opening_prompt_is_not_a_closing_verdict(self):
        events = [
            event(1, "user", "message", payload={"text": "check that everything works perfectly, thanks"})
        ]
        assert classify_outcome(profile_for(events)).value == "unknown"

    def test_work_kind_uses_prompt_then_actions(self):
        build = profile_for([event(1, "user", "message", payload={"text": "implement a new retry command"})])
        assert classify_work_kind(build).value == "build"
        explore = profile_for(
            [
                event(
                    1, "user", "message", payload={"text": "where is the retry logic? explain how it works"}
                ),
                event(2, "assistant", "file_read", payload={"file_path": "/w/a.py"}),
            ]
        )
        assert classify_work_kind(explore).value == "explore"
        chat = profile_for(
            [
                event(1, "user", "message", payload={"text": "what is 12*12?"}),
                event(2, "assistant", "message", payload={"text": "144"}),
            ]
        )
        assert classify_work_kind(chat).value == "chat"

    def test_opaque_runs_are_other(self):
        opaque = profile_for([event(1, "assistant", "command", payload={"arguments": {"cmd": "ls"}})])
        assert classify_work_kind(opaque).value == "other"

    def test_primary_language(self):
        events = [
            event(1, "assistant", "file_edit", payload={"file_path": "/w/a.py"}),
            event(2, "assistant", "file_edit", payload={"file_path": "/w/b.py"}),
            event(3, "assistant", "file_edit", payload={"file_path": "/w/c.ts"}),
        ]
        values, _, _ = evaluate(profile_for(events))
        assert values["work.primary_language"] == "python"


@pytest.fixture
def archive(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    return layout


def seed_session(layout: Layout, host: str, session_id: str, events) -> None:
    write_events(layout.normalized_path(host, session_id), events)
    layout.raw_dir(host, session_id).mkdir(parents=True, exist_ok=True)


def sample_events(host="codex"):
    return [
        event(
            1, "user", "message", payload={"text": "add a retry"}, timestamp="2026-01-01T00:00:00Z", host=host
        ),
        event(
            2,
            "assistant",
            "file_edit",
            payload={"call_id": "c1", "file_path": "/w/a.py"},
            timestamp="2026-01-01T00:00:05Z",
            host=host,
        ),
        event(
            3,
            "tool",
            "tool_result",
            payload={"call_id": "c1", "output": "ok"},
            timestamp="2026-01-01T00:00:06Z",
            host=host,
        ),
    ]


class TestStore:
    def test_round_trip_and_headline_values(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update = update_signals(archive)
        assert update.total == 1
        assert update.added_keys == ["codex/s1"]

        store = load_signal_store(archive)
        assert store.version == definitions_version()
        assert store.is_current()
        values = store.values("codex/s1")
        for signal_id in ("activity.user_turns", "activity.file_edits", "outcome.status", "work.kind"):
            assert signal_id in values
        assert values["activity.user_turns"] == 1
        assert values["activity.file_edits"] == 1
        assert (archive.root / "signals" / "definitions.json").exists()
        assert (archive.root / "signals" / "sessions.jsonl").exists()

    def test_record_shape_matches_the_contract(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        line = (archive.root / "signals" / "sessions.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(line)
        assert set(record) <= {"key", "fingerprint", "version", "values", "evidence", "confidence"}
        assert record["key"] == "codex/s1"
        assert record["fingerprint"].startswith("stat1:")
        for refs in (record.get("evidence") or {}).values():
            assert len(refs) <= 5
        for value in (record.get("confidence") or {}).values():
            assert value != 1.0

    def test_legacy_artifacts_are_removed(self, archive: Layout):
        signals = archive.root / "signals"
        signals.mkdir(parents=True, exist_ok=True)
        for name in ("readings.jsonl", "aggregates.json", "summary.md"):
            (signals / name).write_text("legacy", encoding="utf-8")
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        for name in ("readings.jsonl", "aggregates.json", "summary.md"):
            assert not (signals / name).exists()

    def test_unchanged_sessions_are_reused(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        second = update_signals(archive)
        assert second.reused == 1
        assert second.recomputed == 0

    def test_a_noop_update_does_not_rewrite_the_files(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        signals = archive.root / "signals"
        before = {
            name: (signals / name).stat().st_mtime_ns for name in ("definitions.json", "sessions.jsonl")
        }
        update = update_signals(archive)
        assert update.rewritten is False
        after = {name: (signals / name).stat().st_mtime_ns for name in ("definitions.json", "sessions.jsonl")}
        assert after == before

    def test_a_changed_session_rewrites_the_files(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        signals = archive.root / "signals"
        before = (signals / "sessions.jsonl").stat().st_mtime_ns
        seed_session(
            archive,
            "codex",
            "s1",
            sample_events()
            + [event(4, "user", "message", payload={"text": "thanks"}, timestamp="2026-01-01T00:01:00Z")],
        )
        update = update_signals(archive)
        assert update.rewritten is True
        assert (signals / "sessions.jsonl").stat().st_mtime_ns != before

    def test_a_noop_update_still_removes_legacy_artifacts(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        legacy = archive.root / "signals" / "readings.jsonl"
        legacy.write_text("legacy", encoding="utf-8")
        update = update_signals(archive)
        assert update.rewritten is True
        assert not legacy.exists()

    def test_a_pricing_change_invalidates_stored_costs(self, archive: Layout, tmp_path: Path, monkeypatch):
        from retro.signals import pricing

        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        first_version = load_signal_store(archive).version

        snapshot = tmp_path / "litellm-pricing.json"
        snapshot.write_text(json.dumps({"gpt-5.5": {"input_cost_per_token": 1e-05}}), encoding="utf-8")
        monkeypatch.setattr(pricing, "PRICING_SNAPSHOT", snapshot)

        update = update_signals(archive)
        assert update.changed_keys == ["codex/s1"]
        assert update.rewritten is True
        assert load_signal_store(archive).version != first_version

    def test_changed_fingerprint_recomputes(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        seed_session(
            archive,
            "codex",
            "s1",
            sample_events()
            + [event(4, "user", "message", payload={"text": "thanks"}, timestamp="2026-01-01T00:01:00Z")],
        )
        update = update_signals(archive)
        assert update.changed_keys == ["codex/s1"]
        assert load_signal_store(archive).values("codex/s1")["activity.user_turns"] == 2

    def test_version_change_invalidates_every_session(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        path = archive.root / "signals" / "sessions.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["version"] = "stale"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert update_signals(archive).changed_keys == ["codex/s1"]

    def test_removed_sessions_are_dropped(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        seed_session(archive, "codex", "s2", sample_events())
        update_signals(archive)
        archive.normalized_path("codex", "s2").unlink()
        update = update_signals(archive)
        assert update.removed_keys == ["codex/s2"]
        assert "codex/s2" not in load_signal_store(archive)

    def test_filtered_run_can_keep_other_sessions(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        seed_session(archive, "claude-code", "s2", sample_events(host="claude-code"))
        update_signals(archive)
        update = update_signals(archive, host="codex", prune=False)
        assert update.removed_keys == []
        assert update.total == 2

    def test_catalog_protocol_drives_selection(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        seed_session(archive, "codex", "s2", sample_events())

        class Record:
            def __init__(self, session_id):
                self.key = f"codex/{session_id}"
                self.host = "codex"
                self.session_id = session_id
                self.fingerprint = "fp-1"
                self.client = "codex-cli"
                self.agent = "codex"
                self.interactive = True
                self.project = {"id": "github.com/o/r", "name": "r"}
                self.tags = ["automated"]
                self.started_at = "2026-01-01T00:00:00Z"
                self.models = ["gpt-5.5"]

        class Catalog:
            def included(self):
                return [Record("s1")]

        update = update_signals(archive, Catalog())
        assert update.total == 1
        assert load_signal_store(archive).keys == ["codex/s1"]
        assert update_signals(archive, Catalog()).reused == 1

    def test_force_recomputes_everything(self, archive: Layout):
        seed_session(archive, "codex", "s1", sample_events())
        update_signals(archive)
        assert update_signals(archive, force=True).changed_keys == ["codex/s1"]

    def test_normalized_source_skips_temp_cwd_sessions(self, archive: Layout):
        for session_id, cwd in (
            ("temp1", "/private/var/folders/kw/T/tmpabc"),
            ("real1", "/Users/me/Dev/app"),
        ):
            seed_session(archive, "vscode-copilot", session_id, sample_events(host="vscode-copilot"))
            sidecars = archive.raw_dir("vscode-copilot", session_id) / "sidecars"
            sidecars.mkdir(parents=True, exist_ok=True)
            (sidecars / "workspace.yaml").write_text(
                f"id: {session_id}\ncwd: {cwd}\nclient_name: github/cli\n", encoding="utf-8"
            )
        assert [record.key for record in NormalizedSource(layout=archive).included()] == [
            "vscode-copilot/real1"
        ]
        assert len(list(NormalizedSource(layout=archive, skip_temp=False).included())) == 2


class TestSummarize:
    def test_statistics_per_kind(self):
        values = [
            {"usage.cost_usd": 1.0, "outcome.committed": True, "work.kind": "build"},
            {"usage.cost_usd": 3.0, "outcome.committed": False, "work.kind": "build"},
            {"usage.cost_usd": 5.0, "outcome.committed": True, "work.kind": "debug"},
            {"outcome.committed": True},
        ]
        summary = summarize(values, definitions_payload())
        assert summary["sessions"] == 4
        cost = summary["signals"]["usage.cost_usd"]
        assert cost["n"] == 3
        assert cost["sum"] == pytest.approx(9.0)
        assert cost["mean"] == pytest.approx(3.0)
        assert cost["median"] == pytest.approx(3.0)
        assert cost["max"] == pytest.approx(5.0)
        assert cost["unit"] == "usd"
        assert cost["coverage"] == pytest.approx(0.75)
        committed = summary["signals"]["outcome.committed"]
        assert (committed["true"], committed["false"]) == (3, 1)
        assert committed["true_rate"] == pytest.approx(0.75)
        kind = summary["signals"]["work.kind"]
        assert kind["counts"] == {"build": 2, "debug": 1}
        assert kind["top"] == "build"

    def test_percentiles(self):
        block = summarize([{"n": float(i)} for i in range(1, 11)])["signals"]["n"]
        assert block["median"] == pytest.approx(5.5)
        assert block["p90"] == pytest.approx(9.1)

    def test_rate_and_category_share_and_trend(self):
        from retro.signals import category_share, rate, weekly_trend

        values = [
            {"friction.tool_errors": 1, "activity.tool_calls": 10, "outcome.status": "success"},
            {"friction.tool_errors": 3, "activity.tool_calls": 10, "outcome.status": "failed"},
        ]
        assert rate(values, "friction.tool_errors", "activity.tool_calls") == pytest.approx(0.2)
        assert category_share(values, "outcome.status", ["success"]) == pytest.approx(0.5)
        trend = weekly_trend(
            [("2026-01-05T00:00:00Z", values[0]), ("2026-01-06T00:00:00Z", values[1])],
            "friction.tool_errors",
        )
        assert list(trend) == ["2026-W02"]
        assert trend["2026-W02"]["n"] == 2

    def test_compare_flags_small_groups(self):
        from retro.signals import compare

        groups = {"codex": [{"usage.cost_usd": 1.0}] * 6, "claude": [{"usage.cost_usd": 2.0}]}
        result = compare(groups, definitions_payload(), min_sessions=5)
        assert result["groups"]["codex"]["sufficient"] is True
        assert result["groups"]["claude"]["sufficient"] is False
