#!/usr/bin/env python3
"""Orchestration/monitor-tick markers — shared single source of truth.

rp1-home #974/#1092: a machine-generated orchestration/monitor tick (e.g. a
/loop-cron PM tick that DESCRIBES approval logic) is never a genuine user
command. These markers + predicates are consumed by:
  - unified-prompt-dispatcher.py  (universal gate — marker-only)
  - issue-approval-detector.py    (is_orchestration_prompt — marker OR oversized)
  - issue-worker-detector.py      (defense-in-depth marker bail — marker-only)

Extracted from issue-approval-detector.py by #1092 so the dispatcher's
universal gate and the approval detector share ONE marker set (no drift).

rp1-home#1726: the tuple additionally suppresses peer-session status reports and
PM-to-PM relay/catch-up messages (the `<!-- peer-session report` / `<!-- pm-relay`
anchors) across ALL issue-* detectors via the same universal gate — the #1681 shape.
"""

# rp1-home#884: a genuine generic approval is SHORT. A long, machine-generated
# orchestration directive must never be classified as a user GRANTING approval.
# 50 words is far above any real approval-with-commentary yet far below a
# hundreds-to-thousands-word tick. APPROVAL-SPECIFIC — see has_orchestration_marker
# for the marker-only predicate used by the universal dispatcher gate.
MAX_GENERIC_APPROVAL_WORDS = 50

# rp1-home#1911: ceiling on the explicit-approval exemption (see is_orchestration_prompt).
# A genuine explicit `#N` approval carrying rationale must NOT be length-suppressed, but an
# UNBOUNDED exemption would remove the only mechanical length guard on the explicit path:
# EXPLICIT_APPROVAL_PATTERNS (issue-approval-detector.py) are hash-OPTIONAL `#?(\d+)`, and
# marker-less machine text (LLM-authored `hapi api sessions send`, long operator pastes
# quoting a third-party "LGTM #1234") arrives as role=="user". 200 words clears #1545's
# 75-word trigger ~2.7x and every realistic approval-with-commentary while preserving #884's
# "far below a hundreds-to-thousands-word tick" property. APPROVAL-SPECIFIC (like the 50-word
# ceiling) — NOT used by the universal dispatcher gate.
MAX_EXPLICIT_APPROVAL_WORDS = 200

# rp1-home#884: machine-generated orchestration/monitor-tick markers. Lowercased
# substrings — matched against prompt.lower().
ORCHESTRATION_PROMPT_MARKERS = (
    "pm monitoring tick",
    # rp1-home#1681: the "pm monitoring tick" / "loop-cron tick" literals in this
    # tuple pre-date #1547's PMMON heredoc and never match its ACTUAL text, so every
    # ~4-min pm-monitor tick tripped every issue-* detector (its provenance comment
    # carries an extractable #N and a bare "research" verb). A loop-cron monitor tick
    # is an AUTOMATED machine prompt, never a user issue-workflow request. Both new
    # markers are HTML-comment-ANCHORED so a genuine composite request that merely
    # MENTIONS a tick (e.g. "research issue #N — the pm-monitor tick fix") is NOT
    # suppressed.
    "<!-- loop_cron_tick",   # loop-cron.sh cmd_tick machine marker (in MACHINE_PREFIXES)
    "<!-- pm-monitor tick",  # PMMON heredoc opener in spawn-peer-sessions/SKILL.md
    # rp1-home#1726: peer-session status reports and PM-to-PM relay/catch-up messages
    # carry issue-workflow vocabulary ("Gate-2 APPROVED", "closer running", "dup",
    # "reconcile", "PR-create", "10/35") that tripped every issue-* detector when
    # delivered as a send-to-session machine message. HTML-comment-ANCHORED (matched
    # against prompt.lower()) so a genuine operator request that merely MENTIONS a peer
    # report / relay is NOT suppressed. Producer convention published in
    # project-manager-role/SKILL.md § "Machine-originated message convention".
    "<!-- peer-session report",  # a spawned peer's status report to a monitoring session
    "<!-- pm-relay",             # a PM-to-PM relay / catch-up message
    # rp1-home#1683: the canonical PM issue-worker/closer KICKOFF provenance line. A PM
    # spawns each issue chain with a Step-2.3 kickoff prompt that leads with the literal
    # `This is a PM-spawned issue-worker/closer chain (orchestrator session <PM_SID>, epic
    # #<N> engagement).` (project-manager-role/SKILL.md:1801). That line carries an
    # extractable #N plus work/research verbs, so before #1683 it tripped every issue-*
    # detector IN ADDITION to the explicit /issue-worker slash invocation that actually
    # drives the chain. This entry is the STABLE PREFIX — it stops before the per-engagement
    # parenthetical (the `<PM_SID>`/`#<N>` tail varies). Prose, not HTML-comment-anchored,
    # but the phrase is specific enough that no genuine in-session request contains it.
    # Matched against prompt.lower(), so it is lowercased like every entry in this tuple.
    "this is a pm-spawned issue-worker/closer chain",
    "project-manager-role",
    "/project-manager-role",
    "follow the project-manager-role skill",
    "/loop-cron",
    "loop-cron tick",
    "watched sessions",
    "watched-sessions",
)


def has_orchestration_marker(prompt: str) -> bool:
    """rp1-home #1092: marker-ONLY orchestration check. Safe as a UNIVERSAL gate
    across ALL detectors because it does NOT use the approval-specific word-count
    heuristic (a long legitimate work command must still reach the worker). Used
    by unified-prompt-dispatcher.py and the worker detector's defense-in-depth bail."""
    pl = prompt.lower()
    return any(marker in pl for marker in ORCHESTRATION_PROMPT_MARKERS)


def is_orchestration_prompt(prompt: str, *, has_explicit_approval: bool = False) -> bool:
    """rp1-home #974: marker substring OR oversized prompt. APPROVAL-detector
    behavior preserved bit-for-bit (the >MAX_GENERIC_APPROVAL_WORDS arm is an
    approval-specific defense-in-depth and is NOT used by the universal gate).

    rp1-home#1911: a THIRD arm — the explicit-approval exemption — sits BETWEEN the
    marker arm and the word-count arm. When the caller passes has_explicit_approval=True
    (a literal-`#`-anchored, non-negated approval reference is present) AND the prompt is
    at most MAX_EXPLICIT_APPROVAL_WORDS (200) words, the prompt is NOT treated as an
    orchestration tick, so a genuine verbose human approval mints regardless of the
    50-word generic ceiling. Two invariants keep the false-mint / approval-forgery path
    closed: (1) the marker arm stays FIRST, so a marker-bearing tick that merely QUOTES
    an approval phrase still suppresses; (2) the exemption is BOUNDED at 200 words, so a
    hundreds-to-thousands-word marker-less machine tick still suppresses (#884's property).
    The `has_explicit_approval` default of False keeps every existing caller bit-for-bit.
    Still APPROVAL-SPECIFIC — has_orchestration_marker (the universal gate) is untouched."""
    if has_orchestration_marker(prompt):
        return True                 # marker ALWAYS wins — R2 / AC-2 (rp1-home#1911)
    words = len(prompt.split())
    if has_explicit_approval and words <= MAX_EXPLICIT_APPROVAL_WORDS:
        return False                # rp1-home#1911 — a bounded explicit #N approval is not "oversized"
    if words > MAX_GENERIC_APPROVAL_WORDS:
        return True
    return False
