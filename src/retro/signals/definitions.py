"""The signal catalogue.

Each signal answers part of one of the questions the archive exists to answer:
which agent/model/client to use for which work, where time and money go, where
agents struggle and why, which steering habits to change, whether a session
succeeded, what was risky, and whether the capture can be trusted at all.

Every function here is a pure read over a `SessionProfile`; the single scan has
already happened. Returning `None` means "not applicable to this session" and
the value is omitted from the store rather than stored as a zero.
"""

from __future__ import annotations

from . import commands as cmd
from .classify import OUTCOME_CATEGORIES, WORK_KINDS, classify_outcome, classify_work_kind, primary_language
from .profile import SessionProfile
from .registry import SignalValue, register

LANGUAGES = (
    "python",
    "typescript",
    "javascript",
    "rust",
    "go",
    "java",
    "kotlin",
    "swift",
    "ruby",
    "php",
    "csharp",
    "c",
    "cpp",
    "objc",
    "shell",
    "markdown",
    "config",
    "web",
    "sql",
    "infra",
    "build",
)
USAGE_SOURCES = ("normalized", "raw", "none")
COST_BASES = ("exact", "family", "mixed", "embedded", "unpriced", "none")


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _median(values) -> float | None:
    data = sorted(values)
    if not data:
        return None
    middle = len(data) // 2
    if len(data) % 2:
        return round(data[middle], 3)
    return round((data[middle - 1] + data[middle]) / 2, 3)


# --- usage: the raw accounting, deliberately polarity-neutral ----------------
# A $5 session is not worse than a $1 session; it is worse only at equal
# outcome. Judgement lives in `efficiency.*` and in the metric layer.


@register(
    "usage.cost_usd",
    title="Cost",
    category="usage",
    kind="money",
    unit="usd",
    method="exact",
    description=(
        "Token cost at list API prices for the models actually used. For "
        "subscription agents (Copilot) this is a counterfactual API price, not "
        "what was billed."
    ),
    applicability="sessions with recoverable token usage and a priced model",
)
def _cost_usd(p: SessionProfile):
    if p.cost.basis in ("none", "unpriced"):
        return None
    confidence = 1.0 if p.cost.basis in ("exact", "embedded") else 0.6
    return SignalValue(round(p.cost.total_usd, 6), confidence=confidence)


@register(
    "usage.tokens_total",
    title="Tokens",
    category="usage",
    kind="count",
    unit="tokens",
    method="exact",
    description="Input + cache read + cache write + output tokens across all models.",
    applicability="sessions with recoverable token usage",
)
def _tokens_total(p: SessionProfile):
    return p.usage.total_tokens if p.usage.has_usage else None


@register(
    "usage.tokens_input",
    title="Input tokens",
    category="usage",
    kind="count",
    unit="tokens",
    method="exact",
    description="Fresh, uncached prompt tokens (cache reads and writes excluded for every host).",
    applicability="sessions with recoverable token usage",
)
def _tokens_input(p: SessionProfile):
    return p.usage.totals["input"] if p.usage.has_usage else None


@register(
    "usage.tokens_output",
    title="Output tokens",
    category="usage",
    kind="count",
    unit="tokens",
    method="exact",
    description="Completion tokens, including reasoning tokens when the provider bills them as output.",
    applicability="sessions with recoverable token usage",
)
def _tokens_output(p: SessionProfile):
    return p.usage.totals["output"] if p.usage.has_usage else None


@register(
    "usage.tokens_cache_read",
    title="Cached tokens",
    category="usage",
    kind="count",
    unit="tokens",
    method="exact",
    description="Prompt tokens served from the provider cache.",
    applicability="hosts that report prompt caching (Codex, Claude, Copilot CLI)",
)
def _tokens_cache_read(p: SessionProfile):
    return p.usage.totals["cache_read"] if p.usage.has_usage else None


@register(
    "usage.tokens_cache_write",
    title="Cache writes",
    category="usage",
    kind="count",
    unit="tokens",
    method="exact",
    description="Prompt tokens written into the provider cache (billed at a premium by Anthropic).",
    applicability="hosts that report cache creation (Claude, Copilot CLI)",
)
def _tokens_cache_write(p: SessionProfile):
    return p.usage.totals["cache_create"] if p.usage.has_usage else None


@register(
    "usage.duration_s",
    title="Wall clock",
    category="usage",
    kind="duration",
    unit="seconds",
    method="exact",
    description="Seconds between the first and last timestamped event.",
    applicability="sessions with at least two timestamped events",
)
def _duration(p: SessionProfile):
    return p.duration_s


@register(
    "usage.active_s",
    title="Active time",
    category="usage",
    kind="duration",
    unit="seconds",
    method="exact",
    description="Sum of gaps between consecutive events, each capped at 300 s; excludes idle time.",
    applicability="sessions with at least two timestamped events",
)
def _active(p: SessionProfile):
    return p.active_s


@register(
    "usage.credits",
    title="Premium requests",
    category="usage",
    kind="count",
    unit="count",
    method="exact",
    description="Copilot premium-request multipliers consumed; the real billing unit for Copilot.",
    applicability="Copilot sessions that record request multipliers or credits",
)
def _credits(p: SessionProfile):
    if p.usage.credits is None:
        return None
    return round(p.usage.credits, 4)


# --- activity: what the agent actually did -----------------------------------


@register(
    "activity.user_turns",
    title="Human turns",
    category="activity",
    kind="count",
    unit="count",
    method="heuristic",
    description=(
        "User messages that a human actually typed. Slash-command echoes, local "
        "command output, system reminders and tool-result turns are excluded."
    ),
)
def _user_turns(p: SessionProfile):
    return p.user_turns


@register(
    "activity.assistant_turns",
    title="Assistant turns",
    category="activity",
    kind="count",
    unit="count",
    method="exact",
    description="Assistant message events, excluding reasoning and tool traffic.",
)
def _assistant_turns(p: SessionProfile):
    return p.assistant_messages


@register(
    "activity.tool_calls",
    title="Tool calls",
    category="activity",
    kind="count",
    unit="count",
    method="exact",
    description="Assistant-initiated tool invocations: commands, file reads, file edits and other tools.",
)
def _tool_calls(p: SessionProfile):
    return p.tool_calls


@register(
    "activity.file_edits",
    title="File edits",
    category="activity",
    kind="count",
    unit="count",
    method="exact",
    description="file_edit events (write, patch, multi-edit).",
)
def _file_edits(p: SessionProfile):
    return p.file_edits


@register(
    "activity.files_touched",
    title="Files edited",
    category="activity",
    kind="count",
    unit="count",
    method="heuristic",
    description="Distinct file paths named by edit events.",
)
def _files_touched(p: SessionProfile):
    return len(p.files_edited)


@register(
    "activity.files_read",
    title="Files read",
    category="activity",
    kind="count",
    unit="count",
    method="heuristic",
    description="Distinct file paths named by read events; a proxy for how much context was pulled in.",
)
def _files_read(p: SessionProfile):
    return len(p.files_read)


@register(
    "activity.commands",
    title="Commands",
    category="activity",
    kind="count",
    unit="count",
    method="exact",
    description="Tool calls that carried a shell command line.",
)
def _commands(p: SessionProfile):
    return p.command_calls


@register(
    "activity.edited_lines",
    title="Lines changed",
    category="activity",
    kind="count",
    unit="count",
    method="heuristic",
    description=(
        "Added/removed lines summed over edits that carry a diff or patch body; "
        "edits that only carry a replacement string count its line length."
    ),
    applicability="sessions whose edit payloads carry diff or replacement text",
)
def _edited_lines(p: SessionProfile):
    if not p.file_edits or not p.edits_with_diff:
        return None
    coverage = p.edits_with_diff / p.file_edits
    return SignalValue(p.edited_lines, confidence=round(min(1.0, 0.5 + coverage / 2), 2))


# --- outcome -----------------------------------------------------------------


@register(
    "outcome.status",
    title="Outcome",
    category="outcome",
    kind="category",
    categories=OUTCOME_CATEGORIES,
    polarity="higher_better",
    method="heuristic",
    version=1,
    description=(
        "First matching rule of: empty capture -> unknown; ends on a user abort "
        "-> abandoned; last human message reports failure -> failed; a commit "
        "succeeded or edits plus a passing validation run -> success; positive "
        "closing message -> success; edits without validation or ending in error "
        "-> partial; otherwise failed/unknown."
    ),
)
def _outcome_status(p: SessionProfile):
    judgement = classify_outcome(p)
    return SignalValue(judgement.value, judgement.evidence, judgement.confidence)


@register(
    "outcome.validated",
    title="Validated",
    category="outcome",
    kind="boolean",
    polarity="higher_better",
    method="heuristic",
    description=(
        "True when the last test/typecheck/lint/build command of the session "
        "succeeded. Confidence is higher when a real test or typecheck ran."
    ),
    applicability="sessions that ran at least one validation command",
)
def _validated(p: SessionProfile):
    validated = p.validated
    if validated is None:
        return None
    evidence = tuple(run.event_id for run in p.validation_runs[-5:])
    return SignalValue(validated, evidence, 0.9 if p.strong_validation else 0.7)


@register(
    "outcome.committed",
    title="Committed",
    category="outcome",
    kind="boolean",
    polarity="higher_better",
    method="heuristic",
    description="True when a `git commit` ran and neither failed nor reported nothing to commit.",
)
def _committed(p: SessionProfile):
    evidence = tuple(event_id for event_id, ok in p.commit_events if ok)[:5]
    return SignalValue(p.committed, evidence, 0.95 if p.commit_events else 1.0)


@register(
    "outcome.ended_in_error",
    title="Ended in error",
    category="outcome",
    kind="boolean",
    polarity="lower_better",
    method="heuristic",
    description="True when the last tool result of the session was a failure that was never retried.",
    applicability="sessions with at least one tool result",
)
def _ended_in_error(p: SessionProfile):
    if not p.tool_results:
        return None
    return SignalValue(p.ended_in_error, tuple(p.error_events[-3:]))


# --- friction ----------------------------------------------------------------


@register(
    "friction.user_corrections",
    title="Corrections",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="regex",
    description=(
        "Human turns after the first that reject or reverse what the agent did "
        '("no", "that\'s wrong", "don\'t ...", "still failing", "revert").'
    ),
)
def _corrections(p: SessionProfile):
    return SignalValue(len(p.correction_events), tuple(p.correction_events[:5]), 0.8)


@register(
    "friction.tool_errors",
    title="Tool errors",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description=(
        "Tool results that failed by any mechanism: is_error, success=false, a "
        "non-empty error field, a non-zero nested exit code, or a non-zero exit "
        "code in the output text."
    ),
)
def _tool_errors(p: SessionProfile):
    return SignalValue(p.tool_errors, tuple(p.error_events[:5]))


@register(
    "friction.tool_error_rate",
    title="Tool error rate",
    category="friction",
    kind="ratio",
    unit="ratio",
    polarity="lower_better",
    method="heuristic",
    description="Failed tool results divided by all tool results.",
    applicability="sessions with at least one tool result",
)
def _tool_error_rate(p: SessionProfile):
    return p.tool_error_rate


@register(
    "friction.interruptions",
    title="Interruptions",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description=(
        "User-initiated aborts: Codex `turn_aborted`, Copilot `abort` with a user "
        "reason, Claude's `[Request interrupted by user]`, explicit cancel flags."
    ),
)
def _interruptions(p: SessionProfile):
    return SignalValue(len(p.interruption_events), tuple(p.interruption_events[:5]), 0.85)


@register(
    "friction.longest_error_streak",
    title="Longest error streak",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description="Longest run of consecutive failing tool results; a direct measure of being stuck.",
    applicability="sessions with at least one tool result",
)
def _error_streak(p: SessionProfile):
    if not p.tool_results:
        return None
    return p.error_streak_max


@register(
    "friction.edit_churn",
    title="Edit churn",
    category="friction",
    kind="ratio",
    unit="ratio",
    polarity="lower_better",
    method="heuristic",
    description=(
        "Share of edits that re-touch a file already edited in the same session; "
        "high churn means the agent kept rewriting its own work."
    ),
    applicability="sessions with at least two file edits",
)
def _edit_churn(p: SessionProfile):
    return p.edit_churn


@register(
    "friction.permission_denied",
    title="Permission denials",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description="Permission requests that were denied or could not be approved, blocking the agent.",
)
def _permission_denied(p: SessionProfile):
    return p.permission_denials


@register(
    "friction.provider_errors",
    title="Provider errors",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="regex",
    description=(
        "Events mentioning an API error, rate limit, quota exhaustion or overload "
        "from the model provider rather than from the user's code."
    ),
)
def _provider_errors(p: SessionProfile):
    return SignalValue(p.provider_errors + p.rate_limit_events, confidence=0.6)


@register(
    "friction.context_compactions",
    title="Compactions",
    category="friction",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description=(
        "Context compaction events. Each one means the session outgrew the window "
        "and paid to re-summarize itself."
    ),
)
def _compactions(p: SessionProfile):
    return p.compactions


# --- efficiency --------------------------------------------------------------


@register(
    "efficiency.cost_per_edit",
    title="Cost per edit",
    category="efficiency",
    kind="money",
    unit="usd",
    polarity="lower_better",
    method="heuristic",
    description="Session cost divided by the number of file edits it produced.",
    applicability="sessions with a priced cost and at least one file edit",
)
def _cost_per_edit(p: SessionProfile):
    if p.cost.basis in ("none", "unpriced") or not p.file_edits:
        return None
    return round(p.cost.total_usd / p.file_edits, 6)


@register(
    "efficiency.cache_read_share",
    title="Cache hit share",
    category="efficiency",
    kind="ratio",
    unit="ratio",
    polarity="higher_better",
    method="exact",
    description=(
        "Cached prompt tokens divided by all prompt-side tokens. Cache reads cost "
        "roughly a tenth of fresh input, so this is the single biggest cost lever."
    ),
    applicability="sessions with prompt-side token usage",
)
def _cache_share(p: SessionProfile):
    totals = p.usage.totals
    prompt_side = totals["input"] + totals["cache_read"] + totals["cache_create"]
    return _ratio(totals["cache_read"], prompt_side)


@register(
    "efficiency.time_to_first_edit_s",
    title="Time to first edit",
    category="efficiency",
    kind="duration",
    unit="seconds",
    polarity="lower_better",
    method="exact",
    description="Seconds from the first human turn to the first file edit.",
    applicability="sessions with a timestamped human turn followed by an edit",
)
def _time_to_first_edit(p: SessionProfile):
    return p.time_to_first_edit_s


@register(
    "efficiency.idle_share",
    title="Idle share",
    category="efficiency",
    kind="ratio",
    unit="ratio",
    method="exact",
    description=(
        "Share of wall-clock time with no events for more than 300 s; high values "
        "mean the session was parked, not that the agent was slow."
    ),
    applicability="sessions longer than 60 s",
)
def _idle_share(p: SessionProfile):
    if p.duration_s is None or p.active_s is None or p.duration_s < 60:
        return None
    return _ratio(max(0.0, p.duration_s - p.active_s), p.duration_s)


# --- collaboration -----------------------------------------------------------


@register(
    "collaboration.first_prompt_chars",
    title="Opening prompt size",
    category="collaboration",
    kind="count",
    unit="count",
    method="exact",
    description="Characters in the first human turn, after stripping injected blocks.",
    applicability="sessions with at least one human turn",
)
def _first_prompt_chars(p: SessionProfile):
    return len(p.first_prompt) if p.first_prompt else None


@register(
    "collaboration.turns_before_first_edit",
    title="Turns before first edit",
    category="collaboration",
    kind="count",
    unit="count",
    method="exact",
    description="Human turns spent before the agent made its first edit.",
    applicability="sessions with at least one file edit",
)
def _turns_before_edit(p: SessionProfile):
    return p.turns_before_first_edit if p.file_edits else None


@register(
    "collaboration.reply_latency_s",
    title="Human reply latency",
    category="collaboration",
    kind="duration",
    unit="seconds",
    method="exact",
    version=2,
    description=(
        "Median seconds between the agent's last output and the human replying, "
        "capped at one hour per gap. High values mean supervised, bursty work."
    ),
    applicability="sessions with at least one human reply after an assistant turn",
)
def _reply_latency(p: SessionProfile):
    return _median(p.reply_gaps)


# --- risk --------------------------------------------------------------------


@register(
    "risk.secret_exposure",
    title="Secret exposure",
    category="risk",
    kind="boolean",
    polarity="lower_better",
    method="regex",
    description=(
        "True when a credential-shaped string (provider API key, token, private "
        "key block, JWT, or a non-placeholder secret assignment) appears anywhere "
        "in the captured rollout."
    ),
)
def _secret_exposure(p: SessionProfile):
    return SignalValue(bool(p.secret_kinds), tuple(p.secret_events[:5]), 0.75 if p.secret_kinds else 1.0)


@register(
    "risk.secret_kinds",
    title="Secret kinds",
    category="risk",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="regex",
    description="Number of distinct credential patterns seen; more kinds means a broader leak.",
)
def _secret_kinds(p: SessionProfile):
    return len(p.secret_kinds)


@register(
    "risk.destructive_commands",
    title="Destructive commands",
    category="risk",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="regex",
    description=(
        "Commands that can lose work or publish irreversibly: recursive deletes, "
        "`git reset --hard`, `git clean -fd`, force pushes, branch deletion, "
        "DROP/TRUNCATE, raw disk writes, `chmod 777`, prune -a, publish."
    ),
)
def _destructive(p: SessionProfile):
    return SignalValue(
        len(p.destructive_events), tuple(event_id for _, event_id in p.destructive_events[:5]), 0.85
    )


@register(
    "risk.edits_outside_workspace",
    title="Edits outside workspace",
    category="risk",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="heuristic",
    description="Absolute edited paths that lie outside the session's working directory.",
    applicability="sessions with a known absolute cwd and at least one absolute edited path",
)
def _edits_outside(p: SessionProfile):
    if not p.cwd or not p.files_edited:
        return None
    if not any(path.startswith("/") for path in p.files_edited):
        return None
    return SignalValue(p.edits_outside_workspace, confidence=0.7)


# --- quality: can this session's numbers be trusted? -------------------------


@register(
    "quality.capture_score",
    title="Capture quality",
    category="quality",
    kind="ratio",
    unit="ratio",
    polarity="higher_better",
    method="heuristic",
    description=(
        "1.0 minus penalties for unclassified events, missing timestamps, tool "
        "results that could not be linked to a call, missing token usage, and a "
        "truncated text scan."
    ),
)
def _capture_score(p: SessionProfile):
    if not p.events_total:
        return 0.0
    score = 1.0
    score -= min(0.3, p.unknown_events / p.events_total)
    score -= min(0.3, p.untimed_events / p.events_total)
    if p.tool_results:
        score -= min(0.2, p.unlinked_results / p.tool_results)
    if not p.usage.has_usage:
        score -= 0.2
    if p.secret_scan_truncated:
        score -= 0.1
    return round(max(0.0, min(1.0, score)), 3)


@register(
    "quality.unknown_events",
    title="Unclassified events",
    category="quality",
    kind="count",
    unit="count",
    polarity="lower_better",
    method="exact",
    description="Events the importer could not classify; everything derived from them is invisible.",
)
def _unknown_events(p: SessionProfile):
    return p.unknown_events


@register(
    "quality.untimed_event_ratio",
    title="Untimed events",
    category="quality",
    kind="ratio",
    unit="ratio",
    polarity="lower_better",
    method="exact",
    description="Share of events without a timestamp; these cannot be placed on any timeline.",
    applicability="sessions with at least one event",
)
def _untimed_ratio(p: SessionProfile):
    return _ratio(p.untimed_events, p.events_total)


@register(
    "quality.unlinked_result_ratio",
    title="Unlinked results",
    category="quality",
    kind="ratio",
    unit="ratio",
    polarity="lower_better",
    method="heuristic",
    description=(
        "Share of tool results that could not be matched to their call by id or "
        "parent; failure attribution for those is positional guesswork."
    ),
    applicability="sessions with at least one tool result",
)
def _unlinked_ratio(p: SessionProfile):
    return _ratio(p.unlinked_results, p.tool_results)


@register(
    "quality.usage_source",
    title="Usage source",
    category="quality",
    kind="category",
    categories=USAGE_SOURCES,
    method="exact",
    description="Where token usage came from: the normalized stream, the raw copy, or nowhere.",
)
def _usage_source(p: SessionProfile):
    return p.usage.source


@register(
    "quality.cost_basis",
    title="Cost basis",
    category="quality",
    kind="category",
    categories=COST_BASES,
    method="exact",
    description=(
        "How the dollar figure was produced: an exact rate entry, a model-family "
        "fallback, a mix, a provider-embedded cost, no rate at all, or no usage."
    ),
)
def _cost_basis(p: SessionProfile):
    return p.cost.basis


# --- work --------------------------------------------------------------------


@register(
    "work.kind",
    title="Work kind",
    category="work",
    kind="category",
    categories=WORK_KINDS,
    method="heuristic",
    description=(
        "Intent cues in the opening prompt, weighted against the realized action "
        "mix (edits vs reads, test/doc file share, ops commands, web searches, "
        "tool-free chat). Highest score wins."
    ),
)
def _work_kind(p: SessionProfile):
    judgement = classify_work_kind(p)
    return SignalValue(judgement.value, judgement.evidence, judgement.confidence)


@register(
    "work.primary_language",
    title="Primary language",
    category="work",
    kind="category",
    categories=LANGUAGES,
    method="heuristic",
    description="Most common language among edited files, falling back to read files.",
    applicability="sessions that touched at least one recognizable source file",
)
def _primary_language(p: SessionProfile):
    language = primary_language(p)
    if language is None:
        return None
    return SignalValue(language, confidence=0.9 if p.files_edited else 0.6)


@register(
    "work.validation_commands",
    title="Validation runs",
    category="work",
    kind="count",
    unit="count",
    method="heuristic",
    description=(
        "Test, typecheck, lint and build commands executed. Zero on a code-change "
        "session is the strongest single predictor of rework."
    ),
)
def _validation_commands(p: SessionProfile):
    return sum(p.command_categories.get(category, 0) for category in sorted(cmd.VALIDATION_CATEGORIES))
