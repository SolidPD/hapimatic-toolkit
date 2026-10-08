#!/usr/bin/env python3
# HOOK_EVENT: PostToolUseFailure
# HOOK_MATCHER: 
# HOOK_GROUP: failure-logging
"""
Stall Logger - PostToolUseFailure Hook

VERSION: 1.0.1

Logs tool failures (including timeouts and MCP disconnects) to a JSONL file
for pattern analysis. Injects recovery guidance into Claude's context when
timeout or MCP disconnect failures are detected.

Hook Event: PostToolUseFailure
Matcher: "" (all tools)

Input (stdin JSON):
    {
        "session_id": "abc123",
        "tool_name": "mcp__playwright__browser_navigate",
        "tool_input": {"url": "https://..."},
        "error": "MCP tool timed out after 120000ms",
        "is_interrupt": false
    }

Output (stdout JSON, for timeout/disconnect only):
    {
        "hookSpecificOutput": {
            "additionalContext": "STALL DETECTED: ..."
        }
    }

Log File: ~/.claude/logs/stall-events.jsonl
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / ".claude" / "logs"
LOG_FILE = LOG_DIR / "stall-events.jsonl"

MAX_INPUT_SUMMARY_LENGTH = 200
MAX_ERROR_LENGTH = 500

# Patterns for classifying failure types
TIMEOUT_PATTERNS = [
    r"timed?\s*out",
    r"timeout",
    r"exceeded.*time",
    r"deadline\s*exceeded",
    r"sigterm",
]

MCP_DISCONNECT_PATTERNS = [
    r"not\s*connected",
    r"connection\s*refused",
    r"mcp\s*server",
    r"server\s*disconnected",
    r"transport\s*closed",
    r"econnrefused",
    r"epipe",
]


def classify_failure(error: str, is_interrupt: bool) -> str:
    """Classify a tool failure into a category."""
    if is_interrupt:
        return "user_interrupt"

    error_lower = error.lower()

    for pattern in TIMEOUT_PATTERNS:
        if re.search(pattern, error_lower):
            return "timeout"

    for pattern in MCP_DISCONNECT_PATTERNS:
        if re.search(pattern, error_lower):
            return "mcp_disconnect"

    return "other"


def summarize_input(tool_input: dict) -> str:
    """Create a compact summary of tool input for logging."""
    try:
        if not tool_input:
            return ""
        summary = json.dumps(tool_input, separators=(",", ":"))
        if len(summary) > MAX_INPUT_SUMMARY_LENGTH:
            return summary[:MAX_INPUT_SUMMARY_LENGTH] + "..."
        return summary
    except Exception:
        return ""


def compute_fingerprint(tool_name: str, failure_type: str) -> str:
    """Generate a deduplication fingerprint for a failure pattern."""
    key = f"{tool_name}:{failure_type}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def log_event(entry: dict) -> None:
    """Append a JSONL entry to the stall events log."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def build_recovery_context(tool_name: str, failure_type: str, error: str, stall_count: int) -> str:
    """Build recovery guidance context for Claude."""
    lines = []
    lines.append(f"STALL DETECTED: Tool '{tool_name}' failed ({failure_type}).")

    if failure_type == "timeout":
        lines.append("Recovery: (1) Do NOT retry the same call immediately.")
        lines.append("(2) Consider if the operation can be done differently.")
        lines.append("(3) For MCP tools, the server may need restarting.")
    elif failure_type == "mcp_disconnect":
        lines.append("Recovery: (1) The MCP server appears disconnected.")
        lines.append("(2) Do NOT retry — the server needs to be restarted.")
        lines.append("(3) Try an alternative approach that doesn't need this MCP.")

    if stall_count >= 3:
        lines.append(f"WARNING: This tool has failed {stall_count} times this session.")
        lines.append("STOP retrying and inform the user about the recurring failure.")

    return " ".join(lines)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    error = data.get("error", "")
    is_interrupt = data.get("is_interrupt", False)
    session_id = data.get("session_id", "")

    failure_type = classify_failure(error, is_interrupt)

    # Log every failure
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hook": "stall-logger",
        "tool_name": tool_name,
        "failure_type": failure_type,
        "error": error[:MAX_ERROR_LENGTH] if error else None,
        "tool_input_summary": summarize_input(tool_input) or None,
        "fingerprint": compute_fingerprint(tool_name, failure_type),
        "session_id": session_id or None,
    }
    clean_entry = {k: v for k, v in entry.items() if v is not None}
    log_event(clean_entry)

    # Record in stall tracker for cross-hook state
    stall_count = 0
    if session_id and failure_type in ("timeout", "mcp_disconnect"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from lib.telemetry_logger import log_telemetry_event
            log_telemetry_event(__file__, "hook")
        except Exception:
            pass  # telemetry is optional (absent from the public core tier) and must never gate the stall record
        try:
            from lib.stall_tracker import record_stall
            stall_count = record_stall(session_id, tool_name, error)
        except Exception:
            pass

    # Inject recovery context for timeout/disconnect failures
    if failure_type in ("timeout", "mcp_disconnect"):
        context = build_recovery_context(tool_name, failure_type, error, stall_count)
        output = {
            "hookSpecificOutput": {
                "additionalContext": context,
            }
        }
        print(json.dumps(output))

    sys.exit(0)


if __name__ == "__main__":
    main()
