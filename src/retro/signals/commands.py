"""Shell-command understanding shared by the profile builder and `retro analyze`.

Classification is base-command-first: resolve what program actually runs
(`.venv/bin/pytest`, `python -m pytest`, `npx tsc`, `uv run pytest` all resolve
to the tool), then refine with the sub-command. Matching the raw string instead
would score `rg 'pytest'` as a test run.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from typing import Any

from ..schema import NormalizedEvent
from ..utils import event_tool_input

TEST = "test"
TYPECHECK = "typecheck"
LINT = "lint"
BUILD = "build"
VCS = "vcs"
PACKAGE = "package"
SEARCH = "search"
INSPECT = "inspect"
NET = "net"
OPS = "ops"
RUN = "run"
OTHER = "other"

VALIDATION_CATEGORIES = frozenset({TEST, TYPECHECK, LINT, BUILD})
STRONG_VALIDATION = frozenset({TEST, TYPECHECK})

_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(?:[^\s]*)\s+")
_SEGMENT_RE = re.compile(r"&&|\|\||;|\|(?!\|)|\n")
_WRAPPERS = ("sudo", "env", "time", "nohup", "xargs", "command", "exec", "nice", "stdbuf")
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "rye", "pdm", "bundle", "pnpm", "yarn", "bun", "npm"}

_TEST_BASES = {
    "pytest",
    "py.test",
    "unittest",
    "nosetests",
    "tox",
    "nox",
    "jest",
    "vitest",
    "mocha",
    "ava",
    "karma",
    "rspec",
    "phpunit",
    "ctest",
    "rake",
    "behave",
    "playwright",
    "cypress",
    "testthat",
    "deno",
}
_TYPECHECK_BASES = {"mypy", "dmypy", "pyright", "pyre", "tsc", "flow", "ty", "ruff-check-types"}
_LINT_BASES = {
    "ruff",
    "flake8",
    "pylint",
    "eslint",
    "biome",
    "oxlint",
    "black",
    "isort",
    "prettier",
    "shellcheck",
    "rubocop",
    "swiftlint",
    "markdownlint",
    "yamllint",
    "golangci-lint",
    "gofmt",
    "clippy-driver",
    "pre-commit",
    "cpplint",
    "hadolint",
}
_BUILD_BASES = {"cmake", "ninja", "bazel", "buck", "tsup", "vite", "webpack", "rollup", "esbuild", "swc"}
_SEARCH_BASES = {"rg", "grep", "egrep", "fgrep", "ag", "ack", "fd", "find", "ripgrep"}
_INSPECT_BASES = {
    "ls",
    "cat",
    "head",
    "tail",
    "pwd",
    "wc",
    "sed",
    "awk",
    "file",
    "stat",
    "tree",
    "jq",
    "yq",
    "du",
    "df",
    "echo",
    "printf",
    "which",
    "whereis",
    "realpath",
    "basename",
    "dirname",
    "diff",
    "cut",
    "sort",
    "uniq",
    "xxd",
    "nl",
    "less",
    "more",
    "type",
}
_NET_BASES = {"curl", "wget", "http", "httpie", "ping", "nc", "ssh", "scp", "rsync", "sftp"}
_OPS_BASES = {
    "docker",
    "docker-compose",
    "podman",
    "kubectl",
    "helm",
    "terraform",
    "aws",
    "gcloud",
    "az",
    "launchctl",
    "systemctl",
    "brew",
    "apt",
    "apt-get",
    "yum",
    "dnf",
    "pacman",
    "service",
    "crontab",
    "supervisorctl",
    "flyctl",
    "vercel",
    "heroku",
}
_PACKAGE_BASES = {"pip", "pip3", "pipx", "conda", "mamba", "gem", "composer", "nuget"}
_VCS_BASES = {"git", "gh", "hg", "svn", "jj", "glab"}

_SCRIPT_KEYWORDS: Sequence[tuple[str, str]] = (
    ("typecheck", TYPECHECK),
    ("type-check", TYPECHECK),
    ("tsc", TYPECHECK),
    ("types", TYPECHECK),
    ("test", TEST),
    ("spec", TEST),
    ("e2e", TEST),
    ("coverage", TEST),
    ("lint", LINT),
    ("format", LINT),
    ("fmt", LINT),
    ("check", LINT),
    ("build", BUILD),
    ("compile", BUILD),
    ("bundle", BUILD),
    ("dist", BUILD),
)

_SUBCOMMANDS: dict[str, dict[str, str]] = {
    "cargo": {
        "test": TEST,
        "nextest": TEST,
        "clippy": LINT,
        "fmt": LINT,
        "build": BUILD,
        "check": BUILD,
        "run": RUN,
        "add": PACKAGE,
        "install": PACKAGE,
        "bench": TEST,
        "doc": BUILD,
        "publish": OPS,
    },
    "go": {
        "test": TEST,
        "build": BUILD,
        "vet": LINT,
        "run": RUN,
        "mod": PACKAGE,
        "install": PACKAGE,
        "fmt": LINT,
    },
    "dotnet": {"test": TEST, "build": BUILD, "run": RUN, "restore": PACKAGE, "publish": OPS},
    "mvn": {"test": TEST, "verify": TEST, "package": BUILD, "compile": BUILD, "install": BUILD},
    "gradle": {"test": TEST, "check": TEST, "build": BUILD, "assemble": BUILD},
    "./gradlew": {"test": TEST, "check": TEST, "build": BUILD, "assemble": BUILD},
    "swift": {"test": TEST, "build": BUILD, "run": RUN},
    "deno": {"test": TEST, "lint": LINT, "fmt": LINT, "check": TYPECHECK, "run": RUN},
    "docker": {"build": BUILD, "compose": OPS, "run": RUN, "push": OPS, "pull": PACKAGE},
    "pip": {"install": PACKAGE, "uninstall": PACKAGE, "freeze": INSPECT, "list": INSPECT},
}

_DESTRUCTIVE_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "recursive_delete",
        re.compile(r"\brm\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*[rR][A-Za-z]*f|\brm\s+-[A-Za-z]*f[A-Za-z]*[rR]"),
    ),
    ("hard_reset", re.compile(r"\bgit\s+reset\s+--hard\b")),
    ("git_clean", re.compile(r"\bgit\s+clean\s+-[A-Za-z]*[dfx]")),
    ("force_push", re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?!-with-lease)|(?<!\w)-f(?!\w))")),
    ("checkout_discard", re.compile(r"\bgit\s+checkout\s+(?:--\s+\.|\.\s*$)")),
    ("branch_delete", re.compile(r"\bgit\s+branch\s+-D\b")),
    ("drop_database", re.compile(r"(?i)\b(?:drop|truncate)\s+(?:table|database|schema)\b")),
    ("disk_write", re.compile(r"\b(?:dd\s+if=|mkfs(?:\.\w+)?\b|diskutil\s+erase)")),
    ("chmod_world", re.compile(r"\bchmod\s+(?:-R\s+)?777\b")),
    ("prune_all", re.compile(r"\bdocker\s+system\s+prune\b[^\n]*-a")),
    ("publish", re.compile(r"\b(?:npm|cargo|poetry|twine)\s+publish\b|\btwine\s+upload\b")),
)

_GIT_COMMIT_RE = re.compile(r"\bgit\s+(?:(?:--?[A-Za-z][A-Za-z-]*)(?:[= ]\S+)?\s+){0,3}commit(?!-tree)\b")
_COMMIT_NOOP_RE = re.compile(r"(?i)nothing to commit|no changes added to commit")


def get_base_command(command: str) -> str:
    """Resolve the program a command line actually runs."""
    text = (command or "").strip()
    if not text:
        return ""
    for _ in range(8):
        stripped = _ENV_ASSIGN_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped.strip()
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return ""

    while parts and parts[0] in _WRAPPERS:
        parts = parts[1:]
        while parts and "=" in parts[0] and not parts[0].startswith(("-", "/", ".")):
            parts = parts[1:]
    if not parts:
        return ""

    exe = parts[0].replace("\\", "/")
    if exe.startswith("./gradlew") or exe.endswith("/gradlew"):
        return "./gradlew"
    if "/" in exe:
        exe = exe.rsplit("/", 1)[-1]

    if exe in ("python", "python3", "pythonw", "py") or re.fullmatch(r"python3\.\d+", exe):
        if len(parts) > 2 and parts[1] == "-m":
            return parts[2].rsplit("/", 1)[-1]
        if len(parts) > 1 and not parts[1].startswith("-"):
            return parts[1].replace("\\", "/").rsplit("/", 1)[-1]
        return exe
    if exe in ("npx", "bunx", "pnpx", "dlx") and len(parts) > 1:
        return _skip_flags(parts[1:])
    if exe in _RUNNERS and len(parts) > 2 and parts[1] in ("run", "exec"):
        nxt = _skip_flags(parts[2:])
        if exe in ("npm", "pnpm", "yarn", "bun") and parts[1] == "run":
            return f"{exe} run {nxt}" if nxt else exe
        return nxt or exe
    if exe == "poetry" and len(parts) > 2 and parts[1] == "run":
        return _skip_flags(parts[2:]) or exe
    if exe in ("npm", "pnpm", "yarn", "bun") and len(parts) > 1:
        return f"{exe} {_skip_flags(parts[1:])}".strip()
    if exe == "make":
        target = _skip_flags(parts[1:])
        return f"make {target}".strip() if target else "make"
    return exe


def _skip_flags(parts: Sequence[str]) -> str:
    for part in parts:
        if not part.startswith("-"):
            return part.replace("\\", "/").rsplit("/", 1)[-1]
    return ""


def split_segments(command: str) -> list[str]:
    """Split a command line into independently classifiable segments."""
    text = (command or "").strip()
    if not text:
        return []
    heredoc = re.split(r"<<-?\s*['\"]?EOF", text, maxsplit=1)
    text = heredoc[0] if len(heredoc) > 1 else text
    segments = []
    for raw in _SEGMENT_RE.split(text):
        segment = raw.strip().strip("()").strip()
        if segment:
            segments.append(segment)
    return segments or [text]


def segment_bases(command: str) -> list[str]:
    """Base programs of every segment, so `cd x && pytest` reports `pytest`."""
    bases = []
    for segment in split_segments(command):
        base = get_base_command(segment)
        head = base.split()[0] if base.split() else ""
        if not base or head in ("cd", "export", "set", "source", "."):
            continue
        if base not in bases:
            bases.append(base)
    return bases


def classify_command(command: str) -> set[str]:
    """Return every category present in a (possibly compound) command line."""
    categories: set[str] = set()
    for segment in split_segments(command):
        categories.add(_classify_segment(segment))
    categories.discard(OTHER)
    return categories or {OTHER}


def _classify_segment(segment: str) -> str:
    base = get_base_command(segment)
    words = base.split()
    if not words:
        return OTHER
    if base.startswith(("npm ", "pnpm ", "yarn ", "bun ")):
        return _classify_node_script(base, segment)
    if base.startswith("make"):
        return _script_category(base[5:].strip()) or BUILD
    head = words[0]
    if head in _SUBCOMMANDS:
        return _classify_subcommand(head, segment)
    if head == "cd":
        return INSPECT
    if head in _SEARCH_BASES:
        return SEARCH
    if head in _TEST_BASES:
        return TEST
    if head in _TYPECHECK_BASES:
        return TYPECHECK
    if head in _LINT_BASES:
        return LINT
    if head in _BUILD_BASES:
        return BUILD
    if head in _VCS_BASES:
        return VCS
    if head in _PACKAGE_BASES:
        return PACKAGE
    if head in _OPS_BASES:
        return OPS
    if head in _NET_BASES:
        return NET
    if head in _INSPECT_BASES:
        return INSPECT
    if head in ("node", "python", "python3", "ruby", "bash", "sh", "zsh", "uvicorn", "flask"):
        return RUN
    return OTHER


def _classify_node_script(base: str, segment: str) -> str:
    parts = base.split()
    if len(parts) >= 3 and parts[1] == "run":
        return _script_category(parts[2]) or RUN
    sub = parts[1] if len(parts) > 1 else ""
    if sub in ("test", "t"):
        return TEST
    if sub in ("install", "i", "ci", "add", "remove", "update", "link"):
        return PACKAGE
    if sub in ("publish", "pack"):
        return OPS
    if sub in ("build",):
        return BUILD
    if sub in ("lint", "format"):
        return LINT
    if sub in ("exec", "dlx"):
        return _classify_segment(segment.split(sub, 1)[-1])
    return RUN


def _classify_subcommand(head: str, segment: str) -> str:
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    sub = ""
    for part in parts[1:]:
        if not part.startswith("-"):
            sub = part
            break
    mapping = _SUBCOMMANDS.get(head, {})
    if sub in mapping:
        return mapping[sub]
    if head == "docker":
        return OPS
    if head == "pip":
        return PACKAGE
    return BUILD if head in ("mvn", "gradle", "./gradlew") else OTHER


def _script_category(script: str) -> str | None:
    name = (script or "").lower()
    if not name:
        return None
    for keyword, category in _SCRIPT_KEYWORDS:
        if keyword in name:
            return category
    return None


def destructive_kinds(command: str) -> set[str]:
    return {name for name, pattern in _DESTRUCTIVE_PATTERNS if pattern.search(command or "")}


def is_git_commit(command: str) -> bool:
    text = command or ""
    if not _GIT_COMMIT_RE.search(text):
        return False
    return "--dry-run" not in text


def commit_was_noop(output: str) -> bool:
    return bool(_COMMIT_NOOP_RE.search(output or ""))


def command_text(event: NormalizedEvent) -> str:
    """Extract the command line from a normalized call event, any host."""
    payload = event.payload or {}
    if not isinstance(payload, dict):
        return ""
    for container in (
        payload,
        payload.get("arguments"),
        payload.get("input"),
        event_tool_input(event),
    ):
        text = _command_from_container(container)
        if text:
            return text
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        parsed = _maybe_json(arguments)
        text = _command_from_container(parsed)
        if text:
            return text
        if event.event_type == "command" and parsed is None:
            return arguments
    return ""


def _command_from_container(container: Any) -> str:
    if not isinstance(container, dict):
        return ""
    for key in ("command", "cmd", "command_line", "commandLine", "script"):
        value = container.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            parts = [str(item) for item in value if isinstance(item, (str, int, float))]
            if len(parts) >= 3 and parts[0] in ("bash", "sh", "zsh") and parts[1] in ("-lc", "-c"):
                return parts[2]
            if parts:
                return " ".join(parts)
    return ""


def _maybe_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        import json

        return json.loads(stripped)
    except ValueError:
        return None


def iter_command_categories(commands: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for command in commands:
        out |= classify_command(command)
    return out
