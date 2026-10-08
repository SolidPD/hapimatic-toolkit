"""
Stall Tracker - Shared State for Stall Detection Hooks

VERSION: 1.0.0

Provides functions to record and query tool failure counts per session.
Used by stall-logger.py (PostToolUseFailure) to record failures and
tool-timeout-enforcer.py (PreToolUse) to detect retry loops.

State is stored in per-session JSON files under ~/.claude/hooks/state/.
Files are named stall-counts-{session_hash}.json where session_hash is
a short hash of the session_id.

This module is completely fail-safe: no exception will ever propagate
to the caller.

State File Format:
    {
        "session_id": "abc123",
        "updated_at": "2026-02-27T12:00:00Z",
        "tools": {
            "mcp__playwright__browser_navigate": {
                "count": 3,
                "last_error": "MCP tool timed out",
                "last_at": "2026-02-27T12:00:00Z"
            }
        }
    }
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

STATE_DIR = Path.home() / ".claude" / "hooks" / "state"

# Stall count threshold before warning about retry loops
RETRY_THRESHOLD = 3


def _session_file(session_id: str) -> Path:
    """Get the state file path for a given session."""
    session_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
    return STATE_DIR / f"stall-counts-{session_hash}.json"


def _read_state(session_id: str) -> dict:
    """Read the state file for a session. Returns empty state if not found."""
    try:
        state_file = _session_file(session_id)
        if state_file.exists():
            with open(state_file) as f:
                return json.load(f)
    except Exception:
        pass
    return {"session_id": session_id, "updated_at": "", "tools": {}}


def _write_state(session_id: str, state: dict) -> None:
    """Write state to the session file. Atomic via temp file + rename."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file = _session_file(session_id)
        tmp_file = state_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(state, f, separators=(",", ":"))
        os.replace(str(tmp_file), str(state_file))
    except Exception:
        pass


def record_stall(session_id: str, tool_name: str, error: str) -> int:
    """Record a tool failure and return the updated count.

    Args:
        session_id: The Claude Code session ID.
        tool_name: The tool that failed (e.g., "mcp__playwright__browser_navigate").
        error: The error message from the failure.

    Returns:
        The updated failure count for this tool in this session.
        Returns 0 if recording failed.
    """
    try:
        state = _read_state(session_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["updated_at"] = now

        if tool_name not in state["tools"]:
            state["tools"][tool_name] = {"count": 0, "last_error": "", "last_at": ""}

        state["tools"][tool_name]["count"] += 1
        state["tools"][tool_name]["last_error"] = error[:200]
        state["tools"][tool_name]["last_at"] = now

        _write_state(session_id, state)
        return state["tools"][tool_name]["count"]
    except Exception:
        return 0


def get_stall_count(session_id: str, tool_name: str) -> int:
    """Get the current failure count for a tool in a session.

    Returns 0 if no failures recorded or on any error.
    """
    try:
        state = _read_state(session_id)
        return state.get("tools", {}).get(tool_name, {}).get("count", 0)
    except Exception:
        return 0


def get_all_stall_counts(session_id: str) -> dict:
    """Get all tool failure counts for a session.

    Returns dict of {tool_name: count} or empty dict on error.
    """
    try:
        state = _read_state(session_id)
        return {
            tool: info.get("count", 0)
            for tool, info in state.get("tools", {}).items()
        }
    except Exception:
        return {}


def record_completion(session_id: str) -> None:
    """Record that a tool call just completed successfully.

    Stores the current UTC timestamp in the session state file under
    the 'completion_at' key. Used by idle-pattern-detector to measure
    gaps between successive tool completions.

    Args:
        session_id: The Claude Code session ID.
    """
    try:
        state = _read_state(session_id)
        state["completion_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _write_state(session_id, state)
    except Exception:
        pass


def get_last_completion_time(session_id: str) -> str | None:
    """Get the timestamp of the last recorded tool completion.

    Returns:
        ISO 8601 UTC timestamp string, or None if no completion recorded.
    """
    try:
        state = _read_state(session_id)
        return state.get("completion_at") or None
    except Exception:
        return None


def cleanup_session(session_id: str) -> None:
    """Remove the state file for a session."""
    try:
        state_file = _session_file(session_id)
        if state_file.exists():
            state_file.unlink()
    except Exception:
        pass
