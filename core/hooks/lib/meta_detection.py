"""
Meta-Discussion Detection - Shared Library for Issue Workflow Hooks

This module provides patterns and functions to detect when a user is DISCUSSING
the issue workflow (agents, hooks, detectors) rather than INVOKING it.

This prevents false positives where hooks fire when the user is talking about
the workflow system itself rather than performing workflow actions.

Examples of meta-discussion (should NOT trigger hooks):
- "audit the issue worker agent definition"
- "how does the issue creator work?"
- "review the issue-closer workflow"
- "recommend improvements to the issue-formatter"
- "let's look at the hook implementation"

Examples of real invocations (SHOULD trigger hooks):
- "work on issue #42"
- "create an issue for the bug"
- "close issue #42, it's done"
- "research issue #15"
- "review PR #262"
- "review PR #42 on my-repo"
"""

import re

__version__ = "1.3.0"

# rp1-home#1681: SECONDARY defence-in-depth. is_meta_discussion() DELEGATES to the
# shared machine-tick gate (lib/orchestration_markers.has_orchestration_marker)
# rather than duplicating its marker set, so orchestration_markers.py stays the
# single source of truth for monitor-tick literals. The PRIMARY gate is the
# dispatcher's has_orchestration_marker check (unified-prompt-dispatcher.py, the
# sole registered UserPromptSubmit hook); this delegation only covers the issue-*
# detectors' standalone main() paths, reachable in production solely from frozen
# old-worktree settings.json files.
try:
    from lib.orchestration_markers import has_orchestration_marker as _has_orchestration_marker
except ImportError:  # pragma: no cover — lib/ is on sys.path in every live import context
    try:
        from orchestration_markers import has_orchestration_marker as _has_orchestration_marker
    except ImportError:
        _has_orchestration_marker = None

# Patterns that indicate the user is TALKING ABOUT the workflow system
# rather than invoking it. These are checked BEFORE trigger patterns.
META_DISCUSSION_PATTERNS = [
    # Talking about agents/hooks/detectors as objects of discussion
    r'\b(audit|review|analyze|evaluate|examine|inspect|check)\b.*\b(agent|workflow|hook|detector)\b',
    r'\b(agent|workflow|hook|detector)\b.*\b(audit|review|analysis|evaluation|definition|config|implementation|code|source|logic)\b',

    # Recommending changes to the workflow system
    r'\b(recommend|suggest|propose|improve|enhance|optimize|refactor|update|modify|change|fix)\b.*\b(agent|workflow|hook|detector|pattern|trigger)\b',
    r'\b(recommend|suggest|propose|improve|enhance|optimize|refactor|update|modify|change|fix)\b.*\bissue[- ]?(creator|worker|closer|researcher|formatter|approval|pr[- ]?reviewer)\b',
    r'\b(agent|workflow|hook|detector)\b.*\b(improvement|enhancement|change|update|modification|bug|issue|problem)\b(?!.*#\d+)',

    # Asking questions about how the workflow works
    r'\b(how|why|when|where|what)\s+(does|do|is|are|should|would|could|can)\s+(the\s+)?\b.*\b(issue|workflow|hook|agent|detector)\b.*\b(work|function|operate|trigger|fire|detect|run)\b',
    r'\b(explain|describe|document|understand)\b.*\b(workflow|agent|hook|detector)\b',

    # Naming specific agents/hooks as discussion topics (NOT as invocations)
    # "the issue-worker agent" / "issue-creator hook" / "issue-closer workflow"
    r'\b(the\s+)?issue[- ]?(creator|worker|closer|researcher|formatter|approval|reopener|pr[- ]?reviewer)[- ]?(agent|hook|detector|workflow|definition|implementation|code|logic|pattern)\b',

    # Discussing the PR review system as a concept (NOT reviewing an actual PR)
    r'\b(review|check)\s+(?:the\s+)?(?:PR|pull\s+request)\s+(?:reviewer|workflow|agent|hook|system|process)\b',

    # Planning/designing/architecting workflow changes
    r'\b(design|plan|architect|redesign|rethink|rework)\b.*\b(workflow|agent|hook|detector)\b',

    # Implementing workflow infrastructure itself
    r'\b(implementing|building|creating|writing|coding|developing)\b.*\b(hook|detector|agent|workflow)\b',

    # Looking at / reading workflow code
    r'\b(look\s+at|read|show\s+me|open|view|display|see)\s+(the\s+)?\b.*\b(hook|detector|agent|workflow)\b.*\b(code|source|file|implementation|definition|logic)\b',

    # Testing/debugging the workflow system
    r'\b(test|debug|troubleshoot|diagnose)\b.*\b(hook|detector|agent|workflow)\b',

    # Discussing patterns/triggers of the hooks
    r'\b(trigger|detection|pattern|regex)\b.*\b(hook|detector|agent)\b',
    r'\b(hook|detector|agent)\b.*\b(trigger|detection|pattern|regex)\b',

    # Meta-references to "all hooks" or "the hooks"
    r'\b(all|every|each|the)\s+(issue\s+)?(hooks?|detectors?|agents?)\b.*\b(need|should|must|could|update|change|add|remove)\b',
    r'\b(need|should|must|could|want)\s+to\s+\b.*\b(all|every|each|the)\s+(issue\s+)?(hooks?|detectors?|agents?)\b',
    r'\b(update|change|add|remove|fix|modify)\b.*\b(all|every|each|the)\s+(issue\s+)?(hooks?|detectors?|agents?)\b',
    r'\b(add|apply|include)\b.*\b(to|for|in)\s+(the\s+)?(hooks?|detectors?|agents?)\b',

    # Discussing this very feature (meta-discussion exclusion)
    r'\bmeta[- ]?discussion\b',
    r'\bfalse\s+positive\b.*\b(hook|detector|trigger|workflow)\b',
    r'\bfalse\s+positive\b.*\bprevention\b',
    r'\bexclusion\s+pattern\b',
]


def is_meta_discussion(prompt: str) -> bool:
    """
    Check if the prompt is discussing the workflow system rather than invoking it.

    This function should be called BEFORE checking for trigger patterns in any
    issue-related hook. If it returns True, the hook should exit without
    injecting any context.

    Args:
        prompt: The user's prompt text.

    Returns:
        True if the prompt is a meta-discussion about the workflow system,
        False if it appears to be a genuine invocation.
    """
    # rp1-home#1681: SECONDARY defence-in-depth ONLY. A loop-cron / pm-monitor tick
    # is a machine prompt, never a user workflow request. Delegate to the shared
    # single-source-of-truth gate (never duplicate its marker literals). The PRIMARY
    # enforcement is the dispatcher's has_orchestration_marker gate; this covers the
    # detectors' standalone main() paths (unregistered in production, reachable only
    # from frozen old-worktree settings.json).
    if _has_orchestration_marker is not None and _has_orchestration_marker(prompt):
        return True

    prompt_lower = prompt.lower()

    # Quick exit: if the prompt contains a clear invocation marker with an
    # issue number in a command context, it is probably a real invocation
    # even if it also mentions agents/hooks.
    # e.g., "work on issue #38 - it's about adding meta-discussion to hooks"
    direct_invocation_markers = [
        r'\b(work\s+on|fix|implement|resolve|tackle|address|close|approve|research|create|reopen)\s+(issue\s+)?#\d+\b',
        r'\b(create|make|open|file)\s+(a\s+|an\s+)?(new\s+)?(github\s+)?issue\s+(for|about|to)\b',
        r'\blgtm\b.*#\d+',
        r'\bapprove[d]?\s+(issue\s+)?#\d+',
        r'\breview\s+(?:the\s+)?(?:PR|pull\s+request)\s*#\d+\b',
        r'\b(?:check|look\s+at)\s+(?:PR|pull\s+request)\s*#\d+\b',
    ]

    for marker in direct_invocation_markers:
        if re.search(marker, prompt_lower):
            return False

    # Check meta-discussion patterns
    for pattern in META_DISCUSSION_PATTERNS:
        if re.search(pattern, prompt_lower):
            return True

    return False
