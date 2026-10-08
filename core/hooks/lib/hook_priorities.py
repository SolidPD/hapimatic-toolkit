"""
Hook Priority and Mutual Exclusion System

VERSION: 1.2.0

Ensures only ONE issue/PR-related hook fires per user prompt by implementing
a sentinel-file-based mutual exclusion mechanism.

How it works:
  1. Hooks are assigned priority numbers (lower = higher priority)
  2. When a hook detects its intent, it calls check_and_claim_priority()
  3. If no higher-priority hook has already claimed this prompt, the hook
     proceeds and writes a sentinel file
  4. If a higher-priority hook already claimed, the hook yields (exits silently)
  5. Sentinel files auto-expire after SENTINEL_TTL_SECONDS (60s)

Priority Order (highest first):
  1. user-approval-mint-fallback   - Nonce fallback mint (rp1-home #1750 D4a; ordered BEFORE issue-approval in the registry)
  1. issue-approval-detector       - RETIRED no-op (rp1-home #1950: phrase approvals no longer mint; entry kept for ordering/validator parity)
  1. epic-issue-set-organizer-detector - Organize/reconcile an epic set (disjoint phrases from approval; shares priority; rp1-home #1562)
  2. issue-close-reason-detector   - Close as duplicate/not-planned
  3. issue-worker-detector         - Specific (detects work intent)
  4. issue-tester-detector         - Test/verify intent (Issue #367)
  4. issue-refactor-reflector-detector - Refactor-reflect intent (rp1-home #943; disjoint phrases; shares priority with tester)
  5. issue-researcher-detector     - Specific (detects research intent)
  5. issue-researcher-sweep-detector - Sweep (disjoint patterns; shares priority with researcher; #624)
  6. issue-formatter-detector      - Moderate (format existing issue)
  6. issue-formatter-sweep-detector - Sweep (disjoint patterns; share priority with formatter)
  6. issue-consolidator-detector   - Consolidate duplicates (disjoint patterns; shares priority with formatter; #618)
  7. issue-detector                - Least specific (create/format fallback)
  8. issue-reopener-detector       - Reopen intent (after create fallback)
  9. issue-pr-reviewer-detector    - PR review intent (adjacent to issue lifecycle)
  10. github-project-detector      - GitHub Project v2 tracker creation (rp1-home #745, placed before project-creator)
  10. project-creator-detector     - Project scaffolding (least specific)
  11. issue-transfer-detector      - Transfer issue between repos (after lifecycle hooks)

Requirements:
  - Hooks in settings.json MUST be ordered highest-priority first
  - Each hook calls check_and_claim_priority() AFTER detecting its intent
    but BEFORE injecting context output
  - Fail-open: if the mechanism fails, hooks proceed normally

Sentinel Files:
  - Location: ~/.claude/hooks/.sentinels/
  - Format: hook-fired-{prompt_hash}.json
  - Contents: {"hook": "name", "priority": N, "timestamp": T}
  - TTL: 60 seconds (auto-cleaned by every hook invocation)
"""

import os
import json
import hashlib
import time
from pathlib import Path

__version__ = "1.3.0"

# Sentinel directory for prompt-scoped mutex files
SENTINEL_DIR = Path(os.path.expanduser("~/.claude/hooks/.sentinels"))

# Priority assignments: lower number = higher priority
HOOK_PRIORITIES = {
    "issue-expert-reviewer-sweep-detector": 0,
    "issue-expert-reviewer-detector": 0,
    "issue-approval-detector": 1,
    "epic-issue-set-organizer-detector": 1,
    "issue-close-reason-detector": 2,
    "issue-worker-detector": 3,
    "issue-tester-detector": 4,
    "issue-refactor-reflector-detector": 4,
    "issue-researcher-detector": 5,
    "issue-researcher-sweep-detector": 5,
    "issue-formatter-detector": 6,
    "issue-formatter-sweep-detector": 6,
    "issue-consolidator-detector": 6,
    "issue-detector": 7,
    "issue-reopener-detector": 8,
    "issue-pr-reviewer-detector": 9,
    "issue-transfer-detector": 11,
}

# Sentinel files older than this are automatically cleaned up
SENTINEL_TTL_SECONDS = 60


def _get_prompt_hash(prompt: str) -> str:
    """
    Generate a short hash of the prompt for sentinel filename.

    Uses MD5 truncated to 8 hex chars. This is not for security -
    just for creating unique-enough filenames per prompt.
    """
    return hashlib.md5(prompt.encode()).hexdigest()[:8]


def _get_sentinel_path(prompt_hash: str) -> Path:
    """Get the sentinel file path for a given prompt hash."""
    return SENTINEL_DIR / f"hook-fired-{prompt_hash}.json"


def cleanup_stale_sentinels() -> int:
    """
    Remove sentinel files older than SENTINEL_TTL_SECONDS.

    Called automatically by check_and_claim_priority() to prevent
    sentinel accumulation. Each hook invocation cleans up stale files.

    Returns:
        Number of sentinel files removed.
    """
    if not SENTINEL_DIR.exists():
        return 0

    removed = 0
    now = time.time()

    try:
        for sentinel_file in SENTINEL_DIR.glob("hook-fired-*.json"):
            try:
                age = now - sentinel_file.stat().st_mtime
                if age > SENTINEL_TTL_SECONDS:
                    sentinel_file.unlink()
                    removed += 1
            except (OSError, PermissionError):
                pass  # Skip files we can't access
    except Exception:
        pass  # Don't fail if directory listing fails

    return removed


def check_and_claim_priority(hook_name: str, prompt: str) -> bool:
    """
    Check if this hook should fire, and claim priority if so.

    This is the main entry point for the mutual exclusion system.
    Each hook calls this AFTER detecting its intent but BEFORE
    injecting any context output.

    Args:
        hook_name: The hook's identifier (must match a key in HOOK_PRIORITIES).
        prompt: The user's prompt text (used to generate prompt-scoped sentinel).

    Returns:
        True if this hook should proceed with context injection.
        False if a higher-priority hook has already claimed this prompt.

    Behavior:
        - If no sentinel exists: creates one and returns True
        - If sentinel exists with higher priority (lower number): returns False
        - If sentinel exists with lower priority (higher number): overwrites and returns True
        - If sentinel exists with equal priority: returns False (first claim wins)
        - On any error: returns True (fail-open design)
    """
    try:
        # Clean up stale sentinels on every invocation
        cleanup_stale_sentinels()

        priority = HOOK_PRIORITIES.get(hook_name)
        if priority is None:
            # Unknown hook name - let it through (fail-open)
            return True

        prompt_hash = _get_prompt_hash(prompt)
        sentinel_path = _get_sentinel_path(prompt_hash)

        # Check if a sentinel already exists for this prompt
        if sentinel_path.exists():
            try:
                with open(sentinel_path, 'r') as f:
                    existing = json.load(f)

                existing_priority = existing.get("priority", 999)

                if existing_priority <= priority:
                    # A higher or equal priority hook already claimed this prompt.
                    # Yield: do not fire this hook.
                    return False

                # This hook has higher priority than the existing claim.
                # This shouldn't normally happen with correct hook ordering
                # (highest priority runs first), but handle it gracefully
                # by overwriting the sentinel.
            except (json.JSONDecodeError, KeyError, TypeError):
                # Corrupt sentinel file - overwrite it
                pass

        # Claim this prompt for this hook
        SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

        sentinel_data = {
            "hook": hook_name,
            "priority": priority,
            "timestamp": time.time(),
        }

        with open(sentinel_path, 'w') as f:
            json.dump(sentinel_data, f)

        return True

    except Exception:
        # Fail-open: if anything goes wrong with the priority system,
        # allow the hook to proceed with its normal logic.
        # This ensures the workflow is never broken by the mutex mechanism.
        return True
