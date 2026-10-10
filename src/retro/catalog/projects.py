"""Git identity and cross-surface project consolidation."""
from __future__ import annotations

import configparser
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..config import RetroConfig
from .metadata import HostMetadata


@dataclass
class GitDiscovery:
    remote_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    root: str | None = None
    worktree: bool | None = None


def resolve_cwd(cwd: str | None) -> str | None:
    if not cwd or "://" in cwd:
        return cwd
    expanded = os.path.expandvars(os.path.expanduser(cwd))
    try:
        return str(Path(expanded).resolve(strict=False))
    except OSError:
        return str(Path(expanded).absolute())


def normalize_remote(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().rstrip("/")
    if not raw:
        return None
    if raw.startswith("path:"):
        return None
    if re.fullmatch(r"[^/@:\s]+/[^/\s]+(?:\.git)?", raw):
        path = raw.removesuffix(".git")
        return f"github.com/{path}".casefold()
    scp = re.match(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^\\s]+)$", raw)
    if scp and "://" not in raw:
        host = scp.group("host")
        path = scp.group("path")
    else:
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlparse(candidate)
        host = parsed.hostname or ""
        path = unquote(parsed.path).lstrip("/")
    path = path.rstrip("/").removesuffix(".git")
    parts = [part for part in path.split("/") if part]
    if not host or len(parts) < 2:
        return None
    return f"{host}/{'/'.join(parts)}".casefold()


def discover_git(cwd_resolved: str | None) -> GitDiscovery:
    if not cwd_resolved or "://" in cwd_resolved:
        return GitDiscovery()
    start = Path(cwd_resolved)
    if not start.exists():
        return GitDiscovery()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        dotgit = candidate / ".git"
        if dotgit.is_dir():
            return _git_from_dirs(candidate, dotgit, dotgit, False)
        if dotgit.is_file():
            try:
                marker = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not marker.casefold().startswith("gitdir:"):
                continue
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (candidate / git_dir).resolve(strict=False)
            common_dir = git_dir
            commondir = git_dir / "commondir"
            if commondir.is_file():
                try:
                    raw_common = commondir.read_text(encoding="utf-8").strip()
                    common_dir = (git_dir / raw_common).resolve(strict=False)
                except OSError:
                    common_dir = git_dir
            main_root = common_dir.parent if common_dir.name == ".git" else candidate
            return _git_from_dirs(main_root, git_dir, common_dir, True)
    return GitDiscovery()


def git_input_signature(cwd_resolved: str | None) -> str:
    parts = [cwd_resolved or ""]
    if not cwd_resolved or "://" in cwd_resolved:
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()
    start = Path(cwd_resolved)
    try:
        parts.append(f"cwd:{int(start.exists())}")
    except OSError:
        parts.append("cwd:error")
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()
    if not start.exists():
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        dotgit = candidate / ".git"
        if not dotgit.exists():
            continue
        if dotgit.is_file():
            _append_stat(parts, dotgit)
        else:
            parts.append(f"{dotgit}:directory")
        git_dir = dotgit
        common_dir = dotgit
        if dotgit.is_file():
            marker = _read_text(dotgit)
            if marker and marker.casefold().startswith("gitdir:"):
                git_dir = Path(marker.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (candidate / git_dir).resolve(strict=False)
                common_dir = git_dir
                commondir = git_dir / "commondir"
                _append_stat(parts, commondir)
                raw_common = _read_text(commondir)
                if raw_common:
                    common_dir = (git_dir / raw_common).resolve(strict=False)
        for path in (git_dir / "config", common_dir / "config"):
            _append_stat(parts, path)
        break
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def resolve_git_and_project(
    metadata: HostMetadata,
    *,
    cwd_resolved: str | None,
    config: RetroConfig,
    marker_project: str | None,
    annotation_project: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    slug_path = (
        decode_claude_slug(metadata.project_slug)
        if metadata.project_slug
        else None
    )
    project_path = cwd_resolved or slug_path
    discovered = discover_git(project_path)
    recorded_remote = normalize_remote(metadata.remote_url)
    disk_remote = normalize_remote(discovered.remote_url)
    remote = disk_remote or recorded_remote
    remote_url = discovered.remote_url or metadata.remote_url
    git = {
        "remote": remote,
        "remote_url": remote_url,
        "branch": metadata.branch or discovered.branch,
        "commit": metadata.commit or discovered.commit,
        "root": discovered.root,
        "worktree": discovered.worktree,
    }

    source = "unknown"
    evidence: str | None = None
    project_id = "unknown"
    if annotation_project:
        project_id = _canonical_project_value(annotation_project, config)
        source = "annotation"
        evidence = f"manual project {annotation_project}"
    elif marker_project:
        project_id = _canonical_project_value(marker_project, config)
        source = "marker"
        evidence = f"#retro:project={marker_project}"
    else:
        configured = _configured_path_project(metadata.cwd, project_path, config)
        if configured is not None:
            project_id, pattern = configured
            source = "config"
            evidence = f"path matched {pattern}"
        elif remote:
            project_id = remote
            source = "git-remote"
            evidence = (
                f"repository config remote {discovered.remote_url}"
                if disk_remote
                else f"recorded remote {metadata.remote_url}"
            )
        elif discovered.root:
            project_id = f"path:{discovered.root}"
            source = "git-root"
            evidence = f"Git root {discovered.root}"
        else:
            heuristic = _worktree_project(project_path)
            if heuristic is not None:
                project_id, evidence = heuristic
                source = "worktree-pattern"
            elif cwd_resolved and "://" not in cwd_resolved:
                project_id = f"path:{cwd_resolved}"
                source = "path"
                evidence = f"resolved cwd {cwd_resolved}"
            elif slug_path:
                project_id = f"path:{slug_path}"
                if metadata.project_slug:
                    source = "claude-slug"
                    evidence = f"decoded project slug {metadata.project_slug}"

    aliased = _resolve_alias(project_id, config.projects.get("aliases", {}))
    if aliased != project_id:
        evidence = f"{evidence or project_id}; alias {project_id} -> {aliased}"
        project_id = aliased
        source = "config"
    names = config.projects.get("names", {})
    configured_name = names.get(project_id) if isinstance(names, dict) else None
    name = configured_name or _project_name(project_id)
    return git, {
        "id": project_id,
        "name": name,
        "source": source,
        "evidence": evidence,
    }


def path_glob_matches(pattern: str, value: str) -> bool:
    expanded = os.path.expandvars(os.path.expanduser(pattern))
    candidates = [value, os.path.expandvars(os.path.expanduser(value))]
    try:
        candidates.append(str(Path(candidates[-1]).resolve(strict=False)))
        expanded_resolved = str(Path(expanded).resolve(strict=False))
    except OSError:
        expanded_resolved = expanded
    expressions = {_glob_regex(expanded), _glob_regex(expanded_resolved)}
    return any(expression.fullmatch(candidate) for expression in expressions for candidate in candidates)


def _git_from_dirs(
    main_root: Path,
    git_dir: Path,
    common_dir: Path,
    worktree: bool,
) -> GitDiscovery:
    config_path = common_dir / "config"
    if not config_path.is_file():
        config_path = git_dir / "config"
    remote_url = _config_remote(config_path)
    head = _read_text(git_dir / "HEAD")
    branch: str | None = None
    commit: str | None = None
    if head:
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            prefix = "refs/heads/"
            branch = ref[len(prefix) :] if ref.startswith(prefix) else ref
            commit = _read_ref(common_dir, ref)
        elif re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            commit = head
    return GitDiscovery(
        remote_url=remote_url,
        branch=branch,
        commit=commit,
        root=str(main_root.resolve(strict=False)),
        worktree=worktree,
    )


def _config_remote(path: Path) -> str | None:
    if not path.is_file():
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    sections = [section for section in parser.sections() if section.startswith('remote "')]
    sections.sort(key=lambda section: (section != 'remote "origin"', section))
    for section in sections:
        try:
            value = parser.get(section, "url")
        except (configparser.Error, KeyError):
            continue
        if value.strip():
            return value.strip()
    return None


def _read_ref(common_dir: Path, ref: str) -> str | None:
    direct = _read_text(common_dir / ref)
    if direct:
        return direct
    packed = common_dir / "packed-refs"
    if not packed.is_file():
        return None
    try:
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    except OSError:
        return None
    return None


def _configured_path_project(
    cwd: str | None,
    cwd_resolved: str | None,
    config: RetroConfig,
) -> tuple[str, str] | None:
    entries = config.projects.get("paths", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("glob")
        project = entry.get("project")
        if not isinstance(pattern, str) or not isinstance(project, str):
            continue
        if any(
            value and path_glob_matches(pattern, value)
            for value in (cwd, cwd_resolved)
        ):
            return _canonical_project_value(project, config), pattern
    return None


def _canonical_project_value(value: str, config: RetroConfig) -> str:
    if value.startswith("path:"):
        return value
    names = config.projects.get("names", {})
    if isinstance(names, dict):
        for project_id, name in names.items():
            if isinstance(name, str) and name.casefold() == value.casefold():
                return str(project_id)
    normalized = normalize_remote(value)
    return normalized or value


def _resolve_alias(project_id: str, aliases: Any) -> str:
    if not isinstance(aliases, dict):
        return project_id
    current = project_id
    seen: set[str] = set()
    folded = {str(key).casefold(): str(value) for key, value in aliases.items()}
    while current not in seen:
        seen.add(current)
        next_value = aliases.get(current)
        if next_value is None:
            next_value = folded.get(current.casefold())
        if not isinstance(next_value, str):
            break
        current = next_value
    return current


def _worktree_project(cwd_resolved: str | None) -> tuple[str, str] | None:
    if not cwd_resolved or "://" in cwd_resolved:
        return None
    path = Path(cwd_resolved)
    parts = path.parts
    if len(parts) >= 3 and parts[-1] == "scratchpad":
        slug = parts[-3]
        scratch = re.match(
            r"^-Users-(?P<user>[^-]+)-Dev-repos-(?P<repo>.+)$",
            slug,
        )
        if scratch:
            repo_name = scratch.group("repo").split("-worktrees-", 1)[0]
            candidate = Path(
                "/Users",
                scratch.group("user"),
                "Dev",
                "repos",
                repo_name,
            ).resolve(strict=False)
            git = discover_git(str(candidate))
            remote = normalize_remote(git.remote_url)
            if remote:
                return remote, f"Claude scratch path {cwd_resolved}; source repo {candidate}"
            return f"path:{candidate}", f"Claude scratch path {cwd_resolved}"
    for index, part in enumerate(parts):
        if not part.endswith(".worktrees"):
            continue
        base_name = part[: -len(".worktrees")]
        parent = Path(*parts[:index]) if index else Path(path.anchor)
        candidate = (parent / base_name).resolve(strict=False)
        git = discover_git(str(candidate))
        remote = normalize_remote(git.remote_url)
        if remote:
            return remote, f"worktree path {cwd_resolved}; main repo remote {git.remote_url}"
        return f"path:{candidate}", f"worktree path {cwd_resolved}"
    codex = re.search(r"/\\.codex/worktrees/[^/]+/([^/]+)", cwd_resolved)
    if codex:
        return f"path:{cwd_resolved}", f"Codex worktree path for {codex.group(1)}"
    return None


def decode_claude_slug(value: str) -> str | None:
    if not value.startswith("-"):
        return None
    user_repo = re.match(r"^-Users-(?P<user>[^-]+)-Dev-repos-(?P<repo>.+)$", value)
    if user_repo:
        return resolve_cwd(
            f"/Users/{user_repo.group('user')}/Dev/repos/{user_repo.group('repo')}"
        )
    ssd_repo = re.match(r"^-Volumes-SSD-Dev-repos-(?P<repo>.+)$", value)
    if ssd_repo:
        return resolve_cwd(f"/Volumes/SSD/Dev/repos/{ssd_repo.group('repo')}")
    pieces = [piece for piece in value.split("-") if piece]
    if not pieces:
        return None
    candidate = "/" + "/".join(pieces)
    return resolve_cwd(candidate)


def _project_name(project_id: str) -> str:
    if project_id == "unknown":
        return "Unknown"
    value = project_id[5:] if project_id.startswith("path:") else project_id
    name = Path(value.rstrip("/")).name
    return name or value


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if (
            char == "/"
            and index + 2 < len(pattern)
            and pattern[index + 1 : index + 3] == "**"
        ):
            after = index + 3
            if after == len(pattern):
                out += "(?:/.*)?"
                index = after
            elif pattern[after] == "/":
                out += "/(?:.*/)?"
                index = after + 1
            else:
                out += re.escape(char)
                index += 1
        elif char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                out += ".*"
                index += 2
            else:
                out += "[^/]*"
                index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(out)


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def _append_stat(parts: list[str], path: Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        parts.append(f"{path}:missing")
    else:
        parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
