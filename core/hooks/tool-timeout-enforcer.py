#!/usr/bin/env python3
# HOOK_EVENT: PreToolUse
# HOOK_MATCHER: 
# HOOK_GROUP: pretool-global
"""
Tool Timeout Enforcer - PreToolUse Hook

VERSION: 1.0.1

Detects retry loops by checking if a tool has repeatedly failed in the
current session. When the stall count exceeds the threshold (3), injects
a warning into Claude's context advising it to stop retrying and inform
the user.

Hook Event: PreToolUse
Matcher: "" (all tools — fast exit for tools with no stall history)

Input (stdin JSON):
    {
        "session_id": "abc123",
        "tool_name": "mcp__playwright__browser_navigate",
        "tool_input": {"url": "https://..."}
    }

Output (stdout JSON, only when retry loop detected):
    {
        "decision": "allow",
        "hookSpecificOutput": {
            "additionalContext": "RETRY LOOP WARNING: ..."
        }
    }

Design: Fast-path optimized. If no stall state file exists for the
current session, the hook exits immediately with no output. Python
interpreter startup (~100-200ms on Pi) is the only cost for the
common case.
"""

import json
import sys
from pathlib import Path

# Threshold before warning (matches stall_tracker.RETRY_THRESHOLD)
RETRY_THRESHOLD = 3


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    tool_name = data.get("tool_name", "")

    if not session_id or not tool_name:
        sys.exit(0)

    # Fast path: check if state file exists before importing stall_tracker
    try:
        import hashlib
        session_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
        state_dir = Path.home() / ".claude" / "hooks" / "state"
        state_file = state_dir / f"stall-counts-{session_hash}.json"

        if not state_file.exists():
            sys.exit(0)
    except Exception:
        sys.exit(0)

    # State file exists — check stall count
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from lib.telemetry_logger import log_telemetry_event
        log_telemetry_event(__file__, "hook")
    except Exception:
        pass  # telemetry is optional (absent from the public core tier) and must never gate the retry-loop check
    try:
        from lib.stall_tracker import get_stall_count
        count = get_stall_count(session_id, tool_name)
    except Exception:
        sys.exit(0)

    if count < RETRY_THRESHOLD:
        sys.exit(0)

    # Retry loop detected — warn Claude but allow the call
    context = (
        f"RETRY LOOP WARNING: Tool '{tool_name}' has failed {count} times "
        f"this session. You are likely in a retry loop. "
        f"STOP retrying this tool and either: "
        f"(1) Try a completely different approach, "
        f"(2) Inform the user about the recurring failure, or "
        f"(3) Skip this step and continue with other work."
    )

    output = {
        "decision": "allow",
        "hookSpecificOutput": {
            "additionalContext": context,
        },
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
