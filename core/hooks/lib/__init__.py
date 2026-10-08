# Claude Code Hooks Shared Utilities
#
# This package provides common functions used across multiple hooks
# to eliminate code duplication and ensure consistent behavior.
#
# Usage:
#   from lib.github_utils import get_git_repo, extract_issue_number
#   from lib.workflow_utils import validate_handoff_requirements, check_staleness

"""
Claude Code Hooks - Shared Utilities Package

Provides:
- github_utils: Git repository detection, issue number extraction
- workflow_utils: State validation, handoff requirements, staleness detection
- meta_detection: Meta-discussion detection to prevent false positive hook triggers
- hook_priorities: Priority-based mutual exclusion for issue-related hooks (includes reopener)
- telemetry: Local agent invocation logging with automatic rotation
- block_logger: Hook block event queue for self-improvement issue generation
- agent_diagnostic_logger: Agent recovery path event logging for self-improvement
- session_linking: HAPImatic session linking/unlinking for issue tracking
- lock_sentinel: PID-based lock liveness tracking for agent workflows
- workflow_sentinel: Active workflow state persistence for context compaction recovery
"""

__version__ = "1.8.0"
