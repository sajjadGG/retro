"""``retro-context``: bounded, read-only inspection of one SourceBundle.

The TaskDefiner and ScorerBuilder run inside a sandbox with no network and no
access to the archive. Every command emits JSON, paginates deterministically,
and refuses to read outside the bundle. There is deliberately no "summarize the
rollout" command: agents must cite event IDs and repository paths.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ...schema import NormalizedEvent, read_events
from ...utils import event_command_text, event_text, truncate
from .bundle import MANIFEST_NAME, BundleError, load_bundle, verify_bundle
from .schema import SchemaError, SourceBundleManifest

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT_CHARS = 4000
MAX_GREP_MATCHES = 200
MAX_READ_LINES = 2000
STATES = ("base", "outcome")


class ContextError(ValueError):
    """A retro-context request was invalid or out of bounds."""


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise ContextError("limit must be >= 1")
    return min(limit, MAX_LIMIT)


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve() if relative else root.resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ContextError(f"path escapes the bundle: {relative!r}")
    return candidate


@dataclass(frozen=True)
class RetroContext:
    """Read-only view of a materialized SourceBundle directory."""

    root: Path

    @classmethod
    def open(cls, root: Path) -> RetroContext:
        if not (root / MANIFEST_NAME).is_file():
            raise ContextError(f"not a source bundle: {root}")
        try:
            verified = verify_bundle(root)
        except (BundleError, SchemaError, OSError, json.JSONDecodeError) as error:
            raise ContextError(f"invalid source bundle {root}: {error}") from error
        if not verified:
            raise ContextError(f"source bundle checksum mismatch: {root}")
        return cls(root=root)

    # -- manifest ---------------------------------------------------------

    @property
    def manifest_model(self) -> SourceBundleManifest:
        return load_bundle(self.root).manifest

    def manifest(self) -> dict[str, Any]:
        return self.manifest_model.to_dict()

    # -- rollout ----------------------------------------------------------

    def _events(self) -> Iterator[NormalizedEvent]:
        events_path = self.root / "rollout" / "events.jsonl"
        if not events_path.is_file():
            raise ContextError("bundle has no rollout/events.jsonl")
        return read_events(events_path)

    def _filtered_events(
        self,
        *,
        actor: str | None = None,
        event_type: str | None = None,
    ) -> list[NormalizedEvent]:
        return [
            event
            for event in self._events()
            if (actor is None or event.actor == actor)
            and (event_type is None or event.event_type == event_type)
        ]

    def rollout_list(
        self,
        *,
        actor: str | None = None,
        event_type: str | None = None,
        cursor: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if cursor < 0:
            raise ContextError("cursor must be >= 0")
        bounded = _clamp_limit(limit)
        events = self._filtered_events(actor=actor, event_type=event_type)
        window = events[cursor : cursor + bounded]
        next_cursor = cursor + len(window)
        return {
            "total": len(events),
            "cursor": cursor,
            "limit": bounded,
            "next_cursor": next_cursor if next_cursor < len(events) else None,
            "events": [_event_summary(event) for event in window],
        }

    def rollout_show(self, event_id: str) -> dict[str, Any]:
        for event in self._events():
            if event.event_id == event_id:
                payload = event.to_dict()
                payload["text"] = truncate(event_text(event), MAX_TEXT_CHARS)
                return payload
        raise ContextError(f"unknown event_id: {event_id}")

    def rollout_search(
        self,
        query: str,
        *,
        actor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if not query:
            raise ContextError("search query must not be empty")
        bounded = _clamp_limit(limit)
        needle = query.lower()
        matches: list[dict[str, Any]] = []
        total = 0
        for event in self._filtered_events(actor=actor):
            haystack = "\n".join(
                filter(None, (event.summary, event_text(event), event_command_text(event)))
            ).lower()
            if needle not in haystack:
                continue
            total += 1
            if len(matches) < bounded:
                matches.append(_event_summary(event))
        return {"query": query, "total": total, "limit": bounded, "events": matches}

    def commands(self, *, failed_only: bool = False, limit: int | None = None) -> dict[str, Any]:
        bounded = _clamp_limit(limit)
        records: list[dict[str, Any]] = []
        total = 0
        for event in self._events():
            if event.event_type not in ("command", "tool_call", "tool_result"):
                continue
            command = event_command_text(event)
            if not command:
                continue
            payload = event.payload or {}
            exit_code = payload.get("exit_code")
            if not isinstance(exit_code, int):
                exit_code = None
            if failed_only and (exit_code is None or exit_code == 0):
                continue
            total += 1
            if len(records) < bounded:
                records.append(
                    {
                        "event_id": event.event_id,
                        "timestamp": event.timestamp,
                        "command": truncate(command, 600),
                        "exit_code": exit_code,
                    }
                )
        return {"total": total, "limit": bounded, "commands": records}

    # -- repository -------------------------------------------------------

    def _state_root(self, state: str) -> Path:
        if state not in STATES:
            raise ContextError(f"state must be one of {STATES}, got {state!r}")
        path = self.root / "repo" / state
        if not path.is_dir():
            raise ContextError(f"bundle has no repo/{state}")
        return path

    def repo_tree(
        self,
        *,
        state: str,
        path: str = "",
        depth: int = 2,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if depth < 1:
            raise ContextError("depth must be >= 1")
        bounded = _clamp_limit(limit)
        state_root = self._state_root(state)
        start = _safe_path(state_root, path)
        if not start.exists():
            raise ContextError(f"path not found in {state}: {path!r}")
        entries: list[dict[str, Any]] = []
        total = 0
        for candidate in sorted(start.rglob("*")):
            relative = candidate.relative_to(state_root)
            relative_to_start = candidate.relative_to(start)
            if len(relative_to_start.parts) > depth:
                continue
            total += 1
            if len(entries) < bounded:
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "type": "dir" if candidate.is_dir() else "file",
                        "bytes": candidate.stat().st_size if candidate.is_file() else None,
                    }
                )
        return {
            "state": state,
            "path": PurePosixPath(path).as_posix() if path else ".",
            "depth": depth,
            "total": total,
            "limit": bounded,
            "entries": entries,
        }

    def repo_read(
        self,
        *,
        state: str,
        path: str,
        start: int = 1,
        end: int | None = None,
    ) -> dict[str, Any]:
        if start < 1:
            raise ContextError("start must be >= 1")
        state_root = self._state_root(state)
        target = _safe_path(state_root, path)
        if not target.is_file():
            raise ContextError(f"file not found in {state}: {path!r}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ContextError(f"file is not UTF-8 text: {path!r}") from None
        lines = text.splitlines()
        stop = len(lines) if end is None else min(end, len(lines))
        if stop < start:
            window: list[str] = []
        else:
            window = lines[start - 1 : min(stop, start - 1 + MAX_READ_LINES)]
        return {
            "state": state,
            "path": PurePosixPath(path).as_posix(),
            "start": start,
            "end": start + len(window) - 1 if window else start - 1,
            "total_lines": len(lines),
            "truncated": bool(window) and (start - 1 + len(window)) < stop,
            "lines": window,
        }

    def repo_grep(
        self,
        *,
        state: str,
        query: str,
        glob: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if not query:
            raise ContextError("grep query must not be empty")
        bounded = min(_clamp_limit(limit), MAX_GREP_MATCHES)
        state_root = self._state_root(state)
        matches: list[dict[str, Any]] = []
        total = 0
        for candidate in sorted(state_root.rglob("*")):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(state_root).as_posix()
            if glob and not fnmatch.fnmatch(relative, glob):
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                total += 1
                if len(matches) < bounded:
                    matches.append(
                        {"path": relative, "line": number, "text": truncate(line, 400)}
                    )
        return {
            "state": state,
            "query": query,
            "glob": glob,
            "total": total,
            "limit": bounded,
            "matches": matches,
        }

    def repo_diff(self, *, path: str | None = None) -> dict[str, Any]:
        patch_path = self.root / "repo" / "change.patch"
        if not patch_path.is_file():
            raise ContextError("bundle has no repo/change.patch")
        patch = patch_path.read_text(encoding="utf-8")
        sections = _split_patch(patch)
        if path is not None:
            wanted = PurePosixPath(path).as_posix()
            sections = {key: value for key, value in sections.items() if key == wanted}
            if not sections:
                raise ContextError(f"no diff section for path: {path!r}")
        return {
            "paths": sorted(sections),
            "sections": [
                {"path": key, "patch": truncate(sections[key], MAX_TEXT_CHARS)}
                for key in sorted(sections)
            ],
        }

    def git_log(self, *, max_count: int = DEFAULT_LIMIT) -> dict[str, Any]:
        bounded = _clamp_limit(max_count)
        log_path = self.root / "repo" / "git-log.jsonl"
        if not log_path.is_file():
            raise ContextError("bundle has no repo/git-log.jsonl")
        entries: list[dict[str, Any]] = []
        total = 0
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                total += 1
                if len(entries) < bounded:
                    entries.append(json.loads(line))
        return {"total": total, "limit": bounded, "commits": entries}


def _event_summary(event: NormalizedEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp,
        "actor": event.actor,
        "event_type": event.event_type,
        "summary": truncate(event.summary or event_text(event), 400),
    }


def _split_patch(patch: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1].strip() if len(parts) == 2 else line
            sections.setdefault(current, [])
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value) for key, value in sections.items()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retro-context", description=__doc__)
    parser.add_argument("--bundle", default=".", help="path to the source bundle directory")
    sub = parser.add_subparsers(dest="group", required=True)

    sub.add_parser("manifest")

    rollout = sub.add_parser("rollout").add_subparsers(dest="command", required=True)
    listing = rollout.add_parser("list")
    listing.add_argument("--actor")
    listing.add_argument("--type", dest="event_type")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    show = rollout.add_parser("show")
    show.add_argument("event_id")
    search = rollout.add_parser("search")
    search.add_argument("query")
    search.add_argument("--actor")
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    repo = sub.add_parser("repo").add_subparsers(dest="command", required=True)
    tree = repo.add_parser("tree")
    tree.add_argument("--state", required=True, choices=list(STATES))
    tree.add_argument("--path", default="")
    tree.add_argument("--depth", type=int, default=2)
    tree.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    read = repo.add_parser("read")
    read.add_argument("--state", required=True, choices=list(STATES))
    read.add_argument("--path", required=True)
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int)
    grep = repo.add_parser("grep")
    grep.add_argument("--state", required=True, choices=list(STATES))
    grep.add_argument("--query", required=True)
    grep.add_argument("--glob")
    grep.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    diff = repo.add_parser("diff")
    diff.add_argument("--path")

    git = sub.add_parser("git").add_subparsers(dest="command", required=True)
    log = git.add_parser("log")
    log.add_argument("--max-count", dest="max_count", type=int, default=DEFAULT_LIMIT)

    commands = sub.add_parser("commands")
    commands.add_argument("--failed-only", action="store_true")
    commands.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser


def dispatch(context: RetroContext, args: argparse.Namespace) -> dict[str, Any]:
    group = args.group
    command = getattr(args, "command", None)
    if group == "manifest":
        return context.manifest()
    if group == "rollout":
        if command == "list":
            return context.rollout_list(
                actor=args.actor,
                event_type=args.event_type,
                cursor=args.cursor,
                limit=args.limit,
            )
        if command == "show":
            return context.rollout_show(args.event_id)
        if command == "search":
            return context.rollout_search(args.query, actor=args.actor, limit=args.limit)
    if group == "repo":
        if command == "tree":
            return context.repo_tree(
                state=args.state, path=args.path, depth=args.depth, limit=args.limit
            )
        if command == "read":
            return context.repo_read(
                state=args.state, path=args.path, start=args.start, end=args.end
            )
        if command == "grep":
            return context.repo_grep(
                state=args.state, query=args.query, glob=args.glob, limit=args.limit
            )
        if command == "diff":
            return context.repo_diff(path=args.path)
    if group == "git" and command == "log":
        return context.git_log(max_count=args.max_count)
    if group == "commands":
        return context.commands(failed_only=args.failed_only, limit=args.limit)
    raise ContextError(f"unsupported command: {group} {command or ''}".strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        context = RetroContext.open(Path(args.bundle))
        payload = dispatch(context, args)
    except ContextError as error:
        sys.stdout.write(json.dumps({"error": str(error)}, ensure_ascii=False) + "\n")
        return 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry point
    raise SystemExit(main())


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "ContextError",
    "RetroContext",
    "build_parser",
    "dispatch",
    "main",
]
