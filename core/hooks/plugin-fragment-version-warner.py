#!/usr/bin/env python3
# HOOK_VERSION: 1.1.0
# HOOK_EVENT: SessionStart
# HOOK_MATCHER:
# HOOK_GROUP: integrity
"""
plugin-fragment-version-warner — SessionStart delivery channel for the plugin
registry-fragment loader's problem reports (rp1-home#2212, R7).

The loader (``lib/plugin_fragments.py``) is library code with NO output channel of
its own — the dispatcher only folds ``additionalContext`` from REGISTERED modules,
and the loader runs at ``get_entries()`` time outside that fold, with its stderr dead
on exit 0. So this registered SessionStart hook IS the channel: once per session it
renders every non-empty fragment ``load_report`` — a ``LIB_API_VERSION`` mismatch, a
shadowed duplicate, a broken fragment, a rejected detector declaration, a detector
another fragment already provides (rp1-home#2613), or a malformed declaration that was
dropped (including an entry/detector whose script is missing) — as
``additionalContext``. It NEVER blocks (SessionStart is unblockable anyway); a session
with nothing to report emits nothing.

Once-per-session dedupe is a per-session marker file under ``<HOOKS_DIR>/state/``,
mirroring ``post-compaction-sentinel.py`` (NOT ``session-stash-surfacer.py``, which
re-surfaces after every /clear by design). Registered in ``lib/hook_registry.py``
``EVENT_REGISTRY["SessionStart"]`` only — the single registration surface (#1729),
never ``settings.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HOOKS_DIR, "state")

# Ensure both the hooks dir and its lib/ are importable whether this runs in-process
# (dispatcher) or standalone (tests invoke main() with stdin injected).
for _p in (HOOKS_DIR, os.path.join(HOOKS_DIR, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _session_key(input_data: dict) -> str:
    """Stable per-session key: payload session_id, else a transcript-path hash."""
    data = input_data or {}
    sid = data.get("session_id") or ""
    if sid:
        return sid
    tp = data.get("transcript_path") or ""
    if tp:
        return hashlib.md5(tp.encode()).hexdigest()[:12]
    return "unknown"


def _marker_path(session_key: str) -> str:
    return os.path.join(STATE_DIR, f"plugin-fragment-version-warner-{session_key}.json")


def _already_warned(session_key: str) -> bool:
    return os.path.exists(_marker_path(session_key))


def _mark_warned(session_key: str) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _marker_path(session_key) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"warned": True}, fh)
        os.replace(tmp, _marker_path(session_key))
    except Exception:
        pass  # dedupe is best-effort — over-firing once beats crashing SessionStart


def _collect_reports() -> list | None:
    """Collect per-plugin load reports via the shared loader.

    Returns ``None`` when collection itself failed, so ``main`` can leave the session
    un-marked and a later SessionStart retries instead of staying silent (#2212 IR-3)."""
    try:
        try:
            from lib import plugin_fragments
            from lib.hook_registry import EVENT_REGISTRY
            from lib.detector_registry import PRIORITY_GROUP, INDEPENDENT_GROUP
        except ImportError:  # dual path: lib/ on sys.path[0]
            import plugin_fragments  # type: ignore
            from hook_registry import EVENT_REGISTRY  # type: ignore
            from detector_registry import PRIORITY_GROUP, INDEPENDENT_GROUP  # type: ignore
        return plugin_fragments.collect_fragment_reports(
            EVENT_REGISTRY, PRIORITY_GROUP, INDEPENDENT_GROUP
        )
    except Exception as exc:  # never let the warner crash a session
        print(f"plugin-fragment-version-warner: report collection failed: {exc!r}", file=sys.stderr)
        return None


def _render(reports: list) -> str:
    """Render the reports as a single human-readable additionalContext block."""
    lines = [
        "⚠️ Hapimatic plugin fragments — registry-fragment loader report:",
        "",
    ]
    for report in reports:
        name = report.get("plugin") or report.get("key") or "<unknown plugin>"
        if report.get("broken"):
            lines.append(
                f"- {name}: registry-fragment FAILED to import — skipped for THIS plugin "
                "only; every core hook still runs. See the loader logs for the traceback."
            )
        version_mismatch = report.get("version_mismatch")
        if version_mismatch:
            lines.append(f"- {version_mismatch}")
        for shadowed in report.get("shadowed", []):
            lines.append(
                f"- {name}: {shadowed} is SHADOWED by a core registration — the fragment "
                "copy will not run (core wins)."
            )
        for rejected in report.get("rejected_detectors", []):
            lines.append(f"- {name}: rejected detector declaration — {rejected}.")
        for duplicate in report.get("duplicates", []):
            # rp1-home#2613 OOS-5 (K-DUP): {"detector", "owner"} — the owner's copy runs.
            lines.append(
                f"- {name}: detector {duplicate.get('detector')} duplicates "
                f"{duplicate.get('owner')}'s — only the first copy runs."
            )
        for malformed in report.get("malformed", []):
            lines.append(
                f"- {name}: malformed declaration dropped — {malformed} (the rest of "
                "this plugin and every core hook still run)."
            )
    lines.append("")
    lines.append(
        "These are advisory (never a block). See "
        "`.claude/docs/plugin-registry-fragment-contract.md`."
    )
    return "\n".join(lines)


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    try:
        input_data = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    session_key = _session_key(input_data)
    if _already_warned(session_key):
        return 0  # once per session

    reports = _collect_reports()
    if reports is None:
        return 0  # collection failed — leave un-marked so a later SessionStart retries
    _mark_warned(session_key)  # mark once collection succeeded (dedupe)

    if not reports:
        return 0  # nothing to report → emit nothing

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": _render(reports),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
