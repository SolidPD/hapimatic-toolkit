#!/usr/bin/env python3
# HOOK_VERSION: 1.0.0
# HOOK_EVENT: PostToolUse
# HOOK_MATCHER: Edit|Write
# HOOK_GROUP: lint
"""
PostToolUse hook: Run linting after Edit/Write operations on source files.

Provides immediate lint feedback to Claude after file modifications, enabling
self-correction before code is committed.

BEHAVIOR:
---------
1. Triggers on Edit/Write tool calls (matched by settings.json "Edit|Write")
2. Filters by file extension (.py, .ts, .tsx, .js, .jsx, .mjs, .cjs)
3. Detects project type by walking up from file to find config markers
4. Runs appropriate linter (ruff for Python, ESLint for JS/TS)
5. Feeds lint errors back to Claude via decision:"block" + reason JSON
6. Silent pass (exit 0, no output) when clean or no linter available

LINTER SELECTION:
-----------------
- Python (.py): ruff check <file>
- JS/TS with bun.lock: bun x eslint <file>
- JS/TS with eslint config: npx eslint <file>
- No config found: silent pass
"""

import json
import os
import subprocess
import sys
from pathlib import Path

LINT_TIMEOUT = 10  # seconds

# File extensions that trigger linting
PYTHON_EXTENSIONS = {".py"}
JS_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
ALL_LINT_EXTENSIONS = PYTHON_EXTENSIONS | JS_TS_EXTENSIONS

# Project root markers for JS/TS linting
ESLINT_CONFIG_PATTERNS = [
    "eslint.config.mjs",
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.json",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".eslintrc",
]

# Max directory levels to walk up when searching for project root
MAX_WALK_DEPTH = 10


def find_project_root(file_path: str) -> tuple[Path | None, bool]:
    """
    Walk up from file_path to find the JS/TS project root.

    Returns:
        (project_root, has_bun_lock) — project_root is None if no eslint config found.
    """
    current = Path(file_path).resolve().parent
    for _ in range(MAX_WALK_DEPTH):
        has_bun_lock = (current / "bun.lock").exists()
        has_eslint = any((current / cfg).exists() for cfg in ESLINT_CONFIG_PATTERNS)

        if has_eslint:
            return current, has_bun_lock

        parent = current.parent
        if parent == current:
            break  # reached filesystem root
        current = parent

    return None, False


def run_ruff(file_path: str) -> str | None:
    """Run ruff check on a Python file. Returns lint output or None if clean."""
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-fix", file_path],
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT,
        )
        if result.returncode != 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # ruff not installed or timed out — silent pass
    return None


def run_eslint(file_path: str, project_root: Path, use_bun: bool) -> str | None:
    """Run ESLint on a JS/TS file. Returns lint output or None if clean."""
    if use_bun:
        cmd = ["bun", "x", "eslint", "--no-error-on-unmatched-pattern", file_path]
    else:
        cmd = ["npx", "eslint", "--no-error-on-unmatched-pattern", file_path]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT,
            cwd=str(project_root),
        )
        if result.returncode != 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # linter not available or timed out — silent pass
    return None


def main():
    # Telemetry: fire-and-forget usage event
    try:
        import os as _tos
        _tdir = _tos.path.dirname(_tos.path.abspath(__file__))
        import sys as _tsys
        if _tdir not in _tsys.path:
            _tsys.path.insert(0, _tdir)
        from lib.telemetry_logger import log_telemetry_event
        log_telemetry_event(__file__, "hook")
    except Exception:
        pass

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Only process Edit and Write
    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Check file extension
    ext = Path(file_path).suffix.lower()
    if ext not in ALL_LINT_EXTENSIONS:
        sys.exit(0)

    # Run appropriate linter
    lint_output = None

    if ext in PYTHON_EXTENSIONS:
        lint_output = run_ruff(file_path)
    elif ext in JS_TS_EXTENSIONS:
        project_root, has_bun_lock = find_project_root(file_path)
        if project_root:
            lint_output = run_eslint(file_path, project_root, has_bun_lock)
        # No project root found → silent pass (no eslint config)

    # Report lint errors back to Claude
    if lint_output:
        result = {
            "decision": "block",
            "reason": f"Lint errors in {file_path}:\n{lint_output}",
        }
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
