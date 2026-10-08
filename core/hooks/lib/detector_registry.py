"""
Detector Registry — maps all UserPromptSubmit detectors for the unified dispatcher.

VERSION: 1.2.0  (rp1-home#2613: get_priority_group is the identity — no discovery;
                 get_independent_group's fallback reports the loader failure.
                 1.1.0 — rp1-home#2212: get_priority_group / get_independent_group
                 merge accessors — priority group unchanged by construction,
                 independent group merges plugin-fragment DETECTORS after the core
                 literal)

Defines two groups of detectors:
- Priority group: mutually exclusive, only highest-priority match fires
- Independent group: always run, can all fire simultaneously

Priority numbers match hook_priorities.py (lower = higher priority).
"""

import os

__version__ = "1.2.0"

# Base directory for all detector scripts
HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Priority group: mutually exclusive, only highest-priority match fires.
# Ordered by priority (highest first). First match wins.
PRIORITY_GROUP = [
    {"name": "issue-expert-reviewer-sweep-detector", "priority": 0},
    {"name": "issue-expert-reviewer-detector", "priority": 0},
    {"name": "issue-approval-detector", "priority": 1},
    {"name": "epic-issue-set-organizer-detector", "priority": 1},
    {"name": "issue-close-reason-detector", "priority": 2},
    {"name": "issue-worker-detector", "priority": 3},
    {"name": "issue-tester-detector", "priority": 4},
    {"name": "issue-refactor-reflector-detector", "priority": 4},
    {"name": "issue-researcher-detector", "priority": 5},
    {"name": "issue-researcher-sweep-detector", "priority": 5},
    {"name": "issue-formatter-detector", "priority": 6},
    {"name": "issue-formatter-sweep-detector", "priority": 6},
    {"name": "issue-consolidator-detector", "priority": 6},
    {"name": "issue-detector", "priority": 7},
    {"name": "issue-reopener-detector", "priority": 8},
    {"name": "issue-pr-reviewer-detector", "priority": 9},
    {"name": "issue-transfer-detector", "priority": 11},
]

# Independent group: always run, can all fire simultaneously.
INDEPENDENT_GROUP = []


def get_detector_path(name: str) -> str:
    """Get the full file path for a detector by name.

    rp1-home#2212 (KD-1): a merged fragment detector is addressed by the absolute
    ``path`` in its dict (threaded via ``load_detector_module(name, path=...)``), NOT
    by turning ``name`` into a path — ``name`` stays a bare detector name everywhere
    (``_module_cache`` key, error text, ``check_and_claim_priority``). As a defensive
    convenience, an already-absolute existing path passed here is returned unchanged;
    core detectors are bare names, which never take this branch, so core behavior is
    byte-identical.
    """
    if os.path.isabs(name) and os.path.exists(name):
        return name
    return os.path.join(HOOKS_DIR, f"{name}.py")


def get_priority_group() -> list:
    """Return the PRIORITY detector group.

    UNCHANGED by construction (rp1-home#2212, KD-2 Position 2): a fragment contributes
    INDEPENDENT detectors only, so this always returns the core ``PRIORITY_GROUP``
    literal itself. rp1-home#2613 (RP-S5): it therefore performs no plugin discovery —
    the merge it used to run was the identity, at the cost of a full discovery on every
    prompt.
    """
    return PRIORITY_GROUP


def get_independent_group() -> list:
    """Return the INDEPENDENT detector group, core first.

    = core ``INDEPENDENT_GROUP`` + each enabled plugin's fragment ``DETECTORS`` (with
    an absolute ``path``) in ``PLUGIN_ORDER``-then-name order (rp1-home#2212, R6). A
    byte no-op when no fragment-bearing plugin is enabled; a fragment detector carrying
    ``priority``/``before`` or duplicating a core detector name is rejected/shadowed by
    the loader. Lazy dual-path import; fail-open to the core list.
    """
    try:
        try:
            from lib import plugin_fragments
        except ImportError:
            import plugin_fragments  # type: ignore  # noqa: F401  (dual-path: lib/ on sys.path[0])
        _, independent = plugin_fragments.merged_detectors(
            PRIORITY_GROUP, INDEPENDENT_GROUP
        )
        return independent
    except Exception as exc:
        _report_loader_unavailable("detector_registry.get_independent_group", exc)
        return INDEPENDENT_GROUP


def _report_loader_unavailable(where: str, exc: BaseException) -> None:
    """Delegate to ``hook_registry._report_loader_unavailable`` (rp1-home#2613 RP-F6 —
    one loud report per process, shared by every loader fallback). Lazy dual-path
    import; fail-soft — reporting never changes the fallback's return value."""
    try:
        try:
            from lib.hook_registry import _report_loader_unavailable as report
        except ImportError:
            from hook_registry import _report_loader_unavailable as report  # type: ignore
        report(where, exc)
    except Exception:
        pass
