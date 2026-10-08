#!/usr/bin/env python3
# HOOK_VERSION: 1.0.2
# HOOK_EVENT: PostToolUse
# HOOK_MATCHER:
# HOOK_GROUP: posttool-global
"""
Idle Pattern Detector - PostToolUse Hook

VERSION: 1.0.2

Detects idle gaps between successive tool completions. When the gap exceeds
the threshold (90 seconds), logs the event and injects an advisory into
Claude's context reminding it to check if it's passively waiting.

Since Claude Code hooks only fire on events (not on inactivity), true
real-time idle detection is impossible. Instead, this hook detects idle
gaps retroactively: each PostToolUse call checks how long ago the previous
tool completed. If the gap is abnormally large, it indicates Claude was
idle between the two calls.

Hook Event: PostToolUse
Matcher: "" (all tools)

Input (stdin JSON):
    {
        "session_id": "abc123",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": "file1.txt\nfile2.txt"
    }

NOTE: the PostToolUse runtime sends `tool_response`, NEVER `tool_result` (#1398) —
the field above is illustrative (this detector keys only on session_id/tool_name),
but the wrong key must not be propagated to future hook authors who copy this shape.

Output (stdout JSON, only when idle gap detected):
    {
        "hookSpecificOutput": {
            "additionalContext": "IDLE GAP DETECTED: ..."
        }
    }

Log File: ~/.claude/logs/idle-events.jsonl

Reference: rp1-home#255
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

IDLE_THRESHOLD_SECONDS = 90

LOG_DIR = Path.home() / ".claude" / "logs"
LOG_FILE = LOG_DIR / "idle-events.jsonl"


def log_idle_event(entry: dict) -> None:
    """Append a JSONL entry to the idle events log."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    tool_name = data.get("tool_name", "")

    if not session_id:
        sys.exit(0)

    # Import stall_tracker for completion time tracking
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from lib.telemetry_logger import log_telemetry_event
        log_telemetry_event(__file__, "hook")
    except Exception:
        pass  # telemetry is optional (absent from the public core tier) and must never gate the idle check
    try:
        from lib.stall_tracker import get_last_completion_time, record_completion
    except Exception:
        sys.exit(0)

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Check time since last completion
    last_completion = get_last_completion_time(session_id)

    # Always record current completion for next check
    record_completion(session_id)

    # Fast path: no previous completion recorded
    if not last_completion:
        sys.exit(0)

    # Calculate gap
    try:
        last_time = datetime.strptime(last_completion, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        gap_seconds = (now - last_time).total_seconds()
    except (ValueError, TypeError):
        sys.exit(0)

    if gap_seconds < IDLE_THRESHOLD_SECONDS:
        sys.exit(0)

    # Idle gap detected — log and inject advisory
    gap_minutes = gap_seconds / 60

    log_idle_event(
        {
            "timestamp": now_str,
            "hook": "idle-pattern-detector",
            "session_id": session_id,
            "tool_name": tool_name,
            "gap_seconds": round(gap_seconds, 1),
            "last_completion": last_completion,
        }
    )

    context = (
        f"IDLE GAP DETECTED: {gap_minutes:.1f} minutes elapsed between tool calls. "
        f"This suggests you may have been passively waiting. "
        f"Self-check: Were you stuck waiting for a background agent, deferred operation, "
        f"or callback? If so, take action now — check status, try an alternative approach, "
        f"or inform the user. See RULES.md: 'Passive Waiting Prevention'."
    )

    output = {
        "hookSpecificOutput": {
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
