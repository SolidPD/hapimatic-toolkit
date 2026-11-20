#!/usr/bin/env python3
"""Orchestration/monitor-tick markers — shared single source of truth.

rp1-home #974/#1092: a machine-generated orchestration/monitor tick (e.g. a
/loop-cron PM tick that DESCRIBES approval logic) is never a genuine user
command. These markers + predicates are consumed by:
  - unified-prompt-dispatcher.py  (universal gate — has_orchestration_marker)
  - issue-approval-detector.py    (is_orchestration_prompt — marker OR oversized)
  - issue-worker-detector.py      (defense-in-depth bail — has_orchestration_marker)
  - meta_detection.is_meta_discussion and the other suppress-on-True callers of
    has_orchestration_marker (account, recover-session, skill-creator-augment, …)

Extracted from issue-approval-detector.py by #1092 so the dispatcher's
universal gate and the approval detector share ONE marker set (no drift).

rp1-home#1726: the tuple additionally suppresses peer-session status reports and
PM-to-PM relay/catch-up messages (the `<!-- peer-session report` / `<!-- pm-relay`
anchors) across ALL issue-* detectors via the same universal gate — the #1681 shape.

rp1-home#3226: has_orchestration_marker also returns True for ONE structural
machine-message shape — a Pins answers (or automatic-pick) message, recognized by
is_pins_answers_message. Every caller above inherits it unedited. The hooks that run
OUTSIDE the dispatcher gate call is_pins_answers_message directly
(pm-role-marker-writer.py, lora-import-detector.py); loop-cron-pause-gate.py anchors its
own marker instead. The shape is matched by POSITION, never as a substring, so an
operator sentence that merely mentions the format is still read (the #1681/#1726
anchoring rule).
"""

import re

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


# rp1-home#3226: a Pins answers message is machine text to every prompt-reading hook.
# The Hapimatic pin-answers producer (`shared/src/pinAnswers.ts` `formatPinAnswersMessage`;
# the relay line in `server/src/sync/pinSubmission.ts` `relayLine`) emits one fixed shape:
# line 1 `Pins answers, <time>: …` or `Pins automatic pick, <time>: …`, a blank line, then
# the fence opener below (optionally preceded by the server's relay line and a blank line).
# A change to the heading words, the relay line or the fence language there must change
# this pattern FIRST, or the hole this closes silently reopens.
PINS_ANSWERS_FENCE_LANGUAGE = "hapi_pin_answers"

# Harness container tags that may lead a prompt (a pending notification the harness
# attaches). Case-sensitive: the harness emits lowercase tags. Unlike
# lib/system_notifications.py, `<output-file>` is not stripped — only these two lead a
# prompt.
_HARNESS_SPAN_OPEN = re.compile(r"\s*<(task-notification|system-reminder)>")

# Start-anchored (used with .match), case-sensitive, no JSON parse and no end anchor:
# text appended after a pasted message is suppressed with it (the safe direction, the
# operator's PASTE1 ruling). The relay name has no length cap, because the producer
# already bounds it.
_PINS_ANSWERS_MESSAGE = re.compile(
    r"\s*(?:Answers for [^\r\n]+, which is not running, relayed to you as its Project Manager\r?\n\r?\n)?"
    r"Pins (?:answers|automatic pick), [^\r\n]+\r?\n\r?\n"
    r"```" + re.escape(PINS_ANSWERS_FENCE_LANGUAGE) + r"[ \t]*\r?\n"
)


def _strip_leading_harness_spans(text: str) -> str:
    """Skip leading CLOSED harness spans; stop at the first non-span or an unclosed tag.

    A linear loop on purpose: one regex with a lazy span body backtracks
    catastrophically on adversarial input (rp1-home#3226 premise P8). Always call the
    COMPILED pattern's .match(text, pos): the module function re.match(pattern, text,
    pos) takes FLAGS as its third argument, so an offset like 36 raises, 40 loops
    forever and 128 (re.DEBUG) loops while printing to stdout, the hook's output channel.
    """
    pos = 0
    while True:
        m = _HARNESS_SPAN_OPEN.match(text, pos)
        if not m:
            return text[pos:]
        close_tag = f"</{m.group(1)}>"
        end = text.find(close_tag, m.end())
        if end < 0:
            return text[pos:]
        new_pos = end + len(close_tag)
        if new_pos <= pos:
            break
        pos = new_pos
    return text[pos:]


def is_pins_answers_message(prompt: str) -> bool:
    """rp1-home#3226: True when the prompt BEGINS with a Pins answers or Pins
    automatic-pick message (after leading whitespace and leading closed harness spans).

    Matched by position, not substring: an operator sentence that mentions
    `hapi_pin_answers`, prose starting `Pins answers,`, a quoted pin, or operator text
    BEFORE a pasted message all return False and are read as today. A message pasted at
    the very start is machine text in full (the operator's PASTE1 ruling), and so is
    everything inside it, notes and mark comments included (the operator's NOTE1
    ruling). Reading text typed after a paste is rp1-home#3233's."""
    if not isinstance(prompt, str) or not prompt:
        return False
    return _PINS_ANSWERS_MESSAGE.match(_strip_leading_harness_spans(prompt)) is not None


def has_orchestration_marker(prompt: str) -> bool:
    """rp1-home #1092: orchestration check — markers plus one structural machine-message
    shape (rp1-home#3226). Safe as a UNIVERSAL gate across ALL detectors because it does
    NOT use the approval-specific word-count heuristic (a long legitimate work command
    must still reach the worker). Used by unified-prompt-dispatcher.py, the worker
    detector's defense-in-depth bail and every suppress-on-True caller. The marker
    tuple is matched as substrings; the Pins arm is position-anchored
    (is_pins_answers_message), so a mention of the format is still read."""
    pl = prompt.lower()
    if any(marker in pl for marker in ORCHESTRATION_PROMPT_MARKERS):
        return True
    return is_pins_answers_message(prompt)


def is_orchestration_prompt(prompt: str, *, has_explicit_approval: bool = False) -> bool:
    """rp1-home #974: has_orchestration_marker (marker substring or, since rp1-home#3226,
    a Pins answers message) OR oversized prompt. APPROVAL-detector behavior otherwise
    preserved bit-for-bit (the >MAX_GENERIC_APPROVAL_WORDS arm is an
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
    Still APPROVAL-SPECIFIC — the word-count arms never reach has_orchestration_marker (the
    universal gate)."""
    if has_orchestration_marker(prompt):
        return True                 # marker ALWAYS wins — R2 / AC-2 (rp1-home#1911)
    words = len(prompt.split())
    if has_explicit_approval and words <= MAX_EXPLICIT_APPROVAL_WORDS:
        return False                # rp1-home#1911 — a bounded explicit #N approval is not "oversized"
    if words > MAX_GENERIC_APPROVAL_WORDS:
        return True
    return False
