#!/usr/bin/env python3
"""
Hook Dispatcher — Level-1 in-process adapter for PreToolUse / PostToolUse plus the
Phase-2 events Stop / SessionStart / UserPromptSubmit / PostToolUseFailure (#1734).

VERSION: 1.5.0  (rp1-home#2212: plugin-entry guards — matcher evaluated under a catch; any exception
         except KeyboardInterrupt contained (SystemExit keeps its exit-code meaning); output
         type-checked before the merge, incl. updatedInput shape and no NaN/Infinity; core unchanged)

Runs the standalone hook scripts registered for an event IN-PROCESS (one CPython
interpreter per event instead of N subprocess spawns), replicating Claude Code's
native multi-hook merge, and exposes per-module timing as a dispatch-scoped
structure for downstream telemetry (#1733).

Telemetry batching (#1733): a dispatch opens a telemetry batch (begin_batch) before
its module loop, so every module's log_telemetry_event is CAPTURED rather than
POSTing; at dispatch-end one summary event is flushed (_flush_telemetry_batch) via a
synchronous bounded POST (rp1-home#2001; the #1566 atexit drain is retained as a
no-op — no telemetry thread is ever spawned). This collapses the ~25-30 per-Bash-call
telemetry POSTs to 1 per dispatch (2 per tool call: Pre + Post). Standalone hooks (run
outside the dispatcher) are unchanged.

Per-event merge families (#1734 extends the #1730/#1732 Pre/Post core):
- PreToolUse / PostToolUse — permissionDecision (allow/ask/deny) + mutation fields.
  UNCHANGED — the #1732-proven, live path, kept byte-identical.
- Stop / SubagentStop — a member blocks via a top-level {"decision":"block","reason"}
  + exit 0 (the whole Stop corpus uses this shape; ZERO use exit 2). The dispatcher
  aggregates: any member block wins, reasons are joined in registry order, and one
  top-level {"decision":"block","reason":...} JSON is emitted. No context channel.
- UserPromptSubmit / SessionStart / PostToolUseFailure — additionalContext from every
  firing member is concatenated in registry order; a member's raw NON-JSON stdout
  (e.g. mcp-default-disable's plain text on SessionStart) is folded in as context too.
  UserPromptSubmit ALSO honors a block (loop-cron-pause-gate exits 2 to swallow a
  paused-loop tick → emitted as a top-level {"decision":"block","reason":<stderr>});
  a block wins and SUPPRESSES context (native "prompt erased" semantics). SessionStart
  and PostToolUseFailure cannot block (native semantics), so their block channel is off.

The full DISPATCH path is not yet the live enforcement path — cutover is #1732.
#1731 adds a CAPTURE-ONLY shadow registration (`--shadow <event>`) that runs on
real traffic to record payloads for offline diffing WITHOUT ever blocking or
injecting a decision (see "Shadow-mode capture" below). Removing the shadow
settings.json group (or setting HOOK_DISPATCHER_SHADOW_DISABLE) plus deleting this
file and lib/hook_registry.py is a complete rollback with zero live-behavior impact.

Invocation:

    echo '<Pre/Post tool-event payload>' | python3 hook-dispatcher.py <event>
    echo '<Pre/Post tool-event payload>' | python3 hook-dispatcher.py --shadow <event>

where <event> is PreToolUse or PostToolUse. The payload is read from stdin ONCE.
The plain form dispatches + merges (the #1732 cutover path); the `--shadow` form is
capture-only (#1731) — it appends the raw payload to ~/.claude/logs/dispatch-shadow.jsonl
and ALWAYS exits 0 with no stdout.

Execution model per registry Entry (see lib/hook_registry.py):

- inprocess: inject the payload as sys.stdin, capture sys.stdout / sys.stderr,
  neutralize sys.argv (a hook that reads sys.argv[1] must not see the event name),
  then run the hook. Prefer module.main() under a cached import (the module's
  `if __name__ == '__main__'` guard is suppressed by loading it under a non-main
  __name__, so main() runs only when invoked explicitly). An INT returned by main()
  is captured as the exit code (#1734 — the `sys.exit(main())` guard idiom, e.g.
  loop-cron-pause-gate's `return 2`); a None/non-int return is exit 0. Fall back to
  runpy.run_path(run_name='__main__') for the ~2 hooks that define no callable
  main() and run their logic at module top level (telemetry-skill-detector,
  telemetry-agent-read-detector) — those run per-invocation and are NEVER cached,
  else their top-level logic would fire once per process instead of once per call.
  SystemExit is caught to read the exit code. (os._exit is absent across the whole
  corpus — verified — so SystemExit capture covers every current hook's exit path.)

- subprocess: run the script via subprocess.run(input=payload) — the sanctioned
  escape hatch for the one shell hook (check-approval-sentinel.sh) and any python
  hook that resists in-process execution.

Native merge semantics replicated (the D1-pinned table; epic #1729 R1/R2):

- deny wins if ANY module emits top-level {"decision":"block"|"deny"} + exit 0
  (the dominant corpus mechanism), hookSpecificOutput.permissionDecision == "deny",
  or SystemExit(2).
- permissionDecision precedence: deny > ask > allow. The reason attached is the
  FIRST (registry-order) module at the winning rank.
- additionalContext from every firing module is concatenated in registry order.
- systemMessage is merged (concatenated in registry order).
- a mutating hook's updatedInput (Pre) / updatedToolInput, updatedToolOutput (Post)
  — carried UNDER hookSpecificOutput — are propagated (last-writer-wins).
- an exit code other than 0 or 2 (e.g. version-bump-enforcer's exit 1 + stdout
  text) is NON-blocking under native semantics — fail-open. It is recorded in the
  per-module timing/result structure but is NOT folded into the merged decision
  (the D1 pin; remediation of the four exit-1 gates that use this as a "block"
  mechanism is a separate follow-up, out of epic scope).

The dispatcher itself always exits 0 and conveys a block via the merged JSON
(decision:"block" + permissionDecision:"deny"), never via its own exit 2.

Crash isolation: any exception from a module is logged loudly (block-queue idiom)
and the chain continues — fail-open for THAT module only, merged decision
unaffected (R3).

Timing budget: a single per-event chain budget is measured and a warning logged if
exceeded; no module is killed mid-run and there are no per-module timeouts (R4).

See: rp1-home#1730
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

from lib.hook_registry import Entry, get_entries, get_hook_path  # noqa: E402

# Loud crash logging via the block-queue idiom (fail-safe import; the logger
# self-suppresses under pytest, so test-induced crashes never pollute the queue).
try:
    from lib.block_logger import log_hook_block

    _BLOCK_LOGGER_AVAILABLE = True
except Exception:  # pragma: no cover - defensive import guard
    _BLOCK_LOGGER_AVAILABLE = False

# Telemetry batch flush (rp1-home#1733). A dispatch captures every module's
# log_telemetry_event into ONE batch and flushes a single summary event at
# dispatch-end, collapsing the per-Bash-call POST count (~25-30) to 1 per dispatch
# (2 per tool call: Pre + Post). Fail-safe import: telemetry is fire-and-forget and
# must never break a dispatch.
try:
    from lib import telemetry_logger

    _TELEMETRY_AVAILABLE = True
except Exception:  # pragma: no cover - defensive import guard
    _TELEMETRY_AVAILABLE = False

SUPPORTED_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUseFailure",
)

# Event-class families for merge routing (#1734). PreToolUse / PostToolUse fall
# through to the original tool-permission merge (unchanged). SubagentStop is
# grouped with Stop for forward-compat even though only Stop is registered by #1734.
_STOP_EVENTS = frozenset({"Stop", "SubagentStop"})
# Context-injecting events. Only UserPromptSubmit honors a block (see _merge_context);
# SessionStart and PostToolUseFailure cannot block under native semantics.
_CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "SessionStart", "PostToolUseFailure"})
_BLOCKING_CONTEXT_EVENTS = frozenset({"UserPromptSubmit"})

# One chain time budget per event (ms). Exceeding it logs a WARNING only — no module
# is killed and there are no per-module timeouts (R4). Tunable via env.
CHAIN_BUDGET_MS = int(os.environ.get("HOOK_DISPATCHER_CHAIN_BUDGET_MS", "10000"))

# ── Shadow-mode capture (#1731) ────────────────────────────────────────────────
# The shadow registration (settings.json, added post-#1730) runs this dispatcher in
# a CAPTURE-ONLY mode on every tool call during the #1732 cutover soak: it appends
# the raw payload to a JSONL and ALWAYS exits 0 with no stdout — it never runs the
# hook chain in-line (that would double-fire stateful hooks; #1731 correction #2),
# never blocks, and never injects a decision. The offline differ
# (.claude/scripts/hook-dispatcher-shadow-diff.py) replays each captured payload
# through BOTH the dispatcher and an independent reference chain to compare.
SHADOW_LOG_PATH = os.environ.get(
    "HOOK_DISPATCHER_SHADOW_LOG",
    os.path.expanduser("~/.claude/logs/dispatch-shadow.jsonl"),
)
# Kill-switch shared with #1733: set truthy to disable shadow capture instantly,
# without editing settings.json. Capture still exits 0 (byte-transparent) when off.
SHADOW_DISABLE_ENV = "HOOK_DISPATCHER_SHADOW_DISABLE"

# Native tool-name aliases the matcher treats as equivalent (recovered algorithm).
_ALIASES: dict[str, set[str]] = {
    "Task": {"Agent"},
    "Agent": {"Task"},
    "BashOutput": {"TaskOutput"},
    "TaskOutput": {"BashOutput"},
}

# Charset for the exact-name-list matcher branch: alnum, underscore, pipe, comma,
# space. It deliberately EXCLUDES the hyphen so "mcp__claude-in-chrome__" (a prefix
# matcher containing a hyphen) falls to the regex branch — matching the native
# binary algorithm. (The #1730 directive's charset TEXT listed '-', but its worked
# example states the hyphen routes to regex and the example governs; confirmed by
# the 32-of-46 Bash Pre selection, which only this split reproduces.)
_EXACT_LIST_RE = re.compile(r"[A-Za-z0-9_|, ]+")

# The last dispatch's per-module timing structure — a dispatch-scoped programmatic
# handle for an in-process caller (e.g. #1733's telemetry batch) that imports this
# module and calls dispatch() directly. dispatch() ALSO returns it.
LAST_DISPATCH_TIMINGS: list[dict] = []

_DECISION_RANK = {None: 0, "allow": 1, "ask": 2, "deny": 3}

# Sentinel distinguishing "mutation field absent" from an explicit None value.
_MISSING = object()

# Module caches. `_module_cache` holds imported has-main modules; `_has_main_cache`
# memoizes the AST probe per path. main()-less modules are NEVER cached (they run
# fresh via runpy each invocation).
_module_cache: dict[str, object] = {}
_has_main_cache: dict[str, bool] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Matcher engine (Task 6) — native PreToolUse/PostToolUse matcher algorithm
# ═══════════════════════════════════════════════════════════════════════════════
def matches(matcher: str, tool_name: str) -> bool:
    """Return True iff `matcher` selects `tool_name`, per the native algorithm.

    - "" or "*" → match all.
    - charset ^[A-Za-z0-9_|, ]+$ (NO hyphen) → exact-name alternation list on
      "|"/",": alias-aware EXACT equality per alternative. A plain re.search over
      "Edit|Write" would over-fire (MultiEdit / NotebookEdit / TodoWrite all
      contain "Edit"); re.fullmatch would break the chrome prefix matcher — hence
      the charset gate.
    - else → a real regex, UNANCHORED (re.search) — this preserves the
      "mcp__claude-in-chrome__" prefix matcher (it contains a hyphen, so it takes
      this branch, and search matches "mcp__claude-in-chrome__navigate").
    """
    if matcher == "" or matcher == "*":
        return True
    if _EXACT_LIST_RE.fullmatch(matcher):
        wanted: set[str] = set()
        for alt in (a.strip() for a in re.split(r"[|,]", matcher)):
            if not alt:
                continue
            wanted.add(alt)
            wanted |= _ALIASES.get(alt, set())
        candidates = {tool_name} | _ALIASES.get(tool_name, set())
        return bool(wanted & candidates)
    try:
        return re.search(matcher, tool_name) is not None
    except re.error:
        # A malformed matcher never crashes the chain — it simply matches nothing.
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Per-module result
# ═══════════════════════════════════════════════════════════════════════════════
class ModuleResult:
    """The outcome of executing one registry Entry."""

    def __init__(self, module: str, matcher: str, kind: str):
        self.module = module
        self.matcher = matcher
        self.kind = kind
        self.exit_code: int | None = None
        self.stdout: str = ""
        self.stderr: str = ""
        self.parsed: dict | None = None
        self.elapsed_ms: float = 0.0
        self.crashed: bool = False
        self.error: str | None = None

    def decision(self) -> str | None:
        """This module's contribution to the merged decision.

        Returns "deny" | "ask" | "allow" | None. SystemExit(2) and top-level
        {"decision":"block"|"deny"} both map to "deny"; permissionDecision maps
        straight through. A non-0/non-2 exit is NON-blocking → contributes no
        decision (None). A crashed module contributes nothing (None).
        """
        if self.crashed:
            return None
        if self.exit_code == 2:
            return "deny"
        parsed = self.parsed or {}
        top = parsed.get("decision")
        if top in ("block", "deny"):
            return "deny"
        hso = parsed.get("hookSpecificOutput")
        if isinstance(hso, dict):
            pd = hso.get("permissionDecision")
            if pd in ("deny", "ask", "allow"):
                return pd
        if top == "allow":
            return "allow"
        return None

    def timing(self) -> dict:
        """Dispatch-scoped programmatic timing record (R5, epic Finding 5).

        This is the per-module structure #1733's telemetry batch consumes — NOT
        just a log line. `elapsed_ms` is present for EVERY executed module.
        """
        return {
            "module": self.module,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "decision": self.decision(),
            "exit_code": self.exit_code,
            "crashed": self.crashed,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Module loading + execution
# ═══════════════════════════════════════════════════════════════════════════════
def _module_has_main(path: str) -> bool:
    """True iff the file defines a top-level `def main` (or `async def main`).

    Determined by a static AST parse — never by importing the module (importing a
    main()-less hook would run its top-level logic immediately, consuming stdin).
    Result is memoized per path.
    """
    if path in _has_main_cache:
        return _has_main_cache[path]
    result = False
    try:
        with open(path, "r") as fh:
            tree = ast.parse(fh.read(), filename=path)
        result = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
            for node in tree.body
        )
    except Exception:
        result = False
    _has_main_cache[path] = result
    return result


def _load_module(name: str, path: str):
    """Import a hook module (cached).

    __name__ is set to a non-"__main__" value before exec so the module's
    `if __name__ == "__main__"` guard does NOT fire on import — main() is invoked
    explicitly by the caller. Mirrors unified-prompt-dispatcher.load_detector_module.
    """
    if name in _module_cache:
        return _module_cache[name]
    module_name = name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    module.__name__ = module_name  # suppress the __main__ guard on import
    spec.loader.exec_module(module)
    _module_cache[name] = module
    return module


def _try_parse_json(text: str) -> dict | None:
    """Parse `text` as a JSON object, returning None on any failure or non-object.

    Hooks may print non-JSON to stdout (e.g. version-bump-enforcer's plain block
    message); that is not a decision and must not crash the merge.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _systemexit_code(code) -> int:
    """Map a ``SystemExit.code`` to an integer exit code.

    None → 0 (clean exit), an int → itself, anything else (a string message) → 1
    (natively a string exit code prints to stderr and means exit 1). Single source
    of truth for the mapping used by both the in-process and defensive catches.
    """
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def _run_inprocess(entry: Entry, payload: dict, result: ModuleResult) -> None:
    """Execute an in-process hook with stdin injected + stdout/stderr captured."""
    path = get_hook_path(entry.module)
    payload_json = json.dumps(payload)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    saved_stdin, saved_argv = sys.stdin, sys.argv
    try:
        sys.stdin = io.StringIO(payload_json)
        # Neutralize argv (length 1) so a hook reading sys.argv[1] can't see the
        # dispatcher's event argument (e.g. skill-change-lane-gate's --git-hook path).
        sys.argv = [path]
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                if _module_has_main(path):
                    module = _load_module(entry.module, path)
                    rv = module.main()  # type: ignore[attr-defined]
                    # Capture an int return as the exit code (#1734): a hook whose
                    # BLOCK mechanism is `return N` from main() — its __main__ guard
                    # being `sys.exit(main())`, which our non-__main__ import
                    # suppresses — MUST have that N honored, else the block is
                    # silently discarded. loop-cron-pause-gate's paused-tick swallow
                    # (`return 2` from main()) is exactly this shape. A None / non-int
                    # return is exit 0 (a bare `main()` guard's return is irrelevant).
                    # Verified no Pre/Post corpus hook returns a non-zero int from
                    # main(), so this is a byte no-op for the #1732-proven path.
                    result.exit_code = rv if isinstance(rv, int) else 0
                else:
                    # main()-less hook: run its top-level logic fresh each call.
                    # runpy uses a throwaway namespace and does NOT populate
                    # sys.modules/_module_cache, so the logic fires once per call.
                    runpy.run_path(path, run_name="__main__")
                    result.exit_code = 0
            except SystemExit as se:
                # A string exit code prints to stderr natively — surface it too.
                if se.code is not None and not isinstance(se.code, int):
                    err_buf.write(str(se.code))
                result.exit_code = _systemexit_code(se.code)
    finally:
        sys.stdin = saved_stdin
        sys.argv = saved_argv
        result.stdout = out_buf.getvalue()
        result.stderr = err_buf.getvalue()


def _run_subprocess(entry: Entry, payload: dict, result: ModuleResult) -> None:
    """Execute a subprocess-fallback hook via subprocess.run (R6).

    No per-module timeout is imposed (R4 — the chain budget is measure-and-warn,
    not a kill). The one registered subprocess hook (check-approval-sentinel.sh)
    terminates quickly.
    """
    path = get_hook_path(entry.module)
    cmd = [path] if os.access(path, os.X_OK) else ["bash", path]
    proc = subprocess.run(
        cmd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    result.exit_code = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""


def execute_entry(entry: Entry, payload: dict, event: str) -> ModuleResult:
    """Execute one Entry with crash isolation + timing.

    Any exception is contained here (fail-open for this module only, R3): it is
    logged loudly and the returned ModuleResult is marked crashed so the merge
    skips it. `elapsed_ms` is always recorded.
    """
    result = ModuleResult(entry.module, entry.matcher, entry.kind)
    start = time.monotonic()
    try:
        if entry.kind == "subprocess":
            _run_subprocess(entry, payload, result)
        else:
            _run_inprocess(entry, payload, result)
    except SystemExit as se:  # pragma: no cover - defensive (inner catch handles it)
        result.exit_code = _systemexit_code(se.code)
    except Exception as exc:  # crash isolation — fail-open for THIS module only
        result.crashed = True
        result.error = repr(exc)
        _log_crash(entry.module, exc, event)
    except BaseException as exc:
        # rp1-home#2212 R2-3: a PLUGIN hook raising asyncio.CancelledError,
        # GeneratorExit, a BaseExceptionGroup, … is contained like any crash. Core
        # entries keep their pre-#2212 behavior (the exception propagates).
        if isinstance(exc, KeyboardInterrupt) or not _is_plugin_entry(entry):
            raise
        result.crashed = True
        result.error = repr(exc)
        _log_crash(entry.module, exc, event)
    finally:
        result.elapsed_ms = (time.monotonic() - start) * 1000.0
    if result.parsed is None and result.stdout and not result.crashed:
        result.parsed = _try_parse_json(result.stdout)
    if _is_plugin_entry(entry) and not result.crashed:
        problem = _plugin_output_problem(result.parsed)
        if problem:
            # rp1-home#2212 R2-1: a wrong-typed field would raise inside the merge
            # (e.g. "\n\n".join over a non-str context), OUTSIDE per-module isolation,
            # and lose every core guard's output. The plugin contributes nothing.
            result.crashed = True
            result.error = f"malformed plugin output: {problem}"
            _log_crash(entry.module, ValueError(result.error), event)
    return result


def _is_plugin_entry(entry: Entry) -> bool:
    """True for a merged plugin-fragment entry, which carries an ABSOLUTE module path
    (KD-1); core entries carry a bare basename."""
    return isinstance(entry.module, str) and os.path.isabs(entry.module)


def _entry_matches(entry: Entry, tool_name: str, event: str) -> bool:
    """``matches()`` for one entry. A PLUGIN entry's matcher is evaluated under a catch
    (rp1-home#2212 R2-2) — this runs outside per-module isolation, and an uncaught
    error here would crash the whole dispatch. The loader already rejects matchers
    that fail to compile; this is the second layer. Core entries are unchanged."""
    if not _is_plugin_entry(entry):
        return matches(entry.matcher, tool_name)
    try:
        return matches(entry.matcher, tool_name)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        _log_crash(entry.module, exc, event)
        return False


# The fields the merge reads as strings, top level and under hookSpecificOutput.
_PLUGIN_OUTPUT_STR_FIELDS = ("decision", "reason", "additionalContext", "systemMessage")
_PLUGIN_HSO_STR_FIELDS = ("permissionDecision", "permissionDecisionReason", "additionalContext")


def _plugin_output_problem(parsed: dict | None) -> str | None:
    """Why a PLUGIN entry's parsed JSON output cannot be merged safely, or ``None``
    (rp1-home#2212 R2-1). ``_try_parse_json`` already guarantees a dict or None."""
    if parsed is None:
        return None
    for key in _PLUGIN_OUTPUT_STR_FIELDS:
        value = parsed.get(key)
        if value is not None and not isinstance(value, str):
            return f"{key} is {type(value).__name__}, expected str"
    # rp1-home#2212 R3-1: json.loads accepts NaN/Infinity (and 1e999 → inf), and the
    # merge copies updated* fields through verbatim — so one non-finite number makes the
    # dispatcher print bare NaN/Infinity, which is not valid JSON, and Claude Code would
    # discard the WHOLE merged output, core deny included.
    try:
        json.dumps(parsed, allow_nan=False)
    except ValueError:
        return "output contains a non-finite number (NaN/Infinity), which is not valid JSON"
    hso = parsed.get("hookSpecificOutput")
    if hso is None:
        return None
    if not isinstance(hso, dict):
        return f"hookSpecificOutput is {type(hso).__name__}, expected an object"
    for key in _PLUGIN_HSO_STR_FIELDS:
        value = hso.get(key)
        if value is not None and not isinstance(value, str):
            return f"hookSpecificOutput.{key} is {type(value).__name__}, expected str"
    # rp1-home#2212 R3-1: Claude Code's hook-output schema requires updatedInput to be
    # an object; a null or string value fails validation and the merged output (core
    # deny included) is dropped.
    if "updatedInput" in hso and not isinstance(hso["updatedInput"], dict):
        return (f"hookSpecificOutput.updatedInput is {type(hso['updatedInput']).__name__}, "
                "expected an object")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Merge (Task 3) — replicate native multi-hook merge
# ═══════════════════════════════════════════════════════════════════════════════
def _reason_of(result: ModuleResult) -> str | None:
    """Extract the blocking/ask reason from a module result."""
    if result.exit_code == 2:
        return (result.stderr or "").strip() or f"{result.module} exited 2 (block)"
    parsed = result.parsed or {}
    if parsed.get("decision") in ("block", "deny") and parsed.get("reason"):
        return parsed["reason"]
    hso = parsed.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecisionReason"):
        return hso["permissionDecisionReason"]
    return parsed.get("reason")


def merge_results(event: str, results: list[ModuleResult]) -> dict | None:
    """Fold module results into one merged decision (native semantics).

    Routes by event family (#1734): Stop/SubagentStop → :func:`_merge_stop`;
    UserPromptSubmit/SessionStart/PostToolUseFailure → :func:`_merge_context`;
    everything else (PreToolUse/PostToolUse) falls through to the original
    tool-permission merge below, kept byte-identical to the #1732-proven path.

    Returns the merged hook output dict, or None when nothing fired (byte-equivalent
    to "no hook produced output"). See the module docstring for the full merge
    contract.
    """
    if event in _STOP_EVENTS:
        return _merge_stop(event, results)
    if event in _CONTEXT_EVENTS:
        return _merge_context(event, results, allow_block=event in _BLOCKING_CONTEXT_EVENTS)

    contexts: list[str] = []
    system_messages: list[str] = []
    updated_input = _MISSING
    updated_tool_input = _MISSING
    updated_tool_output = _MISSING
    decision: str | None = None
    reason: str | None = None

    for result in results:
        if result.crashed:
            continue  # fail-open: a crashed module contributes nothing
        parsed = result.parsed or {}

        # Decision precedence: deny > ask > allow. The FIRST module (registry order)
        # to reach the winning rank supplies the reason.
        d = result.decision()
        if d is not None and _DECISION_RANK[d] > _DECISION_RANK[decision]:
            decision = d
            reason = _reason_of(result)

        # additionalContext — concatenated in registry order.
        ctx = _extract_context(parsed)
        if ctx:
            contexts.append(ctx)

        # systemMessage — merged in registry order.
        sm = parsed.get("systemMessage")
        if isinstance(sm, str) and sm:
            system_messages.append(sm)

        # Mutation fields (carried under hookSpecificOutput) — last-writer-wins.
        hso = parsed.get("hookSpecificOutput")
        if isinstance(hso, dict):
            if "updatedInput" in hso:
                updated_input = hso["updatedInput"]
            if "updatedToolInput" in hso:
                updated_tool_input = hso["updatedToolInput"]
            if "updatedToolOutput" in hso:
                updated_tool_output = hso["updatedToolOutput"]

    hso_out: dict = {"hookEventName": event}
    if decision is not None:
        hso_out["permissionDecision"] = decision
        if reason:
            hso_out["permissionDecisionReason"] = reason
    if contexts:
        hso_out["additionalContext"] = "\n\n".join(contexts)
    if updated_input is not _MISSING:
        hso_out["updatedInput"] = updated_input
    if updated_tool_input is not _MISSING:
        hso_out["updatedToolInput"] = updated_tool_input
    if updated_tool_output is not _MISSING:
        hso_out["updatedToolOutput"] = updated_tool_output

    # Nothing beyond the event name and no systemMessage → emit nothing.
    if list(hso_out.keys()) == ["hookEventName"] and not system_messages:
        return None

    merged: dict = {"hookSpecificOutput": hso_out}
    if system_messages:
        merged["systemMessage"] = "\n\n".join(system_messages)
    # Legacy top-level mirror for a deny — the dominant corpus block mechanism is
    # top-level {"decision":"block"}, so emit both forms (consistent) to maximize
    # native compatibility.
    if decision == "deny":
        merged["decision"] = "block"
        if reason:
            merged["reason"] = reason
    return merged


def _extract_context(parsed: dict) -> str | None:
    """Extract additionalContext from a hook result (both output shapes)."""
    hso = parsed.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if ctx:
            return ctx
    ctx = parsed.get("additionalContext")
    return ctx or None


# ═══════════════════════════════════════════════════════════════════════════════
# Phase-2 event merges (#1734) — Stop and context-injecting families
# ═══════════════════════════════════════════════════════════════════════════════
def _merge_stop(event: str, results: list[ModuleResult]) -> dict | None:
    """Merge Stop / SubagentStop results (#1734, Task 0 — the 1734-1 shape).

    The whole Stop corpus blocks via a top-level {"decision":"block","reason":...}
    + exit 0 (ZERO use exit 2; ``ModuleResult.decision()`` maps both forms to
    "deny"). Aggregation: any member block wins; reasons are joined in registry
    order; the result is emitted as ONE top-level {"decision":"block","reason":...}
    JSON — NOT a permissionDecision (Stop has no permission channel). Stop has no
    additionalContext channel either, so only ``systemMessage`` (a warn channel some
    Stop hooks may use) is merged alongside the block. Returns None when no member
    blocks and no systemMessage is present (byte-equivalent to "nothing fired").
    """
    reasons: list[str] = []
    system_messages: list[str] = []
    blocked = False
    for result in results:
        if result.crashed:
            continue  # fail-open: a crashed module contributes nothing (R3)
        if result.decision() == "deny":
            blocked = True
            reason = _reason_of(result)
            if reason:
                reasons.append(reason)
        sm = (result.parsed or {}).get("systemMessage")
        if isinstance(sm, str) and sm:
            system_messages.append(sm)

    if not blocked and not system_messages:
        return None
    merged: dict = {}
    if blocked:
        merged["decision"] = "block"
        if reasons:
            merged["reason"] = "\n\n".join(reasons)
    if system_messages:
        merged["systemMessage"] = "\n\n".join(system_messages)
    return merged or None


def _merge_context(event: str, results: list[ModuleResult], allow_block: bool) -> dict | None:
    """Merge context-injecting events — UserPromptSubmit / SessionStart /
    PostToolUseFailure (#1734, Task 0 — the 1734-2 / 1734-3 shapes).

    additionalContext from every firing member is concatenated in registry order,
    emitted under ``hookSpecificOutput`` with the event's ``hookEventName``. A
    member's raw NON-JSON stdout (e.g. mcp-default-disable's plain text on
    SessionStart) is folded in as context too — the native shape for these events
    treats plain stdout as context, unlike PreToolUse/PostToolUse where it is a
    non-decision (this is exactly why the merge is event-class-aware).

    ``allow_block`` is True only for UserPromptSubmit: loop-cron-pause-gate exits 2
    (stderr = reason) to swallow a paused-loop tick, which the dispatcher forwards as
    a top-level {"decision":"block","reason":...} (the dispatcher always exits 0, so
    a block MUST travel via JSON). A block wins and SUPPRESSES context — matching the
    native semantics where a blocked prompt is erased and never reaches Claude. The
    first blocker (registry order) supplies the reason. SessionStart and
    PostToolUseFailure pass ``allow_block=False`` (they cannot block natively).
    """
    contexts: list[str] = []
    system_messages: list[str] = []
    decision: str | None = None
    reason: str | None = None

    for result in results:
        if result.crashed:
            continue  # fail-open: a crashed module contributes nothing (R3)
        parsed = result.parsed or {}

        if allow_block and result.decision() == "deny":
            if decision is None:  # first blocker (registry order) supplies the reason
                decision = "block"
                reason = _reason_of(result)
            continue  # a blocking member contributes no context

        ctx = _extract_context(parsed)
        if ctx is None and result.parsed is None and (result.stdout or "").strip():
            # Plain-text stdout → context (non-JSON stdout is None-parsed above).
            ctx = result.stdout.rstrip("\n")
        if ctx:
            contexts.append(ctx)

        sm = parsed.get("systemMessage")
        if isinstance(sm, str) and sm:
            system_messages.append(sm)

    # Block wins → suppress context (native "prompt erased" semantics for UPS).
    if decision == "block":
        merged: dict = {"decision": "block"}
        if reason:
            merged["reason"] = reason
        return merged

    hso_out: dict = {"hookEventName": event}
    if contexts:
        hso_out["additionalContext"] = "\n\n".join(contexts)
    merged = {}
    if list(hso_out.keys()) != ["hookEventName"]:
        merged["hookSpecificOutput"] = hso_out
    if system_messages:
        merged["systemMessage"] = "\n\n".join(system_messages)
    return merged or None


# ═══════════════════════════════════════════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════════════════════════════════════════
def dispatch(event: str, payload: dict) -> tuple[dict | None, list[dict]]:
    """Run every matching entry for `event`, returning (merged_output, timings).

    `timings` is the dispatch-scoped programmatic structure (R5 / epic Finding 5)
    consumed by #1733's telemetry batch — a list of per-module records
    {module, elapsed_ms, decision, exit_code, crashed}. It is ALSO stashed on the
    module global LAST_DISPATCH_TIMINGS for an in-process caller.
    """
    global LAST_DISPATCH_TIMINGS
    tool_name = payload.get("tool_name", "") or ""
    # rp1-home#2212: get_entries() is merge-aware — it appends any enabled plugin
    # fragment's EVENT_ENTRIES after the core literal (lib.plugin_fragments.
    # merged_event_entries). A merged fragment entry is an ordinary Entry carrying a
    # RESOLVED ABSOLUTE path in .module, and get_hook_path() returns that path verbatim
    # (KD-1), so _run_inprocess/_run_subprocess import from the plugin root. A plugin
    # entry gets three guards core entries do not need (R5 — a broken plugin must
    # never disable a core safety guard): its matcher is evaluated under a catch
    # (_entry_matches, R2-2), any exception from it other than KeyboardInterrupt is
    # contained like a crash (execute_entry, R2-3 — SystemExit keeps its exit-code
    # meaning, exactly as for a core hook, so sys.exit(2) blocks), and its output is
    # type-checked before the merge reads it (_plugin_output_problem, R2-1/R3-1).
    # Core entries run exactly as before.
    # Note get_entries() runs HERE, BEFORE the begin_batch()/try below, so discovery
    # is OUTSIDE this crash isolation and telemetry batch — the loader's own fail-open
    # (Task 1) is what contains a discovery error, not execute_entry's per-module catch.
    entries = [e for e in get_entries(event) if _entry_matches(e, tool_name, event)]

    results: list[ModuleResult] = []
    # rp1-home#1733: open the telemetry batch BEFORE the module loop (and before the
    # first hook import) so every module's log_telemetry_event — import-time OR
    # main()-time — is captured instead of issuing its own POST. end_batch() + the
    # single flush run in the finally so a mid-loop exception can never strand
    # captured events. Since rp1-home#2001 the flush POST is synchronous and spawns
    # no thread (the #1566 drain is vestigial). The dispatcher emits no telemetry of
    # its own, so there is no ordering constraint to satisfy relative to begin_batch().
    if _TELEMETRY_AVAILABLE:
        telemetry_logger.begin_batch()
    try:
        chain_start = time.monotonic()
        for entry in entries:
            results.append(execute_entry(entry, payload, event))
        chain_ms = (time.monotonic() - chain_start) * 1000.0
        if chain_ms > CHAIN_BUDGET_MS:
            _log_chain_budget_exceeded(event, chain_ms, len(entries))

        merged = merge_results(event, results)
        timings = [r.timing() for r in results]
        LAST_DISPATCH_TIMINGS = timings
        return merged, timings
    finally:
        if _TELEMETRY_AVAILABLE:
            _flush_telemetry_batch(event, results)


# ═══════════════════════════════════════════════════════════════════════════════
# Logging helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _log_crash(module: str, exc: BaseException, event: str) -> None:
    """Log a module crash loudly (stderr + block-queue), per R3."""
    msg = f"hook-dispatcher: module {module!r} crashed during {event}: {exc!r}"
    print(msg, file=sys.stderr)
    if _BLOCK_LOGGER_AVAILABLE:
        try:
            log_hook_block(
                hook="hook-dispatcher",
                reason=msg,
                tool_name=event,
                reason_class="dispatcher_module_crash",
            )
        except Exception:
            pass


def _log_chain_budget_exceeded(event: str, chain_ms: float, module_count: int) -> None:
    """Warn (stderr) when the per-event chain budget is exceeded — never kills (R4)."""
    print(
        f"hook-dispatcher: chain budget exceeded for {event}: {chain_ms:.1f}ms "
        f"over {module_count} modules (budget {CHAIN_BUDGET_MS}ms)",
        file=sys.stderr,
    )


def _emit_timings_log(event: str, timings: list[dict]) -> None:
    """Emit the per-module timing structure to stderr as one JSON line.

    Gated on HOOK_DISPATCHER_DEBUG so the (not-yet-live) dispatcher stays quiet by
    default; when set, `elapsed_ms` for every executed module appears in the
    dispatcher's log output (the AC-3 companion of the AC-4 return structure).
    """
    if os.environ.get("HOOK_DISPATCHER_DEBUG"):
        print(
            "hook-dispatcher timings "
            + json.dumps({"event": event, "modules": timings}),
            file=sys.stderr,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Telemetry batch flush (#1733) — one summary event per dispatch
# ═══════════════════════════════════════════════════════════════════════════════
def _aggregate_decision(results: list[ModuleResult]) -> str | None:
    """The dispatch's aggregate permission decision (deny > ask > allow > None).

    Reuses ModuleResult.decision() + the module _DECISION_RANK so the batched
    telemetry event's aggregate matches the merge's precedence.
    """
    best: str | None = None
    for result in results:
        d = result.decision()
        if d is not None and _DECISION_RANK[d] > _DECISION_RANK[best]:
            best = d
    return best


def _flush_telemetry_batch(event: str, results: list[ModuleResult]) -> None:
    """End the telemetry batch and flush ONE summary event for the dispatch (#1733).

    The single event carries the dispatch event name, the hooks executed, per-module
    elapsed-ms, the aggregate decision, and the modules' captured payloads — all in
    the additive ``context`` field. This is server-compatible (R4): the
    HAPImatic server's telemetry route persists ``context`` to ``context_json`` and
    existing consumers query only the indexed columns (file_path/file_type/
    event_type/duration_ms/outcome), all of which the summary event populates.

    The flush re-enters log_telemetry_event with the batch now INACTIVE (end_batch()
    ran first), so it issues a synchronous bounded POST (~2×_SYNC_TIMEOUT;
    rp1-home#2001). Since #2001 no telemetry thread is ever spawned — the #1566 atexit
    drain is retained as a no-op — so the dispatcher's merged decision returns after
    this bounded POST completes.

    Emits NOTHING when no module captured an event (a zero-telemetry dispatch), so
    batching never INCREASES the POST count over today's per-invocation behavior.
    Never raises — telemetry is fire-and-forget and must never disrupt a dispatch.
    """
    try:
        captured = telemetry_logger.end_batch()
        if not captured:
            return  # nothing to batch → no POST (byte-equivalent to today's zero)
        timings = [r.timing() for r in results]
        context = {
            "batch": True,
            "dispatchEvent": event,
            "hooksExecuted": [t["module"] for t in timings],
            "perModuleElapsedMs": {t["module"]: t["elapsed_ms"] for t in timings},
            "moduleTimings": timings,
            "aggregateDecision": _aggregate_decision(results),
            "capturedEventCount": len(captured),
            "capturedEvents": captured,
        }
        telemetry_logger.log_telemetry_event(
            file_path=__file__,
            file_type="hook",
            event_type="invocation",
            context=context,
        )
    except Exception:
        pass  # fire-and-forget — a telemetry failure must never break a dispatch


# ═══════════════════════════════════════════════════════════════════════════════
# Shadow-mode capture (#1731) — capture-only; NEVER dispatches / blocks / injects
# ═══════════════════════════════════════════════════════════════════════════════
def _utc_now_iso() -> str:
    """UTC timestamp (ms precision, Z-suffixed) — the repo JSONL-row convention."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _shadow_digest(payload: dict) -> str:
    """Stable short digest of a payload — a dedup key for the differ ONLY.

    A digest is NEVER a substitute for the full payload (a digest-only row is not
    replayable, #1731 R8); it is additive metadata for collapsing identical captures.
    """
    try:
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canon = repr(payload)
    return hashlib.sha1(canon.encode("utf-8", "replace")).hexdigest()[:16]


def append_shadow_row(event: str, payload: dict, path: str | None = None) -> None:
    """Append one capture-only row ``{ts, event, payload, digest}`` to the shadow JSONL.

    FULL payload (replayable) per #1731 R8; ``digest`` is additive dedup metadata. The
    caller (:func:`run_shadow`) swallows every exception, so this helper stays minimal.
    """
    target = path or SHADOW_LOG_PATH
    row = {
        "ts": _utc_now_iso(),
        "event": event,
        "payload": payload,
        "digest": _shadow_digest(payload),
    }
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _shadow_disabled() -> bool:
    """True iff the #1733-shared kill-switch env is set to a truthy value."""
    return os.environ.get(SHADOW_DISABLE_ENV, "").strip() not in ("", "0", "false", "False")


def run_shadow(event: str | None, stdin=None, path: str | None = None) -> int:
    """Capture-only shadow entry (#1731 R1). ALWAYS returns 0 with NO stdout.

    A shadow registration runs on EVERY tool call machine-wide during the #1732
    soak, so this path is byte-transparent to Claude Code by construction: it never
    writes stdout (which could inject a decision), never returns non-zero (which
    could block), and swallows every error (fail-open). It does NOT run the hook
    chain — capture only; the differ replays offline (#1731 correction #2).
    """
    if _shadow_disabled():  # kill-switch armed → capture nothing, still exit 0
        return 0
    try:
        stream = stdin if stdin is not None else sys.stdin
        payload = json.load(stream)
        if isinstance(payload, dict):
            append_shadow_row(event or "", payload, path)
    except Exception:
        # Fail-open — a shadow capture failure must NEVER affect the live session.
        pass
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    """Read the event from argv[1] and the payload from stdin, dispatch, print merged.

    Always returns 0: a block is conveyed through the merged JSON (decision:"block"
    + permissionDecision:"deny"), never through the dispatcher's own exit code.
    Malformed input fails open (no-op, exit 0).
    """
    args = sys.argv if argv is None else argv
    # Parse a --shadow / bare-'shadow' flag out; the first non-flag token is the event.
    shadow = False
    positional: list[str] = []
    for token in args[1:]:
        if token in ("--shadow", "shadow"):
            shadow = True
        else:
            positional.append(token)
    event = positional[0] if positional else None

    if shadow:
        # Capture-only mode (#1731): never dispatches, never blocks, never injects.
        return run_shadow(event)

    if event is None:
        print(
            "hook-dispatcher: missing event argument (" + "|".join(SUPPORTED_EVENTS) + ")",
            file=sys.stderr,
        )
        return 0
    if event not in SUPPORTED_EVENTS:
        print(f"hook-dispatcher: unsupported event {event!r}", file=sys.stderr)
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, EOFError) as exc:
        print(f"hook-dispatcher: invalid JSON payload: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("hook-dispatcher: payload is not a JSON object", file=sys.stderr)
        return 0

    merged, timings = dispatch(event, payload)
    _emit_timings_log(event, timings)
    if merged is not None:
        print(json.dumps(merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
