"""
Block Logger - Hook Block Event Queue

VERSION: 1.3.1

Provides a single function to log hook block events to a JSONL queue file
for asynchronous processing into GitHub self-improvement issues.

This module is designed to be called from PreToolUse hooks at the moment
they block an agent action. It is completely fail-safe: no exception
from this module will ever propagate to the caller.

Queue File: ~/.claude/logs/block-queue.jsonl
Format: One JSON object per line (JSONL)
Processing: Handled by ~/.claude/scripts/process-blocks.py (hourly cron)

Usage:
    try:
        from lib.block_logger import log_hook_block
        log_hook_block(
            hook="version-bump-enforcer",
            reason="BLOCKED: Agent file modified without version bump.",
            tool_name="Edit",
            file_path="~/.claude/agents/issue-worker.md",
        )
    except Exception:
        pass  # Never let logging disrupt the hook

Analysis with jq:
    # Count blocks per hook
    jq -r '.hook' ~/.claude/logs/block-queue.jsonl | sort | uniq -c | sort -rn

    # Show unique block patterns
    jq -r '[.hook, .reason_summary] | @tsv' ~/.claude/logs/block-queue.jsonl | sort -u

Smoke-testing a hook that calls this logger:
    A guard hook's pytest suite is the sanctioned verification path — pytest sets
    PYTEST_CURRENT_TEST, which this module auto-detects to suppress queue writes, so
    `python3 -m pytest tests/test_<hook>.py` never pollutes the production queue.

    Always prefix an UNAVOIDABLE live CLI smoke-test (piping a synthetic
    {"tool_name": ...} payload into the hook subprocess) with
    CLAUDE_BLOCK_LOGGER_DISABLED=1, because an un-suppressed smoke-test writes a real
    block-queue entry that the hourly process-blocks.py cron converts into a spurious
    self-improvement issue (file-hook instance rp1-home#796; command-hook instance
    rp1-home#892).

    Example:
        CLAUDE_BLOCK_LOGGER_DISABLED=1 \\
          echo '{"tool_name":"Bash","tool_input":{"command":"echo [object Promise]"}}' \\
          | python3 ~/.claude/hooks/repl-promise-coercion-guard.py
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.3.1"

# Queue file location
QUEUE_DIR = Path.home() / ".claude" / "logs"
QUEUE_FILE = QUEUE_DIR / "block-queue.jsonl"

# Truncate long commands/reasons to keep queue entries manageable
MAX_COMMAND_LENGTH = 200
MAX_REASON_LENGTH = 2000


def _extract_reason_summary(reason: str) -> str:
    """Extract a one-line summary from a block reason message.

    Takes the first non-empty line after stripping common prefixes
    like 'BLOCKED:' and emoji markers.
    """
    for line in reason.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip common prefixes
        for prefix in ("BLOCKED:", "BLOCKED -", "🚫 BLOCKED:"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line:
            return line[:120]  # Cap summary length
    return reason[:120].strip()


def _normalize_for_fingerprint(text: str) -> str:
    """Normalize variable content so identical patterns produce identical fingerprints.

    Strips issue numbers, file paths, step numbers, and other variable fragments
    that would cause the same block pattern type to produce different fingerprints.
    E.g., "research issue #42" and "research issue #99" normalize to the same string.

    Added in v1.1.0 to fix duplicate issue creation (rp1-home#356).
    Extended in v1.2.0 (rp1-home#494) with step-number stripping and final lowercase
    so "Skipping Step 4" / "Skipping Step 12" / "to save time" / "To save time" all
    collapse to identical normalized strings.
    """
    # Issue numbers with hash: #42 -> #N
    text = re.sub(r"#\d+", "#N", text)
    # Bare issue numbers after "issue": "issue 42" -> "issue N"
    text = re.sub(r"\bissue\s+\d+", "issue N", text)
    # File paths: ~/.claude/agents/issue-worker.md -> <path>
    text = re.sub(r"[~/.][\w/._-]+\.md\b", "<path>", text)
    # Python list literals: ['foo', 'bar'] -> [...] (variable match content)
    # Also handles truncated lists where closing ] was cut by summary length cap
    text = re.sub(r"\[.*?\]", "[...]", text)
    text = re.sub(r"\[[^\]]*$", "[...]", text)
    # Step numbers: "Step 4" / "step 12" / "STEP 99" -> "step N" (rp1-home#494 Bucket A)
    text = re.sub(r"\bstep\s+\d+", "step N", text, flags=re.IGNORECASE)
    # Final pass: lowercase to collapse case variants (rp1-home#494 Bucket A).
    # Applied last so the literal substitutions above operate on the original case.
    text = text.lower()
    return text


def _compute_fingerprint(
    hook: str, reason_summary: str, reason_class: str = ""
) -> str:
    """Generate a deduplication fingerprint for a block pattern.

    Uses MD5 of hook name + key part. This groups blocks by pattern rather than
    individual occurrence — the same hook blocking for the same reason always
    produces the same fingerprint.

    Two key paths (added in v1.2.0, rp1-home#494):
    - If `reason_class` is non-empty, the fingerprint key is "{hook}:class={reason_class}".
      This is the explicit opt-in path: the calling hook supplies a stable class
      label that controls fingerprinting independent of the variable `reason_summary`
      text. The "class=" prefix namespaces the value so a literal reason that happens
      to equal a class label can never collide.
    - If `reason_class` is empty (the default, backward-compatible), the fingerprint
      key is "{hook}:{normalized_reason_summary}" — the original v1.0.0/v1.1.0 path,
      with the v1.2.0 normalizer enhancements (lowercase, step numbers).
    """
    if reason_class:
        key = f"{hook}:class={reason_class}"
    else:
        normalized = _normalize_for_fingerprint(reason_summary)
        key = f"{hook}:{normalized}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def log_hook_block(
    hook: str,
    reason: str,
    tool_name: str = "",
    command: str = "",
    file_path: str = "",
    reason_class: str = "",
) -> None:
    """Log a hook block event to the queue file.

    This function MUST be called inside a try/except at the call site.
    It is designed to never raise, but defense-in-depth is the right
    approach for code that runs in the critical path of hook execution.

    Args:
        hook: Name of the blocking hook (e.g., "version-bump-enforcer").
        reason: Full block reason message shown to the agent.
        tool_name: The tool that was blocked (e.g., "Bash", "Edit").
        command: The command that was blocked (truncated to 200 chars).
        file_path: The file path involved, if applicable.
        reason_class: Optional stable class label for fingerprinting (v1.2.0,
            rp1-home#494). When provided, the fingerprint is computed from
            (hook, reason_class) only — independent of the variable `reason`
            text — so the calling hook can supply a constant value here while
            still passing variable, human-readable prose in `reason` for the
            generated issue body. When empty (default), falls back to the
            normalized-reason fingerprint path for backward compatibility.
    """
    # Test-suite isolation guard (rp1-home#578).
    # Suppresses queue writes during pytest runs and explicit-opt-in test contexts.
    #
    # Three env vars control behavior:
    # - PYTEST_CURRENT_TEST: set by pytest for every test (in-process AND subprocess
    #   via env inheritance). Triggers guard.
    # - CLAUDE_BLOCK_LOGGER_DISABLED=1: explicit opt-in disable for non-pytest test
    #   contexts (CI scripts, ad-hoc verification). Triggers guard.
    # - CLAUDE_BLOCK_LOGGER_FORCE_ENABLE=1: explicit override that bypasses the guard.
    #   Required because pytest auto-re-sets PYTEST_CURRENT_TEST at each phase
    #   boundary (setup → call → teardown), so monkeypatch.delenv in a fixture cannot
    #   suppress it for the test body. Tests that legitimately exercise the writer
    #   set this var via monkeypatch.setenv (which sticks because pytest doesn't
    #   manage it). Production code MUST NOT set this var.
    if os.environ.get("CLAUDE_BLOCK_LOGGER_FORCE_ENABLE") != "1" and (
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("CLAUDE_BLOCK_LOGGER_DISABLED") == "1"
    ):
        return
    try:
        reason_summary = _extract_reason_summary(reason)
        fingerprint = _compute_fingerprint(hook, reason_summary, reason_class)

        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hook": hook,
            "reason_summary": reason_summary,
            "reason_class": reason_class or None,
            "fingerprint": fingerprint,
            "tool_name": tool_name or None,
            "command": (command[:MAX_COMMAND_LENGTH] if command else None),
            "file_path": file_path or None,
            "reason": reason[:MAX_REASON_LENGTH] if len(reason) > MAX_REASON_LENGTH else reason,
        }

        # Remove None values for compact output
        clean_entry = {k: v for k, v in entry.items() if v is not None}

        QUEUE_DIR.mkdir(parents=True, exist_ok=True)

        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(clean_entry, separators=(",", ":")) + "\n")

    except Exception:
        # Absolute guarantee: this function never disrupts the hook
        pass
