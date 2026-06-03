"""Generalized helper for headless interaction with Codex."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def call_codex_headless(
    prompt: str,
    schema: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    timeout: int = 900,
    capture_path: Path | None = None,
) -> dict[str, Any] | str:
    """Call the `codex` executable headlessly using stdin.

    If a JSON schema is provided, runs `codex exec --json` with the schema
    and returns the parsed JSON response. Otherwise, runs `codex exec` and
    returns the raw text output.

    If capture_path is provided, writes the raw stdout and stderr to that file.
    """
    with tempfile.TemporaryDirectory(prefix="retro-codex-headless-") as td:
        tmp = Path(td)
        cmd = [
            "codex",
            "-a",
            "never",
            "exec",
        ]

        if schema:
            schema_path = tmp / "schema.json"
            response_path = tmp / "response.json"
            schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
            cmd.extend([
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "-o",
                str(response_path),
                "-",
            ])
        else:
            cmd.extend([
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-",
            ])

        if cwd:
            cwd_path = Path(cwd)
            if cwd_path.exists():
                cmd[3:3] = ["-C", str(cwd_path)]

        proc = None
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("`codex` executable was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            if capture_path:
                _write_headless_capture(capture_path, exc.stdout or "", exc.stderr or "")
            raise RuntimeError("codex headless execution timed out") from exc

        if capture_path:
            _write_headless_capture(capture_path, proc.stdout, proc.stderr)

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex headless execution failed with exit {proc.returncode}"
            )

        if schema:
            if not response_path.exists():
                raise RuntimeError("codex headless execution produced no final response file")
            try:
                return json.loads(response_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError("codex headless final response was not valid JSON") from exc
        else:
            return proc.stdout


def _write_headless_capture(path: Path, stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(stdout, bytes):
        stdout_text = stdout.decode("utf-8", errors="replace")
    else:
        stdout_text = stdout or ""
    if isinstance(stderr, bytes):
        stderr_text = stderr.decode("utf-8", errors="replace")
    else:
        stderr_text = stderr or ""
    text = stdout_text
    if stderr_text.strip():
        text += "\n" + json.dumps({"type": "stderr", "text": stderr_text}, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
