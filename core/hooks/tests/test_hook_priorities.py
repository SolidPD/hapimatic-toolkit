"""
Tests for lib/hook_priorities.py — Priority-based mutual exclusion system.

Tests cover:
- HOOK_PRIORITIES: expected priority assignments
- _get_prompt_hash(): deterministic hashing
- check_and_claim_priority(): claiming, yielding, overwriting
- cleanup_stale_sentinels(): TTL-based cleanup
- Fail-open behavior on errors
"""

import json
import time
import sys
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from lib.hook_priorities import (
    HOOK_PRIORITIES,
    SENTINEL_TTL_SECONDS,
    _get_prompt_hash,
    _get_sentinel_path,
    check_and_claim_priority,
    cleanup_stale_sentinels,
)


# ═══════════════════════════════════════════════════════════════════════
# Priority assignments
# ═══════════════════════════════════════════════════════════════════════

class TestPriorityAssignments:

    def test_approval_highest(self):
        assert HOOK_PRIORITIES["issue-approval-detector"] == 1

    def test_worker_before_researcher(self):
        assert HOOK_PRIORITIES["issue-worker-detector"] < HOOK_PRIORITIES["issue-researcher-detector"]

    def test_researcher_before_formatter(self):
        assert HOOK_PRIORITIES["issue-researcher-detector"] < HOOK_PRIORITIES["issue-formatter-detector"]

    def test_formatter_before_detector(self):
        assert HOOK_PRIORITIES["issue-formatter-detector"] < HOOK_PRIORITIES["issue-detector"]

    def test_detector_before_reopener(self):
        assert HOOK_PRIORITIES["issue-detector"] < HOOK_PRIORITIES["issue-reopener-detector"]

    def test_reopener_before_pr_reviewer(self):
        assert HOOK_PRIORITIES["issue-reopener-detector"] < HOOK_PRIORITIES["issue-pr-reviewer-detector"]

    def test_all_hooks_registered(self):
        expected_hooks = {
            "issue-expert-reviewer-detector",
            "issue-expert-reviewer-sweep-detector",
            "issue-approval-detector",
            "epic-issue-set-organizer-detector",
            "issue-close-reason-detector",
            "issue-worker-detector",
            "issue-tester-detector",
            "issue-refactor-reflector-detector",
            "issue-researcher-detector",
            "issue-researcher-sweep-detector",
            "issue-formatter-detector",
            "issue-formatter-sweep-detector",
            "issue-consolidator-detector",
            "issue-detector",
            "issue-reopener-detector",
            "issue-pr-reviewer-detector",
            "issue-transfer-detector",
        }
        assert set(HOOK_PRIORITIES.keys()) == expected_hooks


# ═══════════════════════════════════════════════════════════════════════
# _get_prompt_hash
# ═══════════════════════════════════════════════════════════════════════

class TestGetPromptHash:

    def test_deterministic(self):
        """Same prompt should always produce same hash."""
        h1 = _get_prompt_hash("fix issue #42")
        h2 = _get_prompt_hash("fix issue #42")
        assert h1 == h2

    def test_different_prompts_different_hashes(self):
        h1 = _get_prompt_hash("fix issue #42")
        h2 = _get_prompt_hash("create an issue")
        assert h1 != h2

    def test_hash_length(self):
        """Hash should be 8 hex characters."""
        h = _get_prompt_hash("any prompt")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)


# ═══════════════════════════════════════════════════════════════════════
# check_and_claim_priority
# ═══════════════════════════════════════════════════════════════════════

class TestCheckAndClaimPriority:

    def test_first_claim_succeeds(self, tmp_sentinel_dir):
        """First hook to claim a prompt should succeed."""
        result = check_and_claim_priority("issue-worker-detector", "fix issue #42")
        assert result is True

        # Sentinel file should exist
        sentinels = list(tmp_sentinel_dir.glob("hook-fired-*.json"))
        assert len(sentinels) == 1

        data = json.loads(sentinels[0].read_text())
        assert data["hook"] == "issue-worker-detector"
        assert data["priority"] == 3

    def test_higher_priority_blocks_lower(self, tmp_sentinel_dir):
        """Higher-priority hook claiming first should block lower-priority."""
        # Higher priority (approval) claims first
        assert check_and_claim_priority("issue-approval-detector", "approve #14") is True
        # Lower priority (worker) should yield
        assert check_and_claim_priority("issue-worker-detector", "approve #14") is False

    def test_lower_priority_blocks_equal(self, tmp_sentinel_dir):
        """Same-priority hook should not overwrite."""
        assert check_and_claim_priority("issue-worker-detector", "fix #42") is True
        assert check_and_claim_priority("issue-worker-detector", "fix #42") is False

    def test_higher_priority_overwrites_lower(self, tmp_sentinel_dir):
        """Higher-priority hook should overwrite a lower-priority claim."""
        # Lower priority claims first (unusual but possible with ordering issues)
        assert check_and_claim_priority("issue-detector", "fix #42") is True
        # Higher priority should overwrite
        assert check_and_claim_priority("issue-worker-detector", "fix #42") is True

        # Verify sentinel was overwritten
        sentinels = list(tmp_sentinel_dir.glob("hook-fired-*.json"))
        data = json.loads(sentinels[0].read_text())
        assert data["hook"] == "issue-worker-detector"

    def test_unknown_hook_allowed(self, tmp_sentinel_dir):
        """Unknown hook name should be allowed (fail-open)."""
        result = check_and_claim_priority("some-unknown-hook", "any prompt")
        assert result is True

    def test_different_prompts_independent(self, tmp_sentinel_dir):
        """Claims for different prompts should not interfere."""
        assert check_and_claim_priority("issue-worker-detector", "fix #42") is True
        assert check_and_claim_priority("issue-worker-detector", "fix #43") is True


# ═══════════════════════════════════════════════════════════════════════
# cleanup_stale_sentinels
# ═══════════════════════════════════════════════════════════════════════

class TestCleanupStaleSentinels:

    def test_removes_old_sentinels(self, tmp_sentinel_dir):
        """Sentinels older than TTL should be removed."""
        # Create a sentinel file
        sentinel = tmp_sentinel_dir / "hook-fired-abc12345.json"
        sentinel.write_text(json.dumps({"hook": "test", "priority": 1, "timestamp": 0}))

        # Make it old by changing mtime
        import os
        old_time = time.time() - SENTINEL_TTL_SECONDS - 10
        os.utime(sentinel, (old_time, old_time))

        removed = cleanup_stale_sentinels()
        assert removed == 1
        assert not sentinel.exists()

    def test_keeps_fresh_sentinels(self, tmp_sentinel_dir):
        """Fresh sentinels should not be removed."""
        sentinel = tmp_sentinel_dir / "hook-fired-abc12345.json"
        sentinel.write_text(json.dumps({"hook": "test", "priority": 1, "timestamp": time.time()}))

        removed = cleanup_stale_sentinels()
        assert removed == 0
        assert sentinel.exists()

    def test_empty_directory(self, tmp_sentinel_dir):
        """Should handle empty directory gracefully."""
        removed = cleanup_stale_sentinels()
        assert removed == 0

    def test_nonexistent_directory(self, tmp_path):
        """Should handle nonexistent directory gracefully."""
        nonexistent = tmp_path / "does-not-exist"
        with patch("lib.hook_priorities.SENTINEL_DIR", nonexistent):
            removed = cleanup_stale_sentinels()
            assert removed == 0


# ═══════════════════════════════════════════════════════════════════════
# Fail-open behavior
# ═══════════════════════════════════════════════════════════════════════

class TestFailOpen:

    def test_permission_error_returns_true(self, tmp_sentinel_dir):
        """If the sentinel directory is not writable, should fail-open."""
        with patch("builtins.open", side_effect=PermissionError("no write")):
            # Should return True (allow the hook to proceed)
            result = check_and_claim_priority("issue-worker-detector", "fix #42")
            assert result is True

    def test_corrupt_sentinel_overwrites(self, tmp_sentinel_dir):
        """Corrupt sentinel file should be overwritten (not crash)."""
        prompt_hash = _get_prompt_hash("fix #42")
        sentinel_path = _get_sentinel_path(prompt_hash)
        tmp_sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text("NOT VALID JSON")

        result = check_and_claim_priority("issue-worker-detector", "fix #42")
        assert result is True


# Generated by the core-tier export: the tmp_sentinel_dir fixture these tests use,
# copied from tests/conftest.py (which is not part of the core tier).
import pytest  # noqa: E402


@pytest.fixture
def tmp_sentinel_dir(tmp_path):
    """
    Provide a temporary sentinel directory and patch hook_priorities to use it.

    Yields the Path so tests can inspect sentinel files.
    """
    sentinel_dir = tmp_path / "sentinels"
    sentinel_dir.mkdir()

    with patch("lib.hook_priorities.SENTINEL_DIR", sentinel_dir):
        yield sentinel_dir
