#!/usr/bin/env python3
# HOOK_VERSION: 1.3.0
# HOOK_EVENT: UserPromptSubmit
# HOOK_MATCHER:
# HOOK_GROUP: unified-dispatcher
"""
Unified Prompt Dispatcher — single-process replacement for 16 UserPromptSubmit hooks.

Instead of spawning 16 separate Python processes per user message, this dispatcher:
1. Reads stdin once
2. Applies universal filters (sentinel keyword, meta-discussion)
3. Runs priority-group detectors in order (first match wins)
4. Runs independent-group detectors (all can fire)
5. Combines triggered contexts into a single output

Performance: ~30-50ms single CPython startup vs ~480-800ms for 16 startups.
Memory: ~40MB RSS vs ~240-320MB transient for 16 processes.

See: rp1-home#430
"""

import contextlib
import importlib.util
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone

# Add hooks directory to path for lib imports
HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOKS_DIR)

# Import shared utilities
try:
    from lib.meta_detection import is_meta_discussion
    META_DETECTION_AVAILABLE = True
except ImportError:
    META_DETECTION_AVAILABLE = False

try:
    from lib import detector_registry as _detector_registry_mod
    from lib.detector_registry import PRIORITY_GROUP, INDEPENDENT_GROUP, get_detector_path
    REGISTRY_AVAILABLE = True
except ImportError:
    _detector_registry_mod = None
    REGISTRY_AVAILABLE = False

# rp1-home #974/#1092: shared orchestration markers. Fail-open (availability flag)
# is deliberate — this is the SOLE registered UserPromptSubmit hook, so a hard
# import that failed would crash ALL prompt processing. The approval detector
# remains the authoritative backstop (it imports the same markers and gates
# sentinel-minting independently).
try:
    from lib.orchestration_markers import has_orchestration_marker
    ORCH_MARKER_AVAILABLE = True
except ImportError:
    ORCH_MARKER_AVAILABLE = False

# Telemetry (fire-and-forget)
try:
    from lib.telemetry_logger import log_telemetry_event
    log_telemetry_event(__file__, "hook")
except Exception:
    pass


# ═══════════════════════════════════════════════════════════════════════
# Module Loading
# ═══════════════════════════════════════════════════════════════════════

_module_cache: dict[str, object] = {}


def load_detector_module(name: str, path: str | None = None) -> object | None:
    """Load a detector module by name using importlib.

    Uses file-path-based loading since detector filenames contain hyphens
    (not valid Python identifiers). Modules are cached after first load.

    rp1-home#2212 (Task 6): an explicit ``path`` (a merged plugin-fragment detector's
    absolute ``path``) is honoured directly; core detectors pass ``path=None`` and
    resolve via ``get_detector_path(name)`` exactly as before. The cache key stays
    ``name`` (fragment detectors that duplicate a core name are rejected/shadowed by
    the loader, so names are unique in the merged independent list).
    """
    if name in _module_cache:
        return _module_cache[name]

    is_fragment = bool(path)  # captured before `path` is resolved for core detectors
    if path:
        pass  # explicit fragment path (already absolute) — use as given
    elif REGISTRY_AVAILABLE:
        path = get_detector_path(name)
    else:
        path = os.path.join(HOOKS_DIR, f"{name}.py")

    if not os.path.exists(path):
        # R4 (#2006): keep the not-found line but dedup it per session. The drift
        # marker (via check_detector_drift) is the dedup record; _stderr_surfaced_names
        # holds names surfaced in a PRIOR prompt. None ⇒ the drift check did not run
        # ⇒ original always-print behavior (byte-identical to pre-#2006 on any failure).
        # This stderr line is a DEAD channel under hook-dispatcher.py (buffered, read
        # only on exit 2 while UPD always exits 0) — a courtesy for direct subprocess
        # invocation only; R2's banner carries the operator signal.
        if _stderr_surfaced_names is None or name not in _stderr_surfaced_names:
            print(f"Dispatcher: detector not found: {path}", file=sys.stderr)
            if _stderr_surfaced_names is not None:
                _stderr_surfaced_names.add(name)
        return None

    try:
        # Convert hyphenated name to valid module name
        module_name = name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # Prevent the module's if __name__ == "__main__" from running
        module.__name__ = module_name
        if is_fragment:
            # rp1-home#2212 IR-1: a plugin detector's import-time print must not reach
            # this dispatcher's stdout (its JSON output channel).
            with contextlib.redirect_stdout(sys.stderr):
                spec.loader.exec_module(module)
        else:
            spec.loader.exec_module(module)
        _module_cache[name] = module
        return module
    except Exception as e:
        print(f"Dispatcher: failed to load {name}: {e}", file=sys.stderr)
        return None
    except BaseException as e:
        if not is_fragment or isinstance(e, KeyboardInterrupt):
            raise  # core detectors keep their pre-#2212 behavior unchanged
        # rp1-home#2212 IR-1 / R2-3: a plugin detector raising SystemExit (or any other
        # BaseException) at import must not take down the prompt dispatcher and every
        # core detector after it.
        print(f"Dispatcher: plugin detector {name} {_exit_description(e)} at import — skipped", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════
# Core Dispatch Logic
# ═══════════════════════════════════════════════════════════════════════

def run_detector(name: str, prompt: str, input_data: dict, path: str | None = None) -> dict | None:
    """Run a single detector's run_detection() function.

    Returns the detector's output dict if triggered, None otherwise.
    Errors are caught and logged to stderr — a failing detector never blocks others.

    rp1-home#2212 (Task 6): ``path`` threads a merged plugin-fragment detector's
    absolute path through to ``load_detector_module``; core detectors pass ``None``.
    """
    module = load_detector_module(name, path)
    if module is None:
        return None

    run_fn = getattr(module, "run_detection", None)
    if run_fn is None:
        print(f"Dispatcher: {name} has no run_detection() function", file=sys.stderr)
        return None

    try:
        if path:
            # rp1-home#2212 IR-1: a plugin detector's print stays off our stdout.
            with contextlib.redirect_stdout(sys.stderr):
                result = run_fn(prompt, input_data)
        else:
            result = run_fn(prompt, input_data)
    except Exception as e:
        print(f"Dispatcher: {name}.run_detection() failed: {e}", file=sys.stderr)
        return None
    except BaseException as e:
        if not path or isinstance(e, KeyboardInterrupt):
            raise  # core detectors keep their pre-#2212 behavior unchanged
        print(f"Dispatcher: plugin detector {name}.run_detection() {_exit_description(e)} — skipped", file=sys.stderr)
        return None
    if path:
        # rp1-home#2212 R2-1: dispatch() reads the result with .get() and joins the
        # contexts OUTSIDE this function's try — a wrong-shaped plugin result would
        # raise there and lose every core detector's context for the prompt.
        problem = _plugin_result_problem(result)
        if problem:
            print(f"Dispatcher: plugin detector {name} returned a malformed result ({problem}) — skipped", file=sys.stderr)
            return None
    return result


def _exit_description(exc: BaseException) -> str:
    """How a plugin detector ended, for its skip line."""
    if isinstance(exc, SystemExit):
        return f"called sys.exit({exc.code!r})"
    return f"raised {exc!r}"


def _plugin_result_problem(result) -> str | None:
    """Why a PLUGIN detector's result cannot be read by dispatch(), or ``None``
    (rp1-home#2212 R2-1). A falsy result is "did not fire" and always fine."""
    if not result:
        return None
    if not isinstance(result, dict):
        return f"result is {type(result).__name__}, expected a dict"
    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if ctx and not isinstance(ctx, str):
            return f"hookSpecificOutput.additionalContext is {type(ctx).__name__}, expected str"
    ctx = result.get("additionalContext")
    if ctx and not isinstance(ctx, str):
        return f"additionalContext is {type(ctx).__name__}, expected str"
    return None


def extract_additional_context(result: dict) -> str | None:
    """Extract additionalContext from a detector's output dict.

    Handles both output formats:
    - {"hookSpecificOutput": {"additionalContext": "..."}}
    - {"additionalContext": "..."}
    """
    if not result:
        return None

    # Preferred: hookSpecificOutput wrapper
    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if ctx:
            return ctx

    # Fallback: bare additionalContext
    ctx = result.get("additionalContext")
    if ctx:
        return ctx

    return None


def dispatch(prompt: str, input_data: dict) -> dict | None:
    """Run all detectors and combine results.

    Priority group: first match wins (mutually exclusive).
    Independent group: all matches fire.
    """
    if not REGISTRY_AVAILABLE:
        print("Dispatcher: detector_registry not available", file=sys.stderr)
        return None

    triggered_contexts: list[str] = []

    # rp1-home#2212 (Task 6): consume the MERGED detector groups so an enabled plugin
    # fragment's INDEPENDENT detectors run after the core independent list (R6). The
    # merge accessors are themselves fail-open to the core literals; the extra try here
    # is the directive's belt-and-suspenders ("degrade to core-only if the merge
    # raises"). The PRIORITY group is returned UNCHANGED (KD-2, Position 2), so
    # first-match routing is byte-identical to the core literal.
    try:
        priority_group = _detector_registry_mod.get_priority_group()
        independent_group = _detector_registry_mod.get_independent_group()
    except Exception as exc:
        priority_group = PRIORITY_GROUP
        independent_group = INDEPENDENT_GROUP
        # rp1-home#2613 (RP-F6): still core-only, but never silently — one loud report
        # per process (stderr + block queue), shared with the registry fallbacks.
        try:
            from lib.hook_registry import _report_loader_unavailable
            _report_loader_unavailable("unified-prompt-dispatcher.dispatch", exc)
        except Exception:
            pass

    # Priority group: run in priority order, first match wins
    for detector in priority_group:
        result = run_detector(
            detector["name"], prompt, input_data, path=detector.get("path")
        )
        if result is not None:
            ctx = extract_additional_context(result)
            if ctx:
                triggered_contexts.append(ctx)
            break  # Only the highest-priority match fires

    # Independent group: all can fire
    for detector in independent_group:
        result = run_detector(
            detector["name"], prompt, input_data, path=detector.get("path")
        )
        if result is not None:
            ctx = extract_additional_context(result)
            if ctx:
                triggered_contexts.append(ctx)

    if not triggered_contexts:
        return None

    # Combine all triggered contexts
    combined_context = "\n\n".join(triggered_contexts)

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": combined_context,
        }
    }


# ═══════════════════════════════════════════════════════════════════════
# Detector Drift Check (rp1-home #2006)
# ═══════════════════════════════════════════════════════════════════════
#
# Surface a MISSING registered detector LOUDLY, ONCE PER SESSION. When the
# Configuration Porter drops detector files on a host (the private product repo #3590),
# every workflow phrase (a USER-APPROVAL token paste, work on issue #N, compaction
# reload, session self-context, …) silently does nothing. Today the only signal is a per-prompt
# stderr line NOBODY reads: under hook-dispatcher.py member stderr is buffered and
# read only on exit 2, while this dispatcher always exits 0 — a missing detector is
# FULLY SILENT (not "one buried line per prompt"). R2's banner carries the signal.
#
# TWO non-negotiable safety properties (R7 / KD2):
#   1. This check can NEVER disable the dispatcher — its caller wraps it in a broad
#      try/except; it uses os.path + importlib ONLY (no subprocess — the #2001 crash
#      class); and it NEVER gates the detector loop.
#   2. The banner MUST live INSIDE the single JSON object the dispatcher already
#      prints (KD2(c)). A bare print() banner would make stdout `banner + JSON`,
#      defeating hook-dispatcher.py's _try_parse_json → _merge_context folds the raw
#      blob in as context while SILENTLY DISCARDING every detector decision for that
#      prompt — a regression far worse than the drift.
#
# The drift marker (~/.claude/state/detector-drift-<session>.json) doubles as the
# once-per-session dedup sentinel (KD3): the banner names only detectors not already
# recorded, and the per-prompt stderr line (Task 2) is deduped against the same set.
# ZERO filesystem writes on a healthy tree (KD6).

DRIFT_MARKER_SCHEMA_VERSION = 1

# Detector names already surfaced (stderr) in a PRIOR prompt this session —
# populated by check_detector_drift() so load_detector_module() dedups its
# not-found line (R4/Task 2). None ⇒ the drift check did not run (registry
# unavailable / it raised / a healthy tree) ⇒ load_detector_module keeps its
# original always-print behavior (byte-identical to pre-#2006 on any failure).
_stderr_surfaced_names: "set[str] | None" = None


def _drift_state_dir() -> str:
    """Directory for drift markers. Env-overridable (DETECTOR_DRIFT_STATE_DIR) so
    tests never write into the real ~/.claude/state (KD3; mirrors the
    _state_base() env-override idiom in lib/approval_dual_accept.py)."""
    override = os.environ.get("DETECTOR_DRIFT_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "state")


def _resolve_drift_session_id(input_data: dict) -> "str | None":
    """Session id for the per-session marker (KD3/OR3): the hook payload's
    session_id FIRST (present in every Claude Code hook payload), then
    HAPI_SESSION_ID, then the transcript-path stem. None if all are unavailable —
    the check then degrades to 'scan + banner, no persistent dedup' (never crash)."""
    sid = input_data.get("session_id")
    if sid:
        return str(sid)
    sid = os.environ.get("HAPI_SESSION_ID")
    if sid:
        return str(sid)
    transcript = input_data.get("transcript_path")
    if transcript:
        stem = os.path.splitext(os.path.basename(str(transcript)))[0]
        if stem:
            return stem
    return None


def _registry_detector_names() -> "list[str]":
    """All registered detector names (PRIORITY_GROUP + INDEPENDENT_GROUP)."""
    names: "list[str]" = []
    for group in (PRIORITY_GROUP, INDEPENDENT_GROUP):
        for detector in group:
            name = detector.get("name")
            if name:
                names.append(name)
    return names


def _missing_detectors() -> "list[str]":
    """Registered detector names whose file is absent on disk (os.path only)."""
    return [
        name
        for name in _registry_detector_names()
        if not os.path.exists(get_detector_path(name))
    ]


def _drift_marker_path(session_id: str) -> str:
    return os.path.join(_drift_state_dir(), f"detector-drift-{session_id}.json")


def _read_drift_surfaced(session_id: "str | None") -> "set[str] | None":
    """Detector names already recorded in this session's marker. None when the
    marker is absent (or the session id is unknown / the file is unreadable) —
    signalling 'nothing surfaced yet this session'."""
    if not session_id:
        return None
    path = _drift_marker_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    recorded = data.get("missing")
    if isinstance(recorded, list):
        return {str(n) for n in recorded}
    return set()


def _write_drift_marker(session_id: str, missing: "list[str]", registry_path: str) -> None:
    """Write/rewrite the versioned drift marker (KD5 consumer schema for
    the private product repo #3551 — keep it small and versioned)."""
    state_dir = _drift_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    marker = {
        "schema_version": DRIFT_MARKER_SCHEMA_VERSION,
        "host": socket.gethostname(),
        "session_id": session_id,
        "missing": sorted(missing),
        "registry_path": registry_path,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_drift_marker_path(session_id), "w", encoding="utf-8") as fh:
        json.dump(marker, fh, indent=2)


def _build_drift_banner(newly_missing: "list[str]") -> str:
    """The loud banner naming the missing detectors + likely cause (R2)."""
    names = "\n".join(f"  - {n}" for n in sorted(newly_missing))
    return (
        "🔴 DETECTOR DRIFT — registered prompt detector(s) MISSING on this host\n"
        f"{len(newly_missing)} detector(s) listed in lib/detector_registry.py have no "
        "file under ~/.claude/hooks/:\n"
        f"{names}\n"
        "Likely cause: a partial config sync / Configuration Porter drop "
        "(the private product repo #3590). Workflow phrases handled by these detectors "
        "(a USER-APPROVAL token paste, work on issue #N, compaction reload, session self-context, …) "
        "will SILENTLY DO NOTHING until the files are restored. Confirm with "
        "`python3 ~/.claude/hooks/lib/hook_registration_validator.py`."
    )


def check_detector_drift(input_data: dict) -> "str | None":
    """Once-per-session detector-drift surfacing. Returns a banner string for the
    dispatcher to fold into its JSON output (R2), or None.

    ADVISORY + FAIL-OPEN (R7): the caller wraps this in try/except so any error
    yields no banner and NEVER blocks the detector loop. os.path + importlib ONLY —
    no subprocess (the #2001 crash class).

    Side effects occur ONLY when detectors are actually missing (KD6 — a healthy
    tree touches NOTHING): it sets the module-level stderr-dedup set (R4/Task 2) and
    writes the versioned drift marker (R3). The marker doubles as the per-session
    dedup record (KD3) — the banner names only detectors not already recorded, so it
    fires once per session and a detector that goes missing mid-session still
    surfaces exactly once."""
    global _stderr_surfaced_names

    if not REGISTRY_AVAILABLE:
        return None

    missing = _missing_detectors()
    if not missing:
        return None  # KD6: byte-identical on a healthy tree — touch NOTHING.

    session_id = _resolve_drift_session_id(input_data)
    surfaced = _read_drift_surfaced(session_id)
    already: "set[str]" = surfaced if surfaced is not None else set()

    # R4/Task 2: hand load_detector_module the set of names surfaced in a PRIOR
    # prompt so its not-found line dedups per session. On the first prompt `already`
    # is empty → each missing name gets exactly one stderr line as the loop hits it.
    _stderr_surfaced_names = set(already)

    newly = [name for name in missing if name not in already]
    if not newly:
        return None  # every currently-missing detector was already surfaced.

    registry_path = getattr(_detector_registry_mod, "__file__", "") or ""

    # Persist the dedup record + consumer marker (KD3/KD5). If the session id is
    # unknown we degrade to 'banner every prompt, no persistent dedup' (OR3); a
    # marker-write failure must never suppress the banner or crash (R7).
    if session_id:
        try:
            _write_drift_marker(session_id, missing, registry_path)
        except Exception:
            pass

    return _build_drift_banner(newly)


def _combine_dispatch_output(dispatch_result: "dict | None", banner: "str | None") -> "dict | None":
    """Fold the drift banner (R2/KD2(c)) INTO the single JSON object the dispatcher
    returns — never a bare print(). The banner rides BOTH channels: Claude sees
    `hookSpecificOutput.additionalContext`; the operator sees the top-level
    `systemMessage` (the only operator-visible channel under hook-dispatcher.py's
    _merge_context). On a healthy tree (banner is None) the dispatch result is
    returned unchanged — byte-identical to pre-#2006 (KD6)."""
    if banner is None:
        return dispatch_result

    detector_ctx = None
    if dispatch_result:
        hso = dispatch_result.get("hookSpecificOutput")
        if isinstance(hso, dict):
            detector_ctx = hso.get("additionalContext")

    combined = banner if not detector_ctx else f"{banner}\n\n{detector_ctx}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": combined,
        },
        "systemMessage": banner,
    }


def _emit_banner_only(banner: "str | None") -> None:
    """When a universal filter bails, still surface the banner (if any) as the
    single JSON object before the caller exits. No-op when banner is None (KD6)."""
    output = _combine_dispatch_output(None, banner)
    if output:
        print(json.dumps(output))


# ═══════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════

def main():
    """Main hook execution — reads stdin, dispatches to all detectors."""
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError) as e:
        print(f"Dispatcher: invalid JSON input: {e}", file=sys.stderr)
        sys.exit(0)

    # rp1-home #2006: detector-drift check runs on EVERY prompt, BEFORE the four
    # universal filters below — so an orchestration/monitor-driven session (exactly
    # the ms1 population that stalled) still banners on its first prompt. ADVISORY +
    # FAIL-OPEN (R7): a crash here must never block the filters or the detector loop.
    banner: "str | None" = None
    try:
        banner = check_detector_drift(input_data)
    except Exception:
        banner = None

    prompt = input_data.get("prompt", "")
    if not prompt:
        _emit_banner_only(banner)
        sys.exit(0)

    # Universal filter: sentinel keyword exclusion
    if re.search(r"\bSENTINEL_KEYWORD_\w+\b", prompt):
        _emit_banner_only(banner)
        sys.exit(0)

    # Universal filter: meta-discussion check
    if META_DETECTION_AVAILABLE and is_meta_discussion(prompt):
        _emit_banner_only(banner)
        sys.exit(0)

    # rp1-home #974/#1092: universal orchestration gate. A machine-generated
    # orchestration/monitor tick (a prompt carrying an orchestration MARKER) must
    # bail before ANY detector runs — not just the approval detector. Marker-ONLY
    # (NOT is_orchestration_prompt's oversized arm) so a long legitimate work
    # command still reaches the worker/formatter detectors.
    if ORCH_MARKER_AVAILABLE and has_orchestration_marker(prompt):
        _emit_banner_only(banner)
        sys.exit(0)

    # Dispatch to all detector groups
    result = dispatch(prompt, input_data)

    # Fold the drift banner (if any) INTO the single JSON object (R2/KD2(c)) —
    # never a bare print(). Banner is None on a healthy tree → output unchanged (KD6).
    output = _combine_dispatch_output(result, banner)
    if output:
        print(json.dumps(output))

    sys.exit(0)


if __name__ == "__main__":
    main()
