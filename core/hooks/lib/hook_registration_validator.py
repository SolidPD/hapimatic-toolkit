"""
Hook Registration Validator — bidirectional consistency check.

VERSION: 1.10.0

Validates that the set of hooks registered in `~/.claude/settings.json` (plus
`lib/detector_registry.py`, `lib/hook_priorities.py`, `tests/test_hook_priorities.py`)
matches the set of hook script files actually present at their registered
absolute paths in the main checkout.

As of v1.1.0 (rp1-home #786), the validator also scans each worktree's
`~/.worktrees/<name>/.claude/settings.json` to catch the session-bricking
class from rp1-home #743 — a worktree-local hook command whose absolute-path
target does not exist in the main checkout. Anchored to the passed `home`
parameter so pre-commit's `--home $TMPDIR` invocation is a natural no-op.

Catches three classes of drift:

- **Forward direction**: an entry in `settings.json` references an absolute
  path that does NOT exist on disk. This is the rp1-home #600 symptom — a
  worker added a hook entry in `settings.json` but only created the file
  inside the worktree, blocking every UserPromptSubmit in that worktree until
  manual recovery copied the file to the main checkout.
  As of v1.7.0 (rp1-home #1943) the forward audit also covers `settings.local.json`
  — the GLOBAL `~/.claude/settings.local.json`, each worktree's
  `.claude/settings.local.json`, AND (via `--project-dir`) a project-level
  checkout's `.claude/settings.json` + `.claude/settings.local.json`. A
  `UserPromptSubmit` hook registered in a `settings.local.json` whose path is
  absent on the current host hard-blocks every prompt exactly as a `settings.json`
  one does — the SL7 silent-block incident, where a tracked project-level
  `settings.local.json` carried RP1-only absolute hook paths. Forward audit ONLY:
  these `settings.local.json` rows are NOT unioned into the reverse-direction
  "registered" set, so reverse/single-surface/dual-registration behavior is
  byte-unchanged.

- **Worktree-local settings.json (rp1-home #786)**: each worktree under
  `~/.worktrees/<name>/` may have its own `.claude/settings.json` whose hook
  commands reference absolute paths in the main checkout
  (`~/.claude/hooks/<name>.py`). When a worker session registers a
  hook command in the worktree's settings before the corresponding file is
  promoted to main, the command silently dangles — and Claude Code's settings
  reloader fires it mid-session, which exits 2 → Claude treats as deny →
  session bricked. This was the rp1-home #743 trigger. Forward + reverse
  drift checks both extend to worktree settings: a worktree-only registration
  counts as "registered" for the reverse-direction audit (otherwise every
  in-flight worktree-added hook would be flagged as orphaned).

- **Dual registration (rp1-home #953)**: a detector that is BOTH directly
  registered in the main `~/.claude/settings.json` AND owned by the unified
  dispatcher (i.e. a `detector_registry.py` PRIORITY_GROUP/INDEPENDENT_GROUP
  member) fires TWICE per UserPromptSubmit — once from its own settings.json
  entry and once via the dispatcher. The dispatcher-migration fix is to register
  only the dispatcher (plus the standalones) and let it own every registry
  member; a leftover direct registration is the double-firing bug. Scope is
  MAIN-settings-only (computed before the worktree union) so in-flight worktree
  branches that still carry the pre-fix direct registrations do not flood live
  worker/closer invocations with false positives. Detected mechanically by
  set-intersecting the registry names against the main settings.json basenames
  — no module import.

- **Reverse direction**: a `*.py` (or detector-style) file exists in
  `~/.claude/hooks/` but is NOT registered. Multiple sub-classes:
    - **settings**: file exists but no settings.json command resolves to it
    - **registry**: detector file imports `check_and_claim_priority` but
      missing from `lib/detector_registry.py` PRIORITY_GROUP/INDEPENDENT_GROUP
    - **priorities**: detector is in PRIORITY_GROUP but missing from
      `lib/hook_priorities.py` HOOK_PRIORITIES
    - **test**: detector is in HOOK_PRIORITIES but missing from
      `tests/test_hook_priorities.py::test_all_hooks_registered` expected_hooks
    - **contract**: detector is a `detector_registry.py` PRIORITY_GROUP/
      INDEPENDENT_GROUP member that EXISTS on disk but defines no module-level
      `run_detection` function (the rp1-home #862 class — the unified
      dispatcher does `getattr(module, "run_detection", None)` and silently
      skips members that lack it; the presence-only checks above cannot catch
      this). Detected via static AST parse, never by importing the module.
      As of v1.6.0 (rp1-home #2006) a member whose FILE is absent no longer
      lands here — it is routed to the exclusive detector_registry FORWARD class
      below, so this layer keeps its true "registered without run_detection"
      meaning for files that actually exist (the private product repo #3567 misread).
    - **strict (opt-in, `--strict`, rp1-home #638)**: by default the reverse-
      settings check covers only `*-detector.py` files (the narrow, low-false-
      positive scope). `--strict` broadens it to EVERY top-level hook file that
      declares intent — a `HOOK_EVENT` annotation OR a priority-API import — but
      is registered in neither settings.json (any event, incl. the worktree
      union) NOR detector_registry. Catches the non-detector dead-code class
      (e.g. `session-edit-logger.py`, or `lora-import-detector.py` before #638)
      that the narrow scope structurally misses. Advisory by design — a
      `HOOK_EVENT` substring in a comment is enough to flag a file, so the
      false-positive surface is wider than the default. NEVER runs in the
      default path: the strict list stays empty unless `--strict` is passed, so
      exit-code/`has_mismatches()` behavior is byte-identical to pre-#638.
      As of v1.5.0 (rp1-home #1735) the DEFAULT reverse-settings scan was widened
      to cover the same intent files (see "Single-surface rule" below), so the
      `--strict` flag is a backward-compatible superset that adds no coverage.
      DEPRECATED as of v1.9.0 (rp1-home #1990): `--strict` is now a documented
      no-op alias — any file it would flag is already caught by the default scan
      (the cascade-skip empties the strict bucket by construction), so the flag
      cannot add coverage. Passing it emits a one-time deprecation warning. The
      flag and the (always-empty) `reverse_mismatches_strict_settings` field are
      retained for caller/`--json`-schema stability; the R1 dispatcher-wiring check
      now owns the "registered ⇒ actually fires" guarantee `--strict` once gestured at.

- **Single-surface rule (v1.5.0, rp1-home #1735)**: `~/.claude/settings.json`
  (AND `~/.claude/settings.local.json`, per R7/D3) may register ONLY the per-event
  `hook-dispatcher.py <event>` dispatcher entries (`DISPATCHER_ALLOWED_COMMANDS`)
  plus a small documented exception list (`SINGLE_SURFACE_EXCEPTIONS`). Every other
  hook MUST live in `lib/hook_registry.py` — the mechanically-enforced single
  registration surface. A standalone hook registration FAILS validation with a
  teaching message naming `hook_registry.py` as the correct surface. Under #1734's
  landed NESTED shape a direct `unified-prompt-dispatcher.py` settings.json entry is
  a single-surface violation (the #953 double-fire hole the dual-registration check
  cannot catch — UPD is not a `detector_registry` member). Two companion checks land
  with the rule: (a) a `hook_registry.py` FORWARD check — an `EVENT_REGISTRY` entry
  whose module file (`.py` or `.sh`) is missing on disk FAILS (#600-class, previously
  unowned); (b) the DEFAULT reverse audit is widened beyond `*-detector.py` to every
  intent-declaring hook file (`HOOK_EVENT` annotation OR priority-API import), so a
  non-detector hook file absent from the registry is flagged WITHOUT `--strict`.
  Scope is MAIN settings + settings.local.json ONLY (not the worktree union), mirroring
  the #953 MAIN-only scoping so in-flight worktree branches do not false-positive.

- **detector_registry FORWARD drift (v1.6.0, rp1-home #2006 / KD4 / D7)**: a
  `detector_registry.py` PRIORITY_GROUP/INDEPENDENT_GROUP member whose on-disk file is
  ABSENT — the unified prompt dispatcher can never fire it, so every workflow phrase it
  owns (a USER-APPROVAL token paste, `work on issue #N`, compaction reload, session self-context, …)
  silently does nothing. This is the private product repo #3590 Configuration Porter-drop
  class (all 38+ detectors gone on ms1 for three days). Reported EXCLUSIVELY in
  `detector_registry_forward_mismatches` so the summary reads "N registry detectors have
  no file" instead of misattributing the absence to a missing `run_detection` (the
  the private product repo #3567 misread). Pure `Path.exists()`, no import/AST — the same
  no-import philosophy as the `hook_registry.py` forward check.

- **Reverse-remediation wording (v1.8.0, rp1-home #1986)**: the reverse-direction
  remediation footers (`reverse_settings`, `reverse_strict`) and the `_render_human`
  "reverse" section label now direct authors to the single registration surface
  (`lib/hook_registry.py` `EVENT_REGISTRY` / `lib/detector_registry.py`) instead of a
  standalone `settings.json` block — the old text told authors to make exactly the
  single-surface violation the rule above then rejects (exit 1), an infinite
  "follow the advice → fail the check" loop. Message text only: validation behavior,
  the `reverse_settings` dict key, and the `reverse_mismatches_settings` field are
  byte-unchanged.

- **Plugin fragments (v1.10.0, rp1-home #2213, epic #2211 Sub B)**: after #2212 a premium
  plugin registers hooks through a SECOND legitimate surface — its registry fragment
  `plugins/<name>/hooks/registry-fragment.py` (`.claude/docs/plugin-registry-fragment-contract.md`).
  REPO-TREE mode (every layer — the fragment under validation lives in the tree under `--home`,
  never in installed config state): for each fragment-bearing `<home>/plugins/<name>/`,
  (R1) every `EVENT_ENTRIES` module / `DETECTORS` path / `NATIVE_SESSIONSTART_CHECK` resolves
  to a file under the plugin root, resolved exactly as the loader resolves it; (R2) a
  hook-shaped `hooks/*.py` the fragment does not name is dead code; (R3) no native-hook surface
  Claude Code 2.1.280 honours (`.claude/docs/plugin-cli-surface-findings.md` §6 — hooks.json,
  manifest / marketplace-entry `hooks`, `modules`, plugin settings.json, skill/command/agent
  frontmatter) registers anything beyond the ONE fragment-declared SessionStart check; (R9) the
  plugin is listed in `.claude-plugin/marketplace.json`. The fragment is parsed with `ast` and
  NEVER imported. A plugin dir without a fragment is not one of ours and is never reported.
  Closer-only `--plugins-installed` adds the INFORMATIONAL installed-set parity rows via
  `lib/plugin_fragments.discover_plugins()` (#2212's discovery — never a second one). Every new
  code path is gated on >= 1 fragment-bearing plugin, so with none the human and `--json` output
  are byte-identical to v1.9.0 (R8). Results land in `ValidationReport.plugins`.

The nested reverse checks are CASCADED: if a file fails R2 (missing from
settings.json), it's NOT also reported under R3/R4 for the same file. This
keeps the mismatch matrix per-file rather than producing duplicate noise.

Single source of truth for three integration layers (per R11):
- `issue-worker` Phase 6 pre-flight (R8)
- `~/.githooks/pre-commit-hook-registration` (R9)
- `issue-closer` Step 14.5 Discoverability Audit extension (R10)

Fail-open philosophy: corrupt input or missing files yield an empty/partial
report and log to stderr — never raise to break the pre-commit chain or
worker workflow. The validator's purpose is to catch INADVERTENT drift;
hard failures during validation defeat the purpose.

CLI usage:
    python3 hook_registration_validator.py                    # validate ~/
    python3 hook_registration_validator.py --home DIR         # validate against DIR
    python3 hook_registration_validator.py --project-dir DIR  # also forward-scan a project checkout's .claude/*settings*.json (rp1-home #1943)
    python3 hook_registration_validator.py --json             # machine-readable output
    python3 hook_registration_validator.py --quiet            # exit-code only
    python3 hook_registration_validator.py --plugins-installed  # + installed-set plugin parity (closer-only, rp1-home #2213)

    --project-dir resolution (rp1-home #1943): when NOT passed and --home is NOT
    given, main() auto-resolves it from $CLAUDE_PROJECT_DIR, else `git rev-parse
    --show-toplevel` (fail-open), else skips the project scan. With --home given the
    validator performs NO project scan and NO git call (hermetic — pre-commit's
    `--home $TMPDIR` path is unchanged).

Exit codes:
    0 — no mismatches
    1 — mismatches found
    2 — internal error (corrupt settings.json, missing dirs, etc.)
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

__version__ = "1.10.0"


# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

# Tokens that indicate the first arg of a command is the script path.
COMMAND_RUNTIMES = {"python", "python3", "bash", "sh", "zsh"}

# Files in .claude/hooks/ that are intentionally NOT hooks. The reverse-settings
# check excludes these. Keep this list minimal and well-commented — each entry
# represents a deliberate decision that the file should NOT be wired into a hook
# event matcher.
KNOWN_NON_HOOK_FILES: set[str] = {
    # Bash worker helper invoked by auto-commit-skill-edits hook; not a hook itself
    "auto-commit-skill-edits-worker.sh",
    # Back-compat shim for the rp1-home #944 detector rename
    # (issue-research-sweep -> issue-researcher-sweep). ~11 worktrees branched
    # pre-rename still register the OLD absolute path; the shim os.execv's the
    # renamed detector so those sessions do not go deaf (a missing UserPromptSubmit
    # hook exits 2 = hard block of every prompt). Intentionally NOT registered in
    # main's settings.json -- registering it there would double-fire detection in
    # fresh post-rename sessions. Remove the shim file + this entry once no worktree
    # registers the old path / rp1-home #948 lands.
    "issue-research-sweep-detector.py",
}


# The legal Claude Code hook events, in lifecycle order (rp1-home #1990 / R4).
# SINGLE SOURCE OF TRUTH for the event set: it used to live ONLY in
# generate-hooks-config.py (:46-55), so the validator could not tell a typo'd event
# ("OnFileSave") from a real one. Defined HERE in the lib — exactly as
# KNOWN_NON_HOOK_FILES is — and imported BACK by the generator (generate-hooks-config.py),
# mirroring the existing KNOWN_NON_HOOK_FILES import seam so the two surfaces never drift.
# Used by the invalid_event_keys check (an EVENT_REGISTRY key not in this set names hooks
# that can never fire) and the advisory HOOK_EVENT header lint.
EVENT_ORDER = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
]

VALID_EVENTS: set[str] = set(EVENT_ORDER)


# ═══════════════════════════════════════════════════════════════════════
# Single-surface rule constants (rp1-home #1735)
# ═══════════════════════════════════════════════════════════════════════

# The ONE shared allowed-set constant defining "a valid direct dispatcher
# registration" (epic #1729 Finding 2 / R33). A settings.json / settings.local.json
# hook command whose resolved script basename is in this set is the sanctioned
# single-surface entry point; everything else must live in lib/hook_registry.py.
#
# Under #1734's landed NESTED shape, settings.json carries exactly ONE
# `hook-dispatcher.py <event>` entry per event and NOTHING else —
# unified-prompt-dispatcher.py is a NESTED hook_registry.py (Level-1) entry, NOT a
# direct settings.json registration. So a DIRECT unified-prompt-dispatcher.py entry
# is a single-surface violation (the #953 double-fire hole the dual-registration
# check cannot catch: UPD is not a detector_registry member). `unified-prompt-dispatcher.py`
# is therefore deliberately ABSENT from this set. (If #1734 had instead landed the
# documented 2-entry alternative — a direct UPD entry alongside the dispatcher — it
# would be added here; the verified landed shape is the nested one.)
DISPATCHER_ALLOWED_COMMANDS: set[str] = {
    "hook-dispatcher.py",
}

# Single-surface exception list (R6/R7, rp1-home #1735). Non-dispatcher hook commands
# that are LEGITIMATELY registered directly in the global surface (settings.json +
# settings.local.json). Mirror the KNOWN_NON_HOOK_FILES discipline — each entry a
# deliberate, documented decision.
#
# Per-repo project-level hooks (`${CLAUDE_PROJECT_DIR}/.claude/hooks/*` registrations
# in a repo's own settings) are NOT listed here and need no entry: the SINGLE-SURFACE /
# reverse / dual-registration scope is `~/.claude/settings.json` (+ `settings.local.json`
# + worktrees), never a project's settings.json, so project-level registrations are out
# of those checks by construction (R6).
#
# FORWARD path-existence ONLY (rp1-home #1943): the forward audit DOES now reach a
# project-level checkout's `.claude/settings.json` + `.claude/settings.local.json` when
# `--project-dir` is passed (the SL7 leak location) AND each worktree's
# `.claude/settings.local.json`. This widens only the "does this registered command's
# path exist on the host" check — it does NOT fold project/local registrations into the
# single-surface or reverse audits above (widening those would reintroduce the
# worktree/peer false-positive class #1735/#953 guard against).
#
# Federation caveat (R7/D3): peers (RP2/SL7/…) may carry DIFFERENT per-machine
# registrations in their own settings.local.json — this list is per-machine
# extensible; add a machine's local helpers here when validating on that machine.
SINGLE_SURFACE_EXCEPTIONS: set[str] = {
    # Per-machine local PostToolUse/Bash sudo-reminder helper, registered in
    # ~/.claude/settings.local.json (NOT settings.json). Brought into the
    # single-surface scan by R7/D3 (the walk historically read only settings.json).
    "nas-sudo-helper.sh",
}


# ═══════════════════════════════════════════════════════════════════════
# Plugin-fragment rule constants (rp1-home #2213)
# ═══════════════════════════════════════════════════════════════════════

# Repo-tree layout the plugin rules walk (relative to the validated `home`). Plugin source
# for v1 lives under `plugins/<name>/` in this repo, listed in `.claude-plugin/marketplace.json`
# as `{"source": "./plugins/<name>"}` (plugin-registry-fragment-contract.md §11).
PLUGINS_DIRNAME = "plugins"
FRAGMENT_RELPATH = Path("hooks") / "registry-fragment.py"
NATIVE_HOOKS_RELPATH = Path("hooks") / "hooks.json"
PLUGIN_MANIFEST_RELPATH = Path(".claude-plugin") / "plugin.json"
MARKETPLACE_RELPATH = Path(".claude-plugin") / "marketplace.json"

# An UNWRAPPED hooks.json declaring one of these at its top level makes Claude Code 2.1.280
# REFUSE the whole plugin (#2381 F-H2b); any other unwrapped shape loads 0 hooks (F-H2a).
PLUGIN_TOPLEVEL_REFUSED_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})

# Non-hook execution surfaces #2213 deliberately does NOT police (#2381 §6 F-X..F-X6): they
# never fire as hooks, so they cannot bypass the dispatcher. Named here so the scope-out is
# explicit rather than silent; the R10 golden gate classifies them as non-hook surfaces.
PLUGIN_NON_HOOK_SURFACES = ("monitors", "mcpServers", "lspServers", "bin", "workflows", "channels")

# Directories the frontmatter scan never descends into, at any depth (vendored deps, caches).
PLUGIN_SCAN_SKIP_DIRS = frozenset({"node_modules", "__pycache__"})
# Skipped ONLY at the plugin root (the plugin's own test fixtures). Deeper down the name is an
# ordinary directory: a skill can be called `tests` (`skills/tests/SKILL.md`), and its
# frontmatter `hooks:` still registers on invocation, so it must be scanned.
PLUGIN_ROOT_SKIP_DIRS = frozenset({"tests"})

# Per-plugin finding kind → the REMEDIATION_FOOTERS key that explains it. Every kind here is a
# HARD violation (counts in has_mismatches). Informational findings use PLUGIN_ADVISORY_KINDS
# and land in the plugin's `advisories` list instead, never `violations`.
PLUGIN_FOOTER_FOR_KIND: dict[str, str] = {
    "missing_module": "plugin_forward",
    "fragment_unparseable": "plugin_forward",
    "fragment_not_static": "plugin_forward",
    "fragment_invalid_event": "plugin_forward",
    "plugin_validation_error": "plugin_forward",
    "unregistered_hook": "plugin_reverse",
    "native_hook_not_allowed": "plugin_native_hook",
    "hooksjson_unwrapped": "plugin_native_hook",
    "hooksjson_toplevel_event": "plugin_native_hook",
    "manifest_hook_not_allowed": "plugin_native_hook",
    "hooks_module_forbidden": "plugin_native_hook",
    "frontmatter_hook_not_allowed": "plugin_native_hook",
    "settings_hook_not_allowed": "plugin_native_hook",
    "handler_type_not_allowed": "plugin_native_hook",
    "native_hooks_unreadable": "plugin_native_hook",
    "detector_position_declared": "plugin_detector",
    "unlisted_in_marketplace": "plugin_marketplace",
}

# INFORMATIONAL findings (never affect the exit code):
#   entry_hook_path_ignored — a marketplace-entry `hooks` PATH string installs but fires nothing
#                             on 2.1.280 (F-H8): no dispatcher bypass, but an author footgun.
#   exec_form_not_fired     — the declared SessionStart check is written in exec-form `args`,
#                             which does not fire on 2.1.280 (F-H14): the check silently never runs.
PLUGIN_ADVISORY_KINDS = frozenset({"entry_hook_path_ignored", "exec_form_not_fired"})


# ═══════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class ParsedCommand:
    """A single command entry parsed from settings.json's hooks arrays."""

    event: str               # e.g. "UserPromptSubmit"
    matcher: str             # e.g. "" or "Bash"
    command: str             # raw command string from settings.json
    resolved_path: Optional[Path]  # extracted absolute script path, or None if unparseable
    setting_json_index: int  # 0-based index within the event's hooks blocks


@dataclass
class ForwardMismatch:
    """A settings.json entry whose resolved_path does not exist on disk."""

    event: str
    matcher: str
    command: str
    resolved_path: str
    setting_json_index: int
    source_settings: str = ""  # absolute path of the settings.json this command came from (rp1-home #786)


@dataclass
class ValidationReport:
    """Bidirectional registration mismatch matrix."""

    forward_mismatches: list[ForwardMismatch] = field(default_factory=list)
    reverse_mismatches_settings: list[str] = field(default_factory=list)
    reverse_mismatches_registry: list[str] = field(default_factory=list)
    reverse_mismatches_priorities: list[str] = field(default_factory=list)
    reverse_mismatches_test: list[str] = field(default_factory=list)
    reverse_mismatches_contract: list[str] = field(default_factory=list)
    # Opt-in --strict layer (rp1-home #638): non-detector intent files lacking
    # any registration. ALWAYS empty unless validate_registrations(strict=True)
    # populated it, so its inclusion below never changes default-mode behavior.
    reverse_mismatches_strict_settings: list[str] = field(default_factory=list)
    dual_registrations: list[str] = field(default_factory=list)
    # Dispatcher wiring (rp1-home #1990 / R1): every lib/hook_registry.py EVENT_REGISTRY
    # key with members must have EXACTLY ONE live `hook-dispatcher.py <event>` line under
    # the matching event in the MAIN settings.json. Zero → the whole event's registry
    # members are silently unwired while the validator used to exit 0 (the live blind
    # spot: delete `hooks.Stop` → 8 members never fire, OK/exit 0). >1 → duplicate. A
    # dispatcher line under the wrong event (arg≠registration) wires nothing. Each item is
    # a human-readable string naming the event + its orphaned members.
    dispatcher_wiring_mismatches: list[str] = field(default_factory=list)
    # Invalid event keys (rp1-home #1990 / R4): an EVENT_REGISTRY key that is not a legal
    # Claude Code hook event (VALID_EVENTS). hook-dispatcher.py dispatches by event key, so
    # a typo'd key (e.g. "OnFileSave") can NEVER fire its members — invisible before this
    # check. Each item names the key + its unreachable members.
    invalid_event_keys: list[str] = field(default_factory=list)
    # Single-surface rule (rp1-home #1735): a settings.json / settings.local.json
    # hook registration that is neither a dispatcher entry nor a documented
    # exception — it must live in lib/hook_registry.py instead. Each item is a
    # human-readable "event: command" string.
    single_surface_violations: list[str] = field(default_factory=list)
    # hook_registry.py FORWARD drift (rp1-home #1735 / R29): an EVENT_REGISTRY
    # module basename whose on-disk file (.py or .sh) does not exist (#600-class).
    hook_registry_forward_mismatches: list[str] = field(default_factory=list)
    # detector_registry.py FORWARD drift (rp1-home #2006 / KD4 / D7): a
    # PRIORITY_GROUP/INDEPENDENT_GROUP member whose on-disk file is ABSENT — the
    # unified dispatcher can never fire it (the private product repo #3590 Configuration
    # Porter-drop class). EXCLUSIVE of reverse_mismatches_contract: a missing file
    # lands ONLY here, so the contract class keeps its true meaning ("N detectors
    # registered without run_detection") for files that actually EXIST — the misread
    # that produced the private product repo #3567.
    detector_registry_forward_mismatches: list[str] = field(default_factory=list)
    # Advisory ONLY (rp1-home #1990 / R4) — does NOT feed has_mismatches() / exit code.
    # A hook FILE whose `# HOOK_EVENT:` header names a non-legal event. Post-#1734 nothing
    # on the live path reads that header (only the obsolete generator + an intent substring
    # scan), so a bad header is a warning, not a failing class — the FIRING surface is the
    # EVENT_REGISTRY key set (invalid_event_keys above), which IS a failing class.
    invalid_event_header_warnings: list[str] = field(default_factory=list)
    # Plugin fragments (rp1-home #2213 / R6): one dict per fragment-bearing plugin under
    # <home>/plugins/ (plus, under --plugins-installed, installed fragment plugins with no
    # repo-tree counterpart). Shape: {name, root, fragment_path, entry_count, detector_count,
    # native_check, violations: [{kind, path?, detail}], advisories: [...], shadowed:
    # [{event?, name}], installed?: {key, root, root_kind, differs}}. Only `violations` feed
    # has_mismatches(); `advisories`, `shadowed` and `installed` are informational. EMPTY
    # unless the tree holds a fragment-bearing plugin, so to_dict()/_render_human() add
    # nothing for a zero-fragment tree (the R8 byte-identical invariant).
    plugins: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def plugin_violation_count(self) -> int:
        return sum(len(p.get("violations") or []) for p in self.plugins)

    def has_mismatches(self) -> bool:
        return bool(
            self.forward_mismatches
            or self.reverse_mismatches_settings
            or self.reverse_mismatches_registry
            or self.reverse_mismatches_priorities
            or self.reverse_mismatches_test
            or self.reverse_mismatches_contract
            or self.reverse_mismatches_strict_settings
            or self.dual_registrations
            or self.dispatcher_wiring_mismatches
            or self.invalid_event_keys
            or self.single_surface_violations
            or self.hook_registry_forward_mismatches
            or self.detector_registry_forward_mismatches
            or self.plugin_violation_count() > 0
        )

    def to_dict(self) -> dict:
        data = {
            "forward_mismatches": [asdict(m) for m in self.forward_mismatches],
            "reverse_mismatches_settings": list(self.reverse_mismatches_settings),
            "reverse_mismatches_registry": list(self.reverse_mismatches_registry),
            "reverse_mismatches_priorities": list(self.reverse_mismatches_priorities),
            "reverse_mismatches_test": list(self.reverse_mismatches_test),
            "reverse_mismatches_contract": list(self.reverse_mismatches_contract),
            "reverse_mismatches_strict_settings": list(self.reverse_mismatches_strict_settings),
            "dual_registrations": list(self.dual_registrations),
            "dispatcher_wiring_mismatches": list(self.dispatcher_wiring_mismatches),
            "invalid_event_keys": list(self.invalid_event_keys),
            "single_surface_violations": list(self.single_surface_violations),
            "hook_registry_forward_mismatches": list(self.hook_registry_forward_mismatches),
            "detector_registry_forward_mismatches": list(self.detector_registry_forward_mismatches),
            "invalid_event_header_warnings": list(self.invalid_event_header_warnings),
        }
        # R8: the `plugins` key exists ONLY when a fragment-bearing plugin was validated, so a
        # zero-fragment tree serializes byte-identically to v1.9.0 (same keys, same order).
        if self.plugins:
            data["plugins"] = [dict(p) for p in self.plugins]
        data["errors"] = list(self.errors)
        return data


# ═══════════════════════════════════════════════════════════════════════
# Command parsing (R5)
# ═══════════════════════════════════════════════════════════════════════


# rp1-home #1000: match $HOME and ${HOME} (but NOT $HOMEBREW etc.). Mirrors the shell-var
# expansion in hook-resolution-guard.py, but anchored to a `home` parameter (NOT os.environ)
# so the validator's --home <TMPDIR> staged-tree mode stays correct.
_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")


def _expand_home_var(cmd: str, home: Path) -> str:
    """Expand $HOME / ${HOME} in a command string against `home` (rp1-home #1000, R-B)."""
    return _HOME_VAR_RE.sub(str(home), cmd)


def parse_command(
    cmd: str, home: Optional[Path] = None, plugin_root: Optional[Path] = None
) -> Optional[Path]:
    """
    Extract the script path from a settings.json command string.

    `plugin_root` (rp1-home #2213): when passed, the command is a PLUGIN native-hook command —
    `${CLAUDE_PLUGIN_ROOT}` / `$CLAUDE_PLUGIN_ROOT` expand to `plugin_root` and the command is
    tokenised shell-style (`shlex`), because the form that fires on Claude Code 2.1.280 embeds
    quotes inside a token (`"${CLAUDE_PLUGIN_ROOT}"/hooks/x.sh`, #2381 F-H14) which the core
    whitespace split below cannot read. With `plugin_root=None` (every core caller) the core
    path runs exactly as before.

    Supported forms (R5):
        "python3 /a/b/c.py"     → /a/b/c.py
        "python /a/b/c.py arg"  → /a/b/c.py
        "bash /a/b/c.sh"        → /a/b/c.sh
        "/a/b/c.py"             → /a/b/c.py    (direct-exec via shebang)
        "'python3 /a/b/c.py'"   → /a/b/c.py    (outer quotes stripped)
        ""                      → None
        "echo hello"            → None         (echo isn't a runtime, no .py/.sh)

    Tokenization: shlex-style whitespace split. If first token is a runtime
    in COMMAND_RUNTIMES, the path is the second token. Otherwise, if the first
    token looks like a script path (starts with `/` or `~` and ends with a
    recognized extension), it's the path. Quoted tokens are unwrapped.

    Returns None if no recognizable script path is found.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return None

    # rp1-home #1000: expand $HOME / ${HOME} against the `home` PARAMETER (NOT the live
    # os.environ['HOME']) before path extraction, so generated hook commands of the form
    # `python3 $HOME/.claude/hooks/X.py` resolve to a real absolute path. The write-guard
    # (claude-settings-write-guard.py) calls this with no `home` so the live Path.home()
    # default applies (D3=B — one shared expansion implementation, two consumers).
    if home is None:
        home = Path.home()
    if plugin_root is not None:
        return _parse_plugin_command(cmd, home, Path(plugin_root))
    cmd = _expand_home_var(cmd, home)

    # Strip outer quotes if the entire command is quoted
    s = cmd.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    # Tokenize on whitespace (simple split — sufficient for the path forms we care about)
    tokens = s.split()
    if not tokens:
        return None

    # Strip surrounding quotes from individual tokens
    def unquote(t: str) -> str:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in {"'", '"'}:
            return t[1:-1]
        return t

    first = unquote(tokens[0])

    # Runtime form: first token is python3/bash/etc., path is second token
    runtime_basename = os.path.basename(first)
    if runtime_basename in COMMAND_RUNTIMES:
        if len(tokens) < 2:
            return None
        path = unquote(tokens[1])
        return Path(path) if _looks_like_path(path) else None

    # Direct-exec form: first token IS the path (must look like a script)
    if _looks_like_path(first):
        return Path(first)

    return None


def _looks_like_path(s: str) -> bool:
    """A token looks like a script path if it's absolute or home-relative."""
    if not s:
        return False
    return s.startswith("/") or s.startswith("~")


# rp1-home #2213: Claude Code's plugin-root variable, in both spellings.
_PLUGIN_ROOT_VAR_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT(?![A-Za-z0-9_])")


def _expand_plugin_vars(s: str, home: Path, plugin_root: Path) -> str:
    """Expand ${CLAUDE_PLUGIN_ROOT} / $CLAUDE_PLUGIN_ROOT and $HOME / ${HOME} in `s`."""
    s = _PLUGIN_ROOT_VAR_RE.sub(lambda _m: str(plugin_root), s)
    return _HOME_VAR_RE.sub(lambda _m: str(home), s)


def _script_from_tokens(tokens: list[str]) -> Optional[Path]:
    """The script a tokenised command runs: tokens[1] after a runtime, else tokens[0]."""
    if not tokens:
        return None
    if os.path.basename(tokens[0]) in COMMAND_RUNTIMES:
        if len(tokens) < 2:
            return None
        return Path(tokens[1]) if _looks_like_path(tokens[1]) else None
    return Path(tokens[0]) if _looks_like_path(tokens[0]) else None


def _parse_plugin_command(cmd: str, home: Path, plugin_root: Path) -> Optional[Path]:
    """parse_command's plugin branch (rp1-home #2213) — see parse_command's docstring.

    Tokenise FIRST, then expand the variables per token, so a plugin root containing
    whitespace stays one token (expanding first would split it)."""
    try:
        tokens = shlex.split(cmd)
        # A command wrapped whole in one pair of quotes ('python3 /a/b.py') is one token.
        if len(tokens) == 1 and any(c.isspace() for c in tokens[0]):
            tokens = shlex.split(tokens[0])
    except ValueError:  # unbalanced quotes — not a resolvable script path
        return None
    return _script_from_tokens([_expand_plugin_vars(t, home, plugin_root) for t in tokens])


_SHELL_CONTROL_CHARS = frozenset(";&|()")


def _shell_control_operator(cmd: str) -> Optional[str]:
    """The first shell construct in `cmd` that runs ANOTHER command (`&&`, `||`, `;`, `|`, `&`,
    a subshell, a command substitution, a newline), or None. Redirections (`>`, `2>&1`) are
    not control operators. Conservative: a construct inside quotes still counts for `$(`/backtick."""
    if "\n" in cmd:
        return "newline"
    if "$(" in cmd or "`" in cmd:
        return "command substitution"
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        for token in lexer:
            if token and set(token) <= _SHELL_CONTROL_CHARS:
                return token
    except ValueError:
        return None
    return None


# ═══════════════════════════════════════════════════════════════════════
# Inventory walkers
# ═══════════════════════════════════════════════════════════════════════


def walk_settings_json(settings_path: Path, home: Optional[Path] = None) -> list[ParsedCommand]:
    """
    Parse settings.json and return every hook command as a ParsedCommand.

    Walks all event types (UserPromptSubmit, PreToolUse, PostToolUse, etc.).
    Fail-open: returns an empty list on parse error and logs to stderr.
    """
    if not settings_path.exists():
        _log_err(f"settings.json not found at {settings_path}")
        return []

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log_err(f"failed to read settings.json: {e}")
        return []

    parsed: list[ParsedCommand] = []
    hooks_section = settings.get("hooks", {})
    if not isinstance(hooks_section, dict):
        return parsed

    for event_name, blocks in hooks_section.items():
        if not isinstance(blocks, list):
            continue
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            matcher = block.get("matcher", "")
            for hook in block.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command", "")
                resolved = parse_command(command, home=home)
                if resolved is not None:
                    # Expand ~ to absolute path (the validator works with absolute paths)
                    resolved = Path(os.path.expanduser(str(resolved)))
                parsed.append(
                    ParsedCommand(
                        event=event_name,
                        matcher=matcher,
                        command=command,
                        resolved_path=resolved,
                        setting_json_index=idx,
                    )
                )

    return parsed


def walk_worktree_settings(
    home: Path,
) -> list[tuple[Path, Path, ParsedCommand]]:
    """
    Discover and parse settings.json AND settings.local.json in each worktree
    under home/.worktrees/.

    Returns a flat list of (worktree_root_path, settings_file_path, ParsedCommand)
    tuples. The worktree_root_path identifies which worktree the command came from;
    the settings_file_path names the EXACT source file (settings.json vs
    settings.local.json) so validate_registrations can (a) populate
    ForwardMismatch.source_settings precisely and (b) keep settings.local.json
    commands OUT of the reverse-direction registered_basenames union (FORWARD
    audit only — reverse behavior byte-identical, rp1-home #1943 V6).

    Anchored to the passed `home` parameter — NOT $HOME or Path.home() — so
    that pre-commit's `--home $TMPDIR` invocation correctly evaluates to
    an empty list (the extracted staged tree has no .worktrees/ subdir, since
    worktrees live outside any git index). This preserves the pre-commit
    behavior bit-for-bit (R4).

    Missing or unreadable worktree settings files are skipped silently:
    walk_settings_json already handles JSON parse errors and OSError via
    fail-open semantics (returns empty list, logs to stderr).

    Returns an empty list if home/.worktrees/ doesn't exist (typical for
    fresh checkouts, CI sandboxes, or the pre-commit tmpdir).

    Added by rp1-home #786 to close the session-bricking gap from #743; extended
    to settings.local.json by rp1-home #1943 (the per-worktree leak vector —
    pre-9bd88a77 worktrees carry a tracked settings.local.json dangling on RP1).
    """
    worktrees_dir = home / ".worktrees"
    if not worktrees_dir.exists() or not worktrees_dir.is_dir():
        return []

    result: list[tuple[Path, Path, ParsedCommand]] = []
    try:
        for entry in sorted(worktrees_dir.iterdir()):
            if not entry.is_dir():
                continue
            for basename in ("settings.json", "settings.local.json"):
                wt_settings = entry / ".claude" / basename
                if not wt_settings.exists():
                    continue
                for cmd in walk_settings_json(wt_settings, home=home):
                    result.append((entry, wt_settings, cmd))
    except OSError as e:
        _log_err(f"failed to walk worktrees dir {worktrees_dir}: {e}")

    return result


def walk_hooks_dir(hooks_dir: Path) -> set[str]:
    """
    Return basenames of every .py file directly in hooks_dir/ (top level only).

    Excludes: __init__.py, __pycache__/*, files in lib/, tests/, state/, .sentinels/,
    and entries in KNOWN_NON_HOOK_FILES.
    """
    if not hooks_dir.exists() or not hooks_dir.is_dir():
        return set()

    result: set[str] = set()
    try:
        for entry in hooks_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.suffix == ".py":
                continue
            if entry.name == "__init__.py":
                continue
            if entry.name in KNOWN_NON_HOOK_FILES:
                continue
            result.add(entry.name)
    except OSError as e:
        _log_err(f"failed to walk hooks_dir {hooks_dir}: {e}")

    return result


def walk_detector_files(hooks_dir: Path) -> set[str]:
    """
    Return basenames of top-level *-detector.py files in hooks_dir.

    A detector is a file whose name matches `*-detector.py` AND which imports
    `check_and_claim_priority` from `lib.hook_priorities`. This is the
    qualification rule (R3): only files that intend to participate in the
    priority mutex are checked for registry membership.
    """
    detectors: set[str] = set()
    for name in walk_hooks_dir(hooks_dir):
        if not name.endswith("-detector.py"):
            continue
        path = hooks_dir / name
        if _imports_priority_api(path):
            detectors.add(name)
    return detectors


def _imports_priority_api(file_path: Path) -> bool:
    """
    Check if a file imports `check_and_claim_priority` from `lib.hook_priorities`.

    Substring match is sufficient — the import statement is unambiguous
    enough that we don't need AST parsing here.
    """
    try:
        content = file_path.read_text()
    except OSError:
        return False
    return "check_and_claim_priority" in content and "hook_priorities" in content


def walk_all_intent_files(hooks_dir: Path) -> set[str]:
    """
    Return basenames of every top-level hook file that declares hook INTENT —
    either a `HOOK_EVENT` annotation anywhere in its content OR an import of the
    priority API (`check_and_claim_priority` from `lib.hook_priorities`).

    Built on `walk_hooks_dir`, so `__init__.py`, `lib/`, `tests/`, and entries
    in KNOWN_NON_HOOK_FILES are excluded for free — that exclusion is exactly
    what keeps `auto-commit-skill-edits-worker.sh` and the #944 back-compat shim
    out of the --strict report.

    Used only by --strict mode (rp1-home #638) to broaden the reverse-settings
    audit beyond `*-detector.py` files. The `HOOK_EVENT` substring match is
    intentionally permissive — --strict is advisory and accepts a wider
    false-positive surface than the default scope (a `HOOK_EVENT` mention in a
    comment is enough to flag a file).
    """
    intent: set[str] = set()
    for name in walk_hooks_dir(hooks_dir):
        path = hooks_dir / name
        try:
            content = path.read_text()
        except OSError as e:
            _log_err(f"could not read hook file for --strict scan {path}: {e}")
            continue
        if "HOOK_EVENT" in content or _imports_priority_api(path):
            intent.add(name)
    return intent


# ═══════════════════════════════════════════════════════════════════════
# Detector registry / priorities / test extraction
# ═══════════════════════════════════════════════════════════════════════


def extract_registry_names(registry_path: Path) -> tuple[set[str], set[str]]:
    """
    Return (priority_group_names, independent_group_names) from detector_registry.py.

    Parses the file with `ast` to find PRIORITY_GROUP and INDEPENDENT_GROUP
    assignments. Each list contains dicts with a "name" key. Returns the set
    of name values from each list.

    Fail-open: returns ({}, {}) on parse error.
    """
    if not registry_path.exists():
        return set(), set()

    try:
        tree = ast.parse(registry_path.read_text())
    except (OSError, SyntaxError) as e:
        _log_err(f"failed to parse detector_registry.py: {e}")
        return set(), set()

    def collect_names(node: ast.AST) -> set[str]:
        """Walk a list-of-dicts AST node and collect each dict's 'name' value."""
        names: set[str] = set()
        if not isinstance(node, ast.List):
            return names
        for elt in node.elts:
            if not isinstance(elt, ast.Dict):
                continue
            for k, v in zip(elt.keys, elt.values):
                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                    if isinstance(v.value, str):
                        names.add(v.value)
        return names

    priority: set[str] = set()
    independent: set[str] = set()
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "PRIORITY_GROUP":
                priority = collect_names(stmt.value)
            elif target.id == "INDEPENDENT_GROUP":
                independent = collect_names(stmt.value)

    return priority, independent


def extract_hook_registry_names(registry_path: Path) -> set[str]:
    """
    Return the set of hook module basenames (no extension) registered in
    hook_registry.py's EVENT_REGISTRY — the #1730 PreToolUse/PostToolUse dispatcher
    registry (rp1-home #1732, R5).

    Membership here means the hook is dispatched by hook-dispatcher.py, so after the
    #1732 cutover removes a hook's direct settings.json entry, its EVENT_REGISTRY
    membership is what keeps it "registered". This lets the reverse-settings audit
    treat the departing `*-detector.py` migrants (and future ones) as registered
    instead of flagging them as orphaned.

    Collects the `module` value (2nd positional arg, or `module=` kwarg) from every
    `Entry(...)` call in the file. hook_registry.py instantiates `Entry` only inside
    EVENT_REGISTRY, so walking all Entry() calls is equivalent to (and more robust
    than) matching the `EVENT_REGISTRY: dict[...] = {...}` AnnAssign directly.

    Parses with `ast` (mirrors extract_registry_names) — reads the file under the
    passed `home`, never imports the live module, so the `--home <staged-tree>`
    pre-commit path validates what's about to commit rather than the live tree.

    Fail-open: returns empty set on parse error / missing file.
    """
    if not registry_path.exists():
        return set()

    try:
        tree = ast.parse(registry_path.read_text())
    except (OSError, SyntaxError) as e:
        _log_err(f"failed to parse hook_registry.py: {e}")
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Entry"):
            continue
        module_val = None
        # module is the 2nd positional arg (Entry(matcher, module, ...)) ...
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            module_val = node.args[1].value
        # ... or the `module=` keyword.
        else:
            for kw in node.keywords:
                if kw.arg == "module" and isinstance(kw.value, ast.Constant):
                    module_val = kw.value.value
                    break
        if isinstance(module_val, str):
            names.add(module_val)
    return names


def _entry_module(node: ast.Call) -> Optional[str]:
    """Return the `module` basename from an `Entry(...)` call node, or None.

    Mirrors the module-extraction logic in extract_hook_registry_names: the 2nd
    positional arg (``Entry(matcher, module, ...)``) or the ``module=`` keyword.
    """
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        if isinstance(node.args[1].value, str):
            return node.args[1].value
    for kw in node.keywords:
        if kw.arg == "module" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return kw.value.value
    return None


def extract_hook_registry_events(registry_path: Path) -> dict[str, list[str]]:
    """
    Return ``{event_key: [module, ...]}`` from hook_registry.py's EVENT_REGISTRY
    dict literal (rp1-home #1990, R1/R4).

    This is the event-GROUPED sibling of extract_hook_registry_names (which flattens
    every Entry() call into a bare name set and whose set[str] signature must stay
    unchanged — #2147 consumes it). The dispatcher-wiring check (R1) needs the per-event
    grouping to answer "does every EVENT_REGISTRY key have exactly one live dispatcher
    line?", and the event-name check (R4) needs the key set to answer "is every key a
    legal Claude Code event?".

    Parses the ``EVENT_REGISTRY: dict[...] = {...}`` literal via `ast` (never imports
    the module), reading under the passed path so the `--home <staged-tree>` pre-commit
    mode validates what is about to commit. An event whose value is not a list literal,
    or an Entry whose module is not a string constant, is skipped silently.

    Fail-open: returns an empty dict on parse error / missing file / absent EVENT_REGISTRY
    — a tree with no hook_registry.py has no wiring invariant to enforce.
    """
    if not registry_path.exists():
        return {}

    try:
        tree = ast.parse(registry_path.read_text())
    except (OSError, SyntaxError) as e:
        _log_err(f"failed to parse hook_registry.py for events: {e}")
        return {}

    events: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        # EVENT_REGISTRY may be an annotated assignment (AnnAssign) or a plain Assign.
        target_dict: Optional[ast.Dict] = None
        if isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == "EVENT_REGISTRY" and isinstance(node.value, ast.Dict):
                target_dict = node.value
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "EVENT_REGISTRY" for t in node.targets) and isinstance(node.value, ast.Dict):
                target_dict = node.value
        if target_dict is None:
            continue
        for key_node, val_node in zip(target_dict.keys, target_dict.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            modules: list[str] = []
            if isinstance(val_node, ast.List):
                for elt in val_node.elts:
                    if isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name) and elt.func.id == "Entry":
                        mod = _entry_module(elt)
                        if mod is not None:
                            modules.append(mod)
            events[key_node.value] = modules
        break  # first EVENT_REGISTRY literal wins
    return events


def extract_hook_priorities(priorities_path: Path) -> set[str]:
    """
    Return the set of HOOK_PRIORITIES keys from hook_priorities.py.

    Parses with `ast` to find HOOK_PRIORITIES = {...}. Returns the keys.

    Fail-open: returns empty set on parse error.
    """
    if not priorities_path.exists():
        return set()

    try:
        tree = ast.parse(priorities_path.read_text())
    except (OSError, SyntaxError) as e:
        _log_err(f"failed to parse hook_priorities.py: {e}")
        return set()

    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name) or target.id != "HOOK_PRIORITIES":
                continue
            if not isinstance(stmt.value, ast.Dict):
                continue
            keys: set[str] = set()
            for k in stmt.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
            return keys

    return set()


def extract_test_expected_hooks(test_path: Path) -> set[str]:
    """
    Return the `expected_hooks` set literal from test_hook_priorities.py.

    Uses ast parsing primarily; falls back to substring matching if AST
    extraction fails (the literal might be refactored into a fixture or
    list-comprehension in the future).

    Fail-open: returns empty set if neither approach finds anything.
    """
    if not test_path.exists():
        return set()

    # Try AST extraction first
    try:
        tree = ast.parse(test_path.read_text())
    except (OSError, SyntaxError) as e:
        _log_err(f"failed to parse test_hook_priorities.py for AST: {e}")
        return _extract_test_expected_via_substring(test_path)

    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name) or target.id != "expected_hooks":
                continue
            if not isinstance(stmt.value, ast.Set):
                continue
            names: set[str] = set()
            for elt in stmt.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
            if names:
                return names

    # AST didn't find a Set literal; fall back to substring
    return _extract_test_expected_via_substring(test_path)


def _extract_test_expected_via_substring(test_path: Path) -> set[str]:
    """
    Fallback: scan test source for double-quoted strings inside the
    test_all_hooks_registered function body. Conservative heuristic.
    """
    try:
        content = test_path.read_text()
    except OSError:
        return set()

    # Locate the test_all_hooks_registered method block heuristically.
    # Match from "def test_all_hooks_registered" to the next top-level def
    # or class boundary (8-space indented or less).
    match = re.search(
        r"def\s+test_all_hooks_registered\b.*?(?=\n\s{0,8}def\s|\n\s{0,4}class\s|\Z)",
        content,
        re.DOTALL,
    )
    if not match:
        return set()
    block = match.group(0)
    # Extract double-quoted identifier-like names (detector basenames)
    names = set(re.findall(r'"([a-z][a-z0-9-]+-detector)"', block))
    return names


# ═══════════════════════════════════════════════════════════════════════
# Contract conformance (R-contract / rp1-home #862)
# ═══════════════════════════════════════════════════════════════════════


def _defines_run_detection(detector_path: Path) -> bool:
    """
    True if the detector file at `detector_path` defines a MODULE-LEVEL
    `run_detection` function (sync or async).

    Approximates the dispatcher's runtime contract — `getattr(module,
    "run_detection", None)` in unified-prompt-dispatcher.py — via a static AST
    parse (no import/exec, so the validator stays side-effect-free and fast).
    We scan only the TOP LEVEL of the module body (`tree.body`, NOT
    `ast.walk`): the runtime `getattr` resolves only module-level attributes,
    so a `run_detection` nested inside a class or another function would NOT
    satisfy the dispatcher and must be treated as missing.

    Fail-open per the validator's philosophy:
        - file missing        → False (flagged: a registry member with no file
                                is a latent non-functional detector — #862 class)
        - OSError on read     → True  (skip: transient FS/permission issue; do
                                not flag, mirrors _imports_priority_api's
                                OSError→benign handling)
        - SyntaxError on parse → False (flagged: a syntax-errored detector
                                cannot load at runtime either, so it IS broken)
        - parses cleanly       → presence of a top-level FunctionDef/
                                AsyncFunctionDef named "run_detection"
    """
    if not detector_path.exists():
        return False
    try:
        source = detector_path.read_text()
    except OSError as e:
        _log_err(f"could not read detector for contract check {detector_path}: {e}")
        return True  # fail-open: do not flag on a transient read error
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        _log_err(f"syntax error parsing detector for contract check {detector_path}: {e}")
        return False  # cannot load at runtime → contract not satisfied
    for node in tree.body:  # top-level only — matches getattr(module, ...) semantics
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_detection":
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# Plugin fragments (rp1-home #2213) — repo-tree single-surface rules
# ═══════════════════════════════════════════════════════════════════════
#
# Static, read-only, NEVER imports a fragment: the dispatcher imports fragments on every
# dispatch, so the contract's "side-effect-free at import" rule protects it; this validator
# runs in the pre-commit and the closer and must not execute plugin code at all (KD-4).


@dataclass
class FragmentStatic:
    """The contract surface of one `registry-fragment.py`, read with `ast` (never imported).

    event_entries: event → entry `module` strings, in declaration order.
    detectors: [{"name", "path" (declared or None), "position_declared"}].
    native_check: the NATIVE_SESSIONSTART_CHECK string, or None.
    problems: why a declaration could not be read statically (→ `fragment_not_static`).
    parse_error: set when the file cannot be read or parsed (→ `fragment_unparseable`).
    """

    event_entries: dict[str, list[str]] = field(default_factory=dict)
    detectors: list[dict] = field(default_factory=list)
    native_check: Optional[str] = None
    lib_api_version: object = None
    problems: list[str] = field(default_factory=list)
    parse_error: Optional[str] = None

    def comparable(self) -> dict:
        """The static names the installed-set parity check compares (R5/Task 3)."""
        return {
            "event_entries": {k: list(v) for k, v in self.event_entries.items()},
            "detectors": [(d["name"], d["path"], d["position_declared"]) for d in self.detectors],
            "native_check": self.native_check,
            "lib_api_version": self.lib_api_version,
            "parse_error": self.parse_error,
        }


_FRAGMENT_SYMBOLS = ("EVENT_ENTRIES", "DETECTORS", "NATIVE_SESSIONSTART_CHECK", "LIB_API_VERSION")


def _is_entry_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "Entry") or (
        isinstance(func, ast.Attribute) and func.attr == "Entry"
    )


def _symbol_root(node: ast.AST) -> Optional[str]:
    """The contract symbol at the base of a subscript/attribute chain (`EVENT_ENTRIES["X"].y`)."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) and node.id in _FRAGMENT_SYMBOLS else None


def _non_plain_bindings(tree: ast.Module) -> list[str]:
    """Every binding or mutation of a contract symbol OTHER than a plain top-level assignment.

    The dispatcher IMPORTS the fragment, so it sees each symbol's final value however it was
    built; this reader sees only plain top-level assignments. An assignment inside if/try/for/
    with/def, a subscript or attribute store (`EVENT_ENTRIES["Stop"] = …`), an augmented
    assignment (`|=`, `+=`), a method call (`.append(…)`, `.update(…)`), `del`, or an import or
    def bound to the name would make the two disagree — the loader would register a module this
    validator never checked. Each such site is reported (→ `fragment_not_static`), never ignored.
    """
    accepted: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            accepted.update(id(t) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            accepted.add(id(node.target))

    found: list[str] = []

    def note(name: str, how: str, node: ast.AST) -> None:
        msg = f"{name} {how} (line {getattr(node, 'lineno', '?')}) — only a plain top-level literal assignment is static"
        if msg not in found:
            found.append(msg)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if (
                node.id in _FRAGMENT_SYMBOLS
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and id(node) not in accepted
            ):
                note(node.id, "is bound or deleted outside a plain top-level assignment", node)
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            name = _symbol_root(node)
            if name:
                note(name, "is mutated in place", node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = _symbol_root(node.func.value)
            if name:
                note(name, f"has a .{node.func.attr}(…) call that can mutate it", node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in _FRAGMENT_SYMBOLS:
                    note(bound, "is bound by an import", node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _FRAGMENT_SYMBOLS:
                note(node.name, "is bound by a def/class statement", node)
    return found


def extract_fragment_names(fragment_path: Path) -> FragmentStatic:
    """Read a fragment's four contract symbols with `ast` (rp1-home #2213, KD-4).

    Mirrors extract_hook_registry_names / extract_registry_names: module-level assignments
    only (last one wins, as at import), literals only. A SyntaxError (or an unreadable file)
    sets `parse_error`; a declaration that is not a literal — or a contract symbol bound or
    mutated anywhere other than a plain top-level assignment (`_non_plain_bindings`) — is
    recorded in `problems`, because the contract makes a fragment pure data and a computed or
    later-mutated value would blind every rule below.
    """
    static = FragmentStatic()
    try:
        tree = ast.parse(fragment_path.read_text(), filename=str(fragment_path))
    except (OSError, SyntaxError, ValueError, RecursionError) as e:
        static.parse_error = f"{type(e).__name__}: {e}"
        return static

    assigned: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _FRAGMENT_SYMBOLS:
                    assigned[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id in _FRAGMENT_SYMBOLS:
                assigned[node.target.id] = node.value
    static.problems.extend(_non_plain_bindings(tree))

    entries_node = assigned.get("EVENT_ENTRIES")
    if entries_node is not None:
        if not isinstance(entries_node, ast.Dict):
            static.problems.append("EVENT_ENTRIES is not a dict literal")
        else:
            for key, value in zip(entries_node.keys, entries_node.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    static.problems.append("EVENT_ENTRIES has a non-literal (or ** spread) key")
                    continue
                if not isinstance(value, (ast.List, ast.Tuple)):
                    static.problems.append(f"EVENT_ENTRIES[{key.value!r}] is not a list literal")
                    continue
                modules: list[str] = []
                for elt in value.elts:
                    module = _entry_module(elt) if isinstance(elt, ast.Call) and _is_entry_call(elt) else None
                    if module is None:
                        static.problems.append(
                            f"EVENT_ENTRIES[{key.value!r}] item is not an Entry(...) call with a "
                            "string-literal module"
                        )
                        continue
                    modules.append(module)
                static.event_entries[key.value] = modules

    detectors_node = assigned.get("DETECTORS")
    if detectors_node is not None:
        if not isinstance(detectors_node, (ast.List, ast.Tuple)):
            static.problems.append("DETECTORS is not a list literal")
        else:
            for index, elt in enumerate(detectors_node.elts):
                if not isinstance(elt, ast.Dict) or any(
                    not (isinstance(k, ast.Constant) and isinstance(k.value, str)) for k in elt.keys
                ):
                    static.problems.append(f"DETECTORS[{index}] is not a dict literal with string keys")
                    continue
                fields: dict[str, ast.expr] = {
                    k.value: v
                    for k, v in zip(elt.keys, elt.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                name_node, path_node = fields.get("name"), fields.get("path")
                if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
                    static.problems.append(f"DETECTORS[{index}] has no string-literal `name`")
                    continue
                if path_node is not None and not (
                    isinstance(path_node, ast.Constant) and isinstance(path_node.value, str)
                ):
                    static.problems.append(f"DETECTORS[{index}] `path` is not a string literal")
                    continue
                static.detectors.append({
                    "name": name_node.value,
                    "path": path_node.value if path_node is not None else None,
                    "position_declared": "priority" in fields or "before" in fields,
                })

    native_node = assigned.get("NATIVE_SESSIONSTART_CHECK")
    if native_node is not None:
        if isinstance(native_node, ast.Constant) and (
            native_node.value is None or isinstance(native_node.value, str)
        ):
            static.native_check = native_node.value or None
        else:
            static.problems.append("NATIVE_SESSIONSTART_CHECK is not a string literal or None")

    version_node = assigned.get("LIB_API_VERSION")
    if isinstance(version_node, ast.Constant):
        static.lib_api_version = version_node.value
    return static


def _plugin_path(root: Path, rel: str) -> tuple[Path, bool]:
    """Resolve a fragment-relative path against the plugin root → (normalised path, inside root)."""
    joined = os.path.normpath(rel if os.path.isabs(rel) else os.path.join(root, rel))
    root_s = os.path.normpath(root)
    return Path(joined), joined == root_s or joined.startswith(root_s + os.sep)


def _strip_script_ext(name: str) -> str:
    """Bare module basename (no directory, no .py/.sh) — the loader's shadow-rule key."""
    base = os.path.basename(name)
    for ext in (".py", ".sh"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _finding(kind: str, path: Optional[str], detail: str) -> dict:
    item: dict = {"kind": kind}
    if path:
        item["path"] = path
    item["detail"] = detail
    return item


def discover_repo_plugins(home: Path) -> list[Path]:
    """Fragment-bearing plugin dirs in the validated tree: `<home>/plugins/*/hooks/registry-fragment.py`.

    REPO-TREE mode (KD-1): the pre-commit validates the extracted staged index and the worker
    pre-flight a worktree root — neither is installed config state, so this is a plain directory
    walk, never discovery. A plugin dir without a fragment is not one of ours (R1) and is skipped.
    """
    plugins_dir = home / PLUGINS_DIRNAME
    if not plugins_dir.is_dir():
        return []
    found: list[Path] = []
    try:
        for entry in sorted(plugins_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and (entry / FRAGMENT_RELPATH).is_file():
                found.append(entry)
    except OSError as e:
        _log_err(f"failed to walk plugins dir {plugins_dir}: {e}")
    return found


def _read_json_file(path: Path) -> tuple[object, Optional[str]]:
    try:
        return json.loads(path.read_text()), None
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def _marketplace_entry_for(marketplace: object, name: str) -> Optional[dict]:
    """The `.claude-plugin/marketplace.json` entry whose `source` is `./plugins/<name>` (R9)."""
    if not isinstance(marketplace, dict):
        return None
    want = os.path.normpath(os.path.join(PLUGINS_DIRNAME, name))
    for entry in marketplace.get("plugins") or []:
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, str) and not os.path.isabs(source):
            if os.path.normpath(source) == want:
                return entry
    return None


def _events_map(obj: object) -> object:
    """A native hooks object in either shape: wrapped `{"hooks": {...}}` or the bare events map."""
    if isinstance(obj, dict) and isinstance(obj.get("hooks"), dict):
        return obj["hooks"]
    return obj


def _police_events_map(
    events_map: object,
    *,
    source: str,
    kind: str,
    allowed: Optional[Path],
    root: Path,
    home: Path,
    out: dict,
) -> None:
    """R3 core: every handler in a settings-shaped events map must be the ONE allowed check.

    `allowed` is the absolute NATIVE_SESSIONSTART_CHECK path (None ⇒ nothing is allowed). A
    handler passes only when it is a `command` handler under `SessionStart` whose script
    resolves to `allowed`; quoted-`${CLAUDE_PLUGIN_ROOT}` `command` and exec-form `args` are
    normalised to the same path (#2381 F-H14). Non-`command` handler types are rejected.
    """
    if not isinstance(events_map, dict):
        out["violations"].append(_finding("native_hooks_unreadable", source, "hook declarations are not an object"))
        return
    for event, blocks in events_map.items():
        handlers: list = []
        for block in blocks if isinstance(blocks, list) else [None]:
            inner = block.get("hooks") if isinstance(block, dict) else None
            if not isinstance(inner, list):
                out["violations"].append(
                    _finding(kind, source, f"{event}: malformed matcher block (expected {{\"hooks\": [...]}})")
                )
                continue
            handlers.extend(inner)
        for handler in handlers:
            if not isinstance(handler, dict):
                out["violations"].append(_finding(kind, source, f"{event}: handler is not an object"))
                continue
            htype = handler.get("type", "command")
            if htype != "command":
                out["violations"].append(
                    _finding(
                        "handler_type_not_allowed",
                        source,
                        f"{event}: handler type {htype!r} — a plugin's native surface may carry only "
                        "the `command` SessionStart check; route behaviour through the fragment",
                    )
                )
                continue
            command, args = handler.get("command"), handler.get("args")
            exec_form = False
            chained: Optional[str] = None
            resolved: Optional[Path] = None
            if isinstance(command, str) and command.strip():
                shown = command
                resolved = parse_command(command, home=home, plugin_root=root)
                chained = _shell_control_operator(command)
            elif isinstance(args, list) and args and all(isinstance(a, str) for a in args):
                exec_form = True  # argv form: no shell, so no control operators to check
                shown = " ".join(args)
                resolved = _script_from_tokens([_expand_plugin_vars(a, home, root) for a in args])
            else:
                shown = json.dumps(handler, sort_keys=True)
            if resolved is not None:
                resolved = Path(os.path.normpath(os.path.expanduser(str(resolved))))
            if event == "SessionStart" and chained is not None:
                # `check.sh && other.py` would run a SECOND native script outside the dispatcher
                # behind the one sanctioned check. Checked BEFORE the path match so the verdict
                # never depends on how the tokeniser happens to split `check.sh;other`.
                out["violations"].append(
                    _finding(
                        kind,
                        source,
                        f"SessionStart command {shown!r} chains another command ({chained!r}) — "
                        "the native entry must be exactly the NATIVE_SESSIONSTART_CHECK",
                    )
                )
                continue
            if event == "SessionStart" and allowed is not None and resolved == allowed:
                if exec_form:
                    out["advisories"].append(
                        _finding(
                            "exec_form_not_fired",
                            source,
                            "the NATIVE_SESSIONSTART_CHECK handler uses exec-form `args`, which does not "
                            "fire on Claude Code 2.1.280 (#2381 F-H14) — use the quoted "
                            "`\"${CLAUDE_PLUGIN_ROOT}\"/<script>` `command` form",
                        )
                    )
                continue
            if event == "SessionStart":
                expected = _rel(allowed, root) if allowed is not None else "None"
                detail = (
                    f"SessionStart command {shown!r} is not the fragment's "
                    f"NATIVE_SESSIONSTART_CHECK ({expected})"
                )
            else:
                detail = (
                    f"{event} hook {shown!r} registered natively — register it in the fragment's "
                    "EVENT_ENTRIES instead"
                )
            out["violations"].append(_finding(kind, source, detail))


def _police_hook_file(path: Path, *, source: str, kind: str, allowed, root, home, out) -> None:
    data, err = _read_json_file(path)
    if err is not None:
        out["violations"].append(_finding("native_hooks_unreadable", source, f"cannot read hooks file: {err}"))
        return
    _police_events_map(_events_map(data), source=source, kind=kind, allowed=allowed, root=root, home=home, out=out)


def _police_hooks_json(root: Path, allowed, home: Path, out: dict) -> None:
    """S1 / S1u / S6 / S11: `<plugin-root>/hooks/hooks.json` (#2381 F-H1, F-H2a/b, F-H9, F-H15)."""
    path = root / NATIVE_HOOKS_RELPATH
    if not path.is_file():
        return
    source = str(NATIVE_HOOKS_RELPATH)
    data, err = _read_json_file(path)
    if err is not None or not isinstance(data, dict):
        detail = f"cannot read hooks.json: {err}" if err else "hooks.json is not a JSON object"
        out["violations"].append(_finding("native_hooks_unreadable", source, detail))
        return
    if "modules" in data:
        out["violations"].append(
            _finding(
                "hooks_module_forbidden",
                source,
                "`modules` (function hooks — early-access, off by default on 2.1.280, F-H9) is not a "
                "sanctioned plugin surface",
            )
        )
    # F-H2b: a top-level PreToolUse/PermissionRequest refuses the plugin whether or not a
    # `hooks` object sits beside it, so this check does not depend on the wrapped shape.
    refused = sorted(k for k in data if k in PLUGIN_TOPLEVEL_REFUSED_EVENTS)
    if refused:
        out["violations"].append(
            _finding(
                "hooksjson_toplevel_event",
                source,
                f"top-level {', '.join(refused)} outside a `hooks` object — Claude Code 2.1.280 "
                "REFUSES the whole plugin (F-H2b)",
            )
        )
    if "hooks" not in data:
        if not refused and "modules" not in data:
            out["violations"].append(
                _finding(
                    "hooksjson_unwrapped",
                    source,
                    "no `hooks` key — Claude Code 2.1.280 loads 0 hooks with a load error (F-H2a); "
                    "use the wrapped {\"hooks\": {\"SessionStart\": [...]}} shape",
                )
            )
        return
    _police_events_map(
        data["hooks"], source=source, kind="native_hook_not_allowed", allowed=allowed, root=root, home=home, out=out
    )


def _police_manifest_hooks(spec, *, source: str, root: Path, allowed, home: Path, out: dict, policed: set) -> None:
    """S2 / S3 / S4: manifest `hooks` as a path, an inline object, or an array of either."""
    items = spec if isinstance(spec, list) else [spec]
    for item in items:
        if isinstance(item, str):
            path, inside = _plugin_path(root, item)
            if os.path.normpath(path) in policed:  # F-H6: the standard hooks.json is loaded once
                continue
            if not inside:
                out["violations"].append(
                    _finding("manifest_hook_not_allowed", source, f"hooks path {item!r} resolves outside the plugin root")
                )
                continue
            policed.add(os.path.normpath(path))
            _police_hook_file(
                path, source=f"{source} → {item}", kind="manifest_hook_not_allowed", allowed=allowed, root=root, home=home, out=out
            )
        elif isinstance(item, dict):
            _police_events_map(
                _events_map(item), source=source, kind="manifest_hook_not_allowed", allowed=allowed, root=root, home=home, out=out
            )
        else:
            out["violations"].append(
                _finding("manifest_hook_not_allowed", source, f"`hooks` value {item!r} is not a path or an object")
            )


def _frontmatter_declares_hooks(md_path: Path) -> bool:
    """True when a markdown file's leading YAML frontmatter has a top-level `hooks:` key."""
    try:
        lines = md_path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return False
        if re.match(r"hooks\s*:", line):
            return True
    return False


def _police_frontmatter(root: Path, out: dict) -> None:
    """S7 / S8 / S9 / S11: skill / command / agent (and skills-dir root SKILL.md) frontmatter `hooks:`."""
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.normpath(dirpath) == os.path.normpath(root)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in PLUGIN_SCAN_SKIP_DIRS
            and not (at_root and d in PLUGIN_ROOT_SKIP_DIRS)
        )
        for fname in sorted(filenames):
            if not fname.endswith(".md"):
                continue
            md_path = Path(dirpath) / fname
            if _frontmatter_declares_hooks(md_path):
                out["violations"].append(
                    _finding(
                        "frontmatter_hook_not_allowed",
                        _rel(md_path, root),
                        "frontmatter `hooks:` — skill hooks are invocation-scoped and command/agent "
                        "hooks are ignored on 2.1.280 (F-H10/F-H11/F-H12); register hooks in the "
                        "fragment's EVENT_ENTRIES",
                    )
                )


def _plugin_report(
    name: str,
    root: Optional[Path],
    *,
    entry_count: int = 0,
    detector_count: int = 0,
    native_check: Optional[str] = None,
) -> dict:
    """A `plugins[]` report entry (R6 shape) — the ONE constructor, so every path (validated,
    fail-closed, installed-only) emits the same keys in the same order. `root=None` marks an
    installed plugin with no repo-tree fragment."""
    return {
        "name": name,
        "root": str(root) if root is not None else None,
        "fragment_path": str(root / FRAGMENT_RELPATH) if root is not None else None,
        "entry_count": entry_count,
        "detector_count": detector_count,
        "native_check": native_check,
        "violations": [],
        "advisories": [],
        "shadowed": [],
    }


def _validate_one_plugin(
    home: Path,
    plugin_dir: Path,
    *,
    marketplace: object,
    marketplace_err: Optional[str],
    core_event_basenames: dict[str, set[str]],
    core_detectors: set[str],
    known_events: set[str],
) -> dict:
    """Run R1 / R2 / R3 / R6 / R9 for one fragment-bearing plugin dir; return its report dict."""
    name = plugin_dir.name
    root = Path(os.path.normpath(plugin_dir))
    fragment = root / FRAGMENT_RELPATH
    out = _plugin_report(name, root)

    # R9: a fragment-bearing plugin nothing can install is a violation.
    entry = _marketplace_entry_for(marketplace, name)
    if entry is None:
        where = f"(marketplace unreadable: {marketplace_err})" if marketplace_err else ""
        out["violations"].append(
            _finding(
                "unlisted_in_marketplace",
                str(MARKETPLACE_RELPATH),
                f"no `.claude-plugin/marketplace.json` entry with source \"./plugins/{name}\" {where}".rstrip(),
            )
        )

    static = extract_fragment_names(fragment)
    if static.parse_error is not None:
        # Without a readable fragment the allow-list is unknown — every other rule would be noise.
        out["violations"].append(_finding("fragment_unparseable", str(FRAGMENT_RELPATH), static.parse_error))
        return out
    out["entry_count"] = sum(len(mods) for mods in static.event_entries.values())
    out["detector_count"] = len(static.detectors)
    out["native_check"] = static.native_check
    for problem in static.problems:
        out["violations"].append(_finding("fragment_not_static", str(FRAGMENT_RELPATH), problem))

    # R1: forward drift — resolve exactly as the loader does (plugin_fragments._entry_script_exists /
    # _detector_path), so a module the dispatcher really runs is never a false positive.
    registered: set[str] = set()
    for event, modules in static.event_entries.items():
        if event not in known_events:  # the loader's check: the dispatcher's wired events
            out["violations"].append(
                _finding(
                    "fragment_invalid_event",
                    str(FRAGMENT_RELPATH),
                    f"EVENT_ENTRIES key {event!r} is not a dispatcher event — its {len(modules)} entr"
                    f"{'y' if len(modules) == 1 else 'ies'} can never fire",
                )
            )
        core_names = core_event_basenames.get(event, set())
        for module in modules:
            path, inside = _plugin_path(root, module)
            hit = next((c for c in (str(path), f"{path}.py", f"{path}.sh") if os.path.isfile(c)), None)
            if not inside:
                out["violations"].append(
                    _finding("missing_module", module, f"{event} entry module resolves outside plugins/{name}/")
                )
            elif hit is None:
                out["violations"].append(
                    _finding("missing_module", module, f"{event} entry module has no file (path, .py or .sh) under plugins/{name}/")
                )
            else:
                registered.add(os.path.normpath(hit))
            if _strip_script_ext(module) in core_names:
                out["shadowed"].append({"event": event, "name": _strip_script_ext(module)})
    for det in static.detectors:
        rel = det["path"] or os.path.join("hooks", f"{det['name']}.py")
        path, inside = _plugin_path(root, rel)
        if not inside or not path.is_file():
            where = "resolves outside" if not inside else "has no file under"
            out["violations"].append(
                _finding("missing_module", rel, f"detector {det['name']!r} {where} plugins/{name}/")
            )
        else:
            registered.add(os.path.normpath(path))
        if det["position_declared"]:
            out["violations"].append(
                _finding(
                    "detector_position_declared",
                    str(FRAGMENT_RELPATH),
                    f"detector {det['name']!r} declares `priority`/`before` — a fragment contributes "
                    "INDEPENDENT detectors only (Position 2)",
                )
            )
        if det["name"] in core_detectors:
            out["shadowed"].append({"name": det["name"]})
    allowed: Optional[Path] = None
    if static.native_check:
        allowed, inside = _plugin_path(root, static.native_check)
        if not inside or not allowed.is_file():
            out["violations"].append(
                _finding("missing_module", static.native_check, f"NATIVE_SESSIONSTART_CHECK has no file under plugins/{name}/")
            )

    # R2: reverse drift — a hook-shaped hooks/*.py the fragment names nowhere is dead code.
    # Skipped when the fragment is not static (the registered set would be incomplete).
    hooks_dir = root / "hooks"
    if not static.problems and hooks_dir.is_dir():
        for path in sorted(hooks_dir.iterdir()):
            if not path.is_file() or path.suffix != ".py" or path.name in ("__init__.py", FRAGMENT_RELPATH.name):
                continue
            if allowed is not None and os.path.normpath(path) == os.path.normpath(allowed):
                continue
            # The core hook-shape heuristics, read decode-safe: a non-UTF-8 helper must not
            # crash the plugin's validation (that would drop its report — a silent pass).
            try:
                content = path.read_text(errors="replace")
            except OSError:
                content = ""
            hook_shaped = (
                path.name.endswith("-detector.py")
                or "HOOK_EVENT" in content
                or ("check_and_claim_priority" in content and "hook_priorities" in content)
            )
            if hook_shaped and os.path.normpath(path) not in registered:
                out["violations"].append(
                    _finding(
                        "unregistered_hook",
                        _rel(path, root),
                        "hook-shaped module named by neither EVENT_ENTRIES nor DETECTORS (dead code)",
                    )
                )

    # R3: every native-hook surface Claude Code 2.1.280 honours (#2381 §6).
    policed: set[str] = {os.path.normpath(root / NATIVE_HOOKS_RELPATH)}
    _police_hooks_json(root, allowed, home, out)  # S1 / S1u / S6 / S11
    manifest_path = root / PLUGIN_MANIFEST_RELPATH
    if manifest_path.is_file():
        manifest, err = _read_json_file(manifest_path)
        if err is not None or not isinstance(manifest, dict):
            out["violations"].append(
                _finding("native_hooks_unreadable", str(PLUGIN_MANIFEST_RELPATH), f"cannot read plugin.json: {err or 'not an object'}")
            )
        elif "hooks" in manifest:  # S2 / S3 / S4
            _police_manifest_hooks(
                manifest["hooks"], source=f"{PLUGIN_MANIFEST_RELPATH} hooks", root=root, allowed=allowed, home=home, out=out, policed=policed
            )
    if isinstance(entry, dict) and "hooks" in entry:  # S5
        entry_hooks = entry["hooks"]
        for item in entry_hooks if isinstance(entry_hooks, list) else [entry_hooks]:
            source = f"{MARKETPLACE_RELPATH} entry hooks"
            if isinstance(item, str):
                out["advisories"].append(
                    _finding(
                        "entry_hook_path_ignored",
                        source,
                        f"marketplace-entry `hooks` path {item!r} installs but fires NOTHING on 2.1.280 "
                        "(F-H8) — only an inline-object entry takes effect",
                    )
                )
            else:
                _police_events_map(
                    _events_map(item), source=source, kind="manifest_hook_not_allowed", allowed=allowed, root=root, home=home, out=out
                )
    settings_path = root / "settings.json"
    if settings_path.is_file():  # S10
        settings, err = _read_json_file(settings_path)
        if isinstance(settings, dict) and "hooks" in settings:
            out["violations"].append(
                _finding(
                    "settings_hook_not_allowed",
                    "settings.json",
                    "a plugin settings.json `hooks` block is dropped by Claude Code 2.1.280 (F-H13); "
                    "register hooks in the fragment's EVENT_ENTRIES",
                )
            )
    _police_frontmatter(root, out)  # S7 / S8 / S9 / S11
    return out


def validate_plugin_fragments(
    home: Path,
    core_event_basenames: dict[str, set[str]],
    core_detectors: set[str],
    known_events: Optional[set[str]] = None,
) -> list[dict]:
    """Repo-tree plugin rules for every fragment-bearing `<home>/plugins/<name>/` (rp1-home #2213).

    `known_events` mirrors the loader's `_known_events()` — the EVENT_REGISTRY keys the
    dispatcher is wired for in the validated tree (default: the legal VALID_EVENTS).
    Returns [] when the tree holds no fragment-bearing plugin, so the caller's report — and the
    rendered output — is untouched (R8).
    """
    known = set(known_events) if known_events else set(VALID_EVENTS)
    plugin_dirs = discover_repo_plugins(home)
    if not plugin_dirs:
        return []
    marketplace, marketplace_err = _read_json_file(home / MARKETPLACE_RELPATH)
    reports: list[dict] = []
    for plugin_dir in plugin_dirs:
        try:
            reports.append(
                _validate_one_plugin(
                    home,
                    plugin_dir,
                    marketplace=marketplace,
                    marketplace_err=marketplace_err,
                    core_event_basenames=core_event_basenames,
                    core_detectors=core_detectors,
                    known_events=known,
                )
            )
        except Exception as e:  # noqa: BLE001 — isolate per plugin, but FAIL CLOSED
            # A plugin we could not validate must not vanish from the report (exit 0 would
            # hide its real violations). Report it as a hard violation instead — the same
            # outcome a core internal error gets (exit 2 → the pre-commit blocks).
            _log_err(f"plugin {plugin_dir.name}: validation error {e!r}")
            failed = _plugin_report(plugin_dir.name, Path(os.path.normpath(plugin_dir)))
            failed["violations"].append(
                _finding("plugin_validation_error", None, f"the validator could not check this plugin: {e!r}")
            )
            reports.append(failed)
    return reports


@contextlib.contextmanager
def _block_logger_disabled():
    """Keep the read-only validator read-only: `discover_plugins()` logs discovery failures to the
    block queue (which process-blocks.py turns into issues). The dispatcher already reports those
    durably; here they go to stderr only, which the closer's audit output shows."""
    previous = os.environ.get("CLAUDE_BLOCK_LOGGER_DISABLED")
    os.environ["CLAUDE_BLOCK_LOGGER_DISABLED"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CLAUDE_BLOCK_LOGGER_DISABLED", None)
        else:
            os.environ["CLAUDE_BLOCK_LOGGER_DISABLED"] = previous


def _import_discover_plugins():
    """Dual-path import of #2212's discovery (R5: never a second discovery). None on failure."""
    try:
        try:
            from lib.plugin_fragments import discover_plugins  # type: ignore
        except ImportError:
            from plugin_fragments import discover_plugins  # type: ignore
        return discover_plugins
    except Exception as e:  # noqa: BLE001 — fail-open: the parity check is informational
        _log_err(f"--plugins-installed: cannot import lib/plugin_fragments.discover_plugins ({e!r}); parity check skipped")
        return None


def apply_installed_parity(reports: list[dict]) -> None:
    """Closer-only installed-set parity (R5 / Task 3, `--plugins-installed`) — INFORMATIONAL.

    For each enabled fragment-bearing plugin `discover_plugins()` resolves (per consolidated D1:
    root_kind "source" = a directory/file marketplace running in place, "cache" = an installPath
    copy), compare its fragment's static names with the repo-tree fragment of the same name and
    attach `installed: {key, root, root_kind, differs}`. An installed plugin with no repo-tree
    counterpart is appended with `differs: None`. Never touches settings; never affects the exit code.
    """
    discover = _import_discover_plugins()
    if discover is None:
        return
    with _block_logger_disabled():
        try:
            installed = discover()
        except Exception as e:  # noqa: BLE001 — discovery is fail-open by contract
            _log_err(f"--plugins-installed: discovery failed ({e!r}); parity check skipped")
            return
    by_name = {r["name"]: r for r in reports}
    orphans: list[dict] = []
    for info in installed:
        installed_static = extract_fragment_names(Path(info.root) / FRAGMENT_RELPATH)
        row = {"key": info.key, "root": info.root, "root_kind": info.root_kind, "differs": None}
        report = by_name.get(info.name)
        if report is not None and report.get("fragment_path"):
            repo_static = extract_fragment_names(Path(report["fragment_path"]))
            row["differs"] = installed_static.comparable() != repo_static.comparable()
            report["installed"] = row
        else:
            orphan = _plugin_report(
                info.name,
                None,
                entry_count=sum(len(m) for m in installed_static.event_entries.values()),
                detector_count=len(installed_static.detectors),
                native_check=installed_static.native_check,
            )
            orphan["installed"] = row
            orphans.append(orphan)
    reports.extend(sorted(orphans, key=lambda r: r["name"]))


# ═══════════════════════════════════════════════════════════════════════
# Main validation entry point
# ═══════════════════════════════════════════════════════════════════════


def validate_registrations(
    home: Optional[Path] = None,
    strict: bool = False,
    project_dir: Optional[Path] = None,
    plugins_installed: bool = False,
) -> ValidationReport:
    """
    Run bidirectional registration validation.

    Args:
        home: Directory tree root that contains `.claude/`. Defaults to `Path.home()`.
        strict: When True (--strict, rp1-home #638), additionally populate
            `report.reverse_mismatches_strict_settings` with non-detector hook
            files that declare intent but lack any registration. When False
            (default), that list stays empty and behavior is byte-identical to
            the pre-#638 validator.
        project_dir: When passed explicitly (rp1-home #1943), FORWARD-scan a
            project-level checkout's `.claude/settings.json` + `.claude/settings.local.json`
            for host-absent hook paths (the SL7 leak location). Scanned ONLY when it
            is DIFFERENT from `home` (avoids double-counting when the checkout IS `~`).
            This function NEVER resolves a project dir on its own and makes NO git
            call — all resolution/fallback lives in `main()` and runs only when
            `--home` is absent (R5/V3 hermeticity: `--home` → no project scan, no git).
        plugins_installed: When True (`--plugins-installed`, rp1-home #2213 — the closer
            passes it), attach INFORMATIONAL installed-set parity rows to `report.plugins`
            via lib/plugin_fragments.discover_plugins(). Never affects the exit code.

    Returns:
        ValidationReport with all mismatch lists populated.

    The function is pure: it reads files under `home` (and an explicit `project_dir`)
    and returns a report. Pre-commit's staged-tree extraction passes a `tmp` dir as
    `home` to validate "what's about to commit" rather than the live filesystem.
    """
    if home is None:
        home = Path.home()
    home = Path(home).resolve()

    report = ValidationReport()

    settings_path = home / ".claude" / "settings.json"
    hooks_dir = home / ".claude" / "hooks"
    registry_path = hooks_dir / "lib" / "detector_registry.py"
    priorities_path = hooks_dir / "lib" / "hook_priorities.py"
    test_path = hooks_dir / "tests" / "test_hook_priorities.py"

    # ─────────────────────────────────────────────────────────────
    # Inventory
    # ─────────────────────────────────────────────────────────────
    parsed_commands = walk_settings_json(settings_path, home=home)
    hooks_files = walk_hooks_dir(hooks_dir)
    detector_files = walk_detector_files(hooks_dir)
    priority_names, independent_names = extract_registry_names(registry_path)
    hook_priority_keys = extract_hook_priorities(priorities_path)
    test_expected = extract_test_expected_hooks(test_path)
    registry_names = priority_names | independent_names
    # R5 (rp1-home #1732): EVENT_REGISTRY (hook_registry.py) membership — the #1730
    # PreToolUse/PostToolUse dispatcher registry. Kept SEPARATE from registry_names
    # so it feeds ONLY the reverse-settings audit (Layer 3 below) and NEVER the #953
    # dual-registration check: during the shadow-soak/pre-flip window a migrating hook
    # is legitimately in BOTH hook_registry.py AND settings.json, which must not read
    # as a dual registration. Minimal tolerance only — the strict single-surface FAIL
    # rule is #1735.
    hook_registry_names = extract_hook_registry_names(hooks_dir / "lib" / "hook_registry.py")

    # Names that appear in settings.json (any event) — used as "registered in Layer 1"
    registered_basenames: set[str] = set()
    for pc in parsed_commands:
        if pc.resolved_path is not None:
            registered_basenames.add(pc.resolved_path.name)

    # ─────────────────────────────────────────────────────────────
    # Dual registration (rp1-home #953): registry member ALSO directly registered
    # ─────────────────────────────────────────────────────────────
    # A detector that is both a detector_registry.py member (owned + fired by the
    # unified dispatcher) AND directly registered in the MAIN settings.json fires
    # twice per UserPromptSubmit. Scope is MAIN-settings-only: snapshot
    # registered_basenames here, BEFORE the worktree union below, so in-flight
    # worktree branches still carrying the pre-fix direct registrations do not
    # produce false positives on live worker/closer invocations. The dispatcher
    # itself and the standalones (pm-inbox-checker, skill-creator-augment-detector)
    # are naturally exempt — they are not in registry_names.
    main_registered_basenames = set(registered_basenames)
    report.dual_registrations = sorted(
        name for name in registry_names if f"{name}.py" in main_registered_basenames
    )

    # ─────────────────────────────────────────────────────────────
    # Dispatcher wiring (rp1-home #1990 / R1, R2) + invalid event keys (R4)
    # ─────────────────────────────────────────────────────────────
    # The invariant the validator never checked: every lib/hook_registry.py
    # EVENT_REGISTRY key is served by exactly one live `hook-dispatcher.py <event>`
    # line in the MAIN settings.json. validate_registrations verifies that PRESENT
    # registrations are ALLOWED (single-surface) — it never verified every registry
    # key HAS its dispatcher line, so deleting `hooks.Stop` silently unwired all Stop
    # members while the run stayed `OK`, exit 0 (the #1990 blind spot; the original
    # "registered-but-unregeneratable" framing is now triple-gated by #1735/#1983 and
    # cannot fire on any config that passes the commit gate — the live gap is wiring).
    #
    # Scope is MAIN settings.json ONLY — iterate `parsed_commands` (parsed at :inventory
    # above, BEFORE the worktree union in the forward scan below), the same MAIN-only
    # snapshot discipline the #953 dual-registration check uses, so in-flight worktree
    # branches never false-positive. Fail-open: an empty EVENT_REGISTRY (no file / parse
    # error) means there is no wiring invariant to enforce — skip entirely.
    registry_events = extract_hook_registry_events(hooks_dir / "lib" / "hook_registry.py")
    if registry_events:
        # Count correctly-wired dispatcher lines per settings event. A line counts for
        # event E iff its resolved basename ∈ DISPATCHER_ALLOWED_COMMANDS, it is not a
        # --shadow line, it sits under settings event E (pc.event), AND its trailing
        # `<event>` argument also names E (a `Stop` arg under PreToolUse wires nothing —
        # the dispatcher would run the wrong event's registry).
        wiring_mismatches: list[str] = []
        dispatcher_counts: dict[str, int] = {}
        for pc in parsed_commands:  # MAIN settings only (worktree union is later)
            if pc.resolved_path is None or pc.resolved_path.name not in DISPATCHER_ALLOWED_COMMANDS:
                continue
            if "--shadow" in pc.command:
                continue
            tokens = pc.command.split()
            arg_event = tokens[-1] if tokens else ""
            if arg_event != pc.event:
                wiring_mismatches.append(
                    f"{pc.event}: dispatcher line passes arg '{arg_event}' (≠ its "
                    f"registration event) — wires nothing"
                )
                continue
            dispatcher_counts[pc.event] = dispatcher_counts.get(pc.event, 0) + 1

        for event, members in registry_events.items():
            if not members:
                # An empty key has nothing to wire — no dispatcher line required.
                continue
            count = dispatcher_counts.get(event, 0)
            if count == 0:
                wiring_mismatches.append(
                    f"{event}: no live hook-dispatcher.py line — {len(members)} member(s) "
                    f"unwired: {', '.join(members)}"
                )
            elif count > 1:
                wiring_mismatches.append(
                    f"{event}: duplicate dispatcher lines ({count}) — exactly one expected"
                )

        # A correctly-argued dispatcher line under an event that is NOT an EVENT_REGISTRY
        # key at all is a stale line (harmless, but named so it can be removed).
        for event in dispatcher_counts:
            if event not in registry_events:
                wiring_mismatches.append(
                    f"{event}: dispatcher line for event with no EVENT_REGISTRY members "
                    f"(stale line)"
                )
        report.dispatcher_wiring_mismatches = sorted(wiring_mismatches)

        # R4: an EVENT_REGISTRY key that is not a legal Claude Code event (VALID_EVENTS).
        # hook-dispatcher.py dispatches by key against SUPPORTED_EVENTS, so a typo'd key
        # can NEVER fire its members — invisible before this check (exit 0 today).
        invalid_keys: list[str] = []
        for event, members in registry_events.items():
            if event not in VALID_EVENTS:
                invalid_keys.append(
                    f"{event}: not a Claude Code hook event — {len(members)} member(s) can "
                    f"never fire: {', '.join(members) if members else '(none)'}"
                )
        report.invalid_event_keys = sorted(invalid_keys)

    # R4 (advisory, rp1-home #1990): a hook FILE whose `# HOOK_EVENT:` header names a
    # non-legal event. WARNING only — does NOT feed has_mismatches()/exit code (post-#1734
    # nothing on the live path reads that header; the FIRING surface is the EVENT_REGISTRY
    # key set checked above). Uses the same strict header regex the generator's
    # parse_hook_metadata uses, so a HOOK_EVENT substring in prose does not trip it.
    header_warnings: list[str] = []
    for name in hooks_files:
        if not (name.endswith(".py") or name.endswith(".sh")):
            continue
        try:
            content = (hooks_dir / name).read_text()
        except OSError:
            continue
        m = re.search(r"^# HOOK_EVENT:[ \t]*(.+)$", content, re.MULTILINE)
        if m:
            ev = m.group(1).strip()
            if ev not in VALID_EVENTS:
                header_warnings.append(f"{name}: # HOOK_EVENT: {ev} is not a legal event")
    report.invalid_event_header_warnings = sorted(header_warnings)

    # ─────────────────────────────────────────────────────────────
    # Single-surface rule (rp1-home #1735 / R1): the global settings surface is
    # dispatcher-only
    # ─────────────────────────────────────────────────────────────
    # The mechanically-enforced invariant that makes lib/hook_registry.py THE single
    # registration surface: the GLOBAL settings surface — the MAIN settings.json AND
    # settings.local.json (R7/D3) — may register ONLY the per-event dispatcher entries
    # (DISPATCHER_ALLOWED_COMMANDS) plus a small documented exception list
    # (SINGLE_SURFACE_EXCEPTIONS). Every other hook must be a hook_registry.py entry.
    # A direct unified-prompt-dispatcher.py entry is a violation under #1734's nested
    # shape (the #953 double-fire hole the dual-registration check cannot catch).
    #
    # Scope is MAIN settings + settings.local.json ONLY — deliberately NOT the worktree
    # union (mirrors the #953 dual-registration MAIN-only scoping), so in-flight worktree
    # branches carrying pre-consolidation direct registrations do not flood live
    # worker/closer invocations with false positives.
    single_surface_commands = list(parsed_commands)  # main settings.json (already parsed)
    settings_local_path = home / ".claude" / "settings.local.json"
    if settings_local_path.exists():
        single_surface_commands += walk_settings_json(settings_local_path, home=home)
    ss_violations: set[str] = set()
    for pc in single_surface_commands:
        if pc.resolved_path is None:
            # Not a resolvable script path (e.g. an inline shell command) — cannot
            # identify it as a standalone hook registration; skip. Registry/forward
            # drift is a separate concern handled by the other checks.
            continue
        basename = pc.resolved_path.name
        if basename in DISPATCHER_ALLOWED_COMMANDS or basename in SINGLE_SURFACE_EXCEPTIONS:
            continue
        ss_violations.add(f"{pc.event}: {pc.command}")
    report.single_surface_violations = sorted(ss_violations)

    # ─────────────────────────────────────────────────────────────
    # hook_registry.py FORWARD drift (rp1-home #1735 / R2 / R29)
    # ─────────────────────────────────────────────────────────────
    # An EVENT_REGISTRY entry whose module file is missing on disk is the #600-class
    # symptom for the registry surface — previously UNOWNED (the pin's forward check
    # validated only settings.json / worktree commands; a stub hook_registry.py entry
    # pointing at a nonexistent module was silently ignored). Resolution mirrors
    # hook_registry.get_hook_path: a module resolves if <name>.py OR <name>.sh exists
    # (check-approval-sentinel is the one .sh, kind="subprocess"). hook_registry_names
    # is already extracted via AST above (no import), so the --home <staged-tree>
    # pre-commit path validates what is about to commit.
    registry_forward_missing: set[str] = set()
    for module_name in hook_registry_names:
        py_path = hooks_dir / f"{module_name}.py"
        sh_path = hooks_dir / f"{module_name}.sh"
        if not py_path.exists() and not sh_path.exists():
            registry_forward_missing.add(module_name)
    report.hook_registry_forward_mismatches = sorted(registry_forward_missing)

    # ─────────────────────────────────────────────────────────────
    # Forward direction (R1, R5, R6): registration → missing file
    # ─────────────────────────────────────────────────────────────
    for pc in parsed_commands:
        if pc.resolved_path is None:
            # Couldn't parse a path; that's a separate concern (could be
            # legitimate non-script commands). Skip silently.
            continue
        # R6: vanilla os.path.exists against the absolute path. No worktree-
        # relative path translation.
        if not pc.resolved_path.exists():
            report.forward_mismatches.append(
                ForwardMismatch(
                    event=pc.event,
                    matcher=pc.matcher,
                    command=pc.command,
                    resolved_path=str(pc.resolved_path),
                    setting_json_index=pc.setting_json_index,
                    source_settings=str(settings_path),
                )
            )

    # ─────────────────────────────────────────────────────────────
    # Global settings.local.json FORWARD scan (rp1-home #1943, R1/R2)
    # ─────────────────────────────────────────────────────────────
    # A UserPromptSubmit hook registered in ~/.claude/settings.local.json whose
    # command path is absent hard-blocks every prompt exactly as a settings.json
    # one does — but settings.local.json was previously parsed ONLY into the
    # single-surface check (settings_local_path above), never the forward
    # path-existence audit. FORWARD-only: these rows are NOT unioned into
    # registered_basenames, so reverse/single-surface/dual behavior is unchanged.
    # (settings_local_path is already defined in the single-surface block above.)
    if settings_local_path.exists():
        for pc in walk_settings_json(settings_local_path, home=home):
            if pc.resolved_path is None:
                continue
            if not pc.resolved_path.exists():
                report.forward_mismatches.append(
                    ForwardMismatch(
                        event=pc.event,
                        matcher=pc.matcher,
                        command=pc.command,
                        resolved_path=str(pc.resolved_path),
                        setting_json_index=pc.setting_json_index,
                        source_settings=str(settings_local_path),
                    )
                )

    # ─────────────────────────────────────────────────────────────
    # Worktree-local settings.json discovery (rp1-home #786)
    # ─────────────────────────────────────────────────────────────
    # Each worktree under home/.worktrees/ may have its own .claude/settings.json
    # with hook commands. These commands typically reference absolute paths under
    # ~/.claude/hooks/ (the main checkout). When a worker session adds
    # a hook entry in the worktree's settings.json before the corresponding file
    # is promoted to main, the absolute-path command silently dangles — and
    # Claude Code's settings reloader (utils/hooks.ts:4203) reloads the worktree's
    # settings mid-session, fires the broken command, python3 exits 2 on the
    # missing path, and Claude treats exit 2 as a deny — bricking the session.
    # This was the rp1-home #743 trigger case.
    #
    # We extend the forward + reverse checks to include worktree settings.json
    # commands. Anchored to the passed `home` parameter so pre-commit's
    # `--home $TMPDIR` invocation correctly finds no worktrees (the staged tree
    # has no .worktrees/ subdir) and behaves identically to pre-#786.
    worktree_commands = walk_worktree_settings(home)

    # Forward direction (R1, R6): extend the forward-drift check to worktree commands.
    # wt_settings names the exact source file (settings.json OR settings.local.json,
    # rp1-home #1943), used verbatim as source_settings so the operator sees which
    # per-worktree file leaked.
    for wt_path, wt_settings, pc in worktree_commands:
        if pc.resolved_path is None:
            continue
        if not pc.resolved_path.exists():
            report.forward_mismatches.append(
                ForwardMismatch(
                    event=pc.event,
                    matcher=pc.matcher,
                    command=pc.command,
                    resolved_path=str(pc.resolved_path),
                    setting_json_index=pc.setting_json_index,
                    source_settings=str(wt_settings),
                )
            )

    # Union worktree-registered basenames into registered_basenames (R3) so the
    # reverse-settings layer treats a worktree-only registration as "registered"
    # for the reverse-direction audit. Without this union, every in-flight
    # worktree-added hook would be flagged as orphaned by the R2 reverse check.
    # ONLY worktree settings.json commands are unioned — settings.local.json rows
    # are FORWARD-audit only (rp1-home #1943 V6), so the reverse/single-surface/dual
    # behavior stays byte-identical to pre-#1943.
    for wt_path, wt_settings, pc in worktree_commands:
        if pc.resolved_path is not None and wt_settings.name == "settings.json":
            registered_basenames.add(pc.resolved_path.name)

    # ─────────────────────────────────────────────────────────────
    # Project-level settings FORWARD scan (rp1-home #1943, R1/R2)
    # ─────────────────────────────────────────────────────────────
    # When invoked against a project checkout that is NOT `home` — the exact SL7
    # leak location `~/projects/rp1-home/.claude/settings.local.json`, explicitly
    # OUT of the global-surface scope noted above — forward-scan its
    # .claude/settings.json + .claude/settings.local.json for host-absent hook paths.
    # FORWARD AUDIT ONLY: project rows are NOT unioned into registered_basenames and
    # are NOT fed into the single-surface / dual-registration / reverse checks (those
    # stay ~-scoped per #1735/#953). project_dir is scanned ONLY when passed explicitly;
    # resolution + fallback live in main() and never run when --home is given (R5/V3
    # hermeticity — no project scan, no git call under pre-commit's --home $TMPDIR).
    if project_dir is not None:
        proj = Path(project_dir).resolve()
        if proj != home:
            proj_settings_files: list[Path] = []
            # Under home/.worktrees/<wt>/ the checkout's settings.json is ALREADY
            # covered by walk_worktree_settings — scan ONLY its settings.local.json
            # there, to avoid a duplicate row for settings.json.
            worktrees_root = home / ".worktrees"
            if worktrees_root not in proj.parents:
                proj_settings_files.append(proj / ".claude" / "settings.json")
            proj_settings_files.append(proj / ".claude" / "settings.local.json")

            # Dedup appended rows by (source_settings, event, command).
            seen_forward = {
                (m.source_settings, m.event, m.command)
                for m in report.forward_mismatches
            }
            for psf in proj_settings_files:
                if not psf.exists():
                    continue
                for pc in walk_settings_json(psf, home=home):
                    if pc.resolved_path is None:
                        continue
                    if not pc.resolved_path.exists():
                        key = (str(psf), pc.event, pc.command)
                        if key in seen_forward:
                            continue
                        seen_forward.add(key)
                        report.forward_mismatches.append(
                            ForwardMismatch(
                                event=pc.event,
                                matcher=pc.matcher,
                                command=pc.command,
                                resolved_path=str(pc.resolved_path),
                                setting_json_index=pc.setting_json_index,
                                source_settings=str(psf),
                            )
                        )

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — settings layer (R2; scope WIDENED by rp1-home #1735/R30)
    # ─────────────────────────────────────────────────────────────
    # Scope:
    #   - *-detector.py files (the original #618 class), UNION every
    #     intent-declaring hook file — a `HOOK_EVENT` annotation OR a priority-API
    #     import (the same set that used to be --strict-only, #638). Post-#1734
    #     lib/hook_registry.py is THE single registration surface, so a non-detector
    #     hook file absent from ALL registration layers is genuine drift that MUST be
    #     flagged by DEFAULT — no longer only under --strict (R30). walk_all_intent_files
    #     returns only *.py files and excludes KNOWN_NON_HOOK_FILES, so the widening
    #     inherits those exclusions for free.
    #   - A file is "registered" if it's in Layer 1 (settings.json, ALL events, incl.
    #     the worktree union) OR Layer 2 (detector_registry PRIORITY_GROUP/
    #     INDEPENDENT_GROUP) OR Layer 3 (hook_registry.py EVENT_REGISTRY — the #1730
    #     dispatcher registry; after the #1732 cutover a dispatched hook lives ONLY
    #     here with no direct settings.json entry, so this membership is what keeps
    #     it registered).
    detector_named_files: set[str] = {n for n in hooks_files if n.endswith("-detector.py")}
    reverse_scan_files: set[str] = detector_named_files | walk_all_intent_files(hooks_dir)
    files_missing_settings: set[str] = set()
    for name in reverse_scan_files:
        name_no_ext = name[:-3]  # drop ".py"
        registered_layer1 = name in registered_basenames
        registered_layer2 = name_no_ext in registry_names
        registered_layer3 = name_no_ext in hook_registry_names
        if not registered_layer1 and not registered_layer2 and not registered_layer3:
            files_missing_settings.add(name)
    report.reverse_mismatches_settings = sorted(files_missing_settings)

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — registry layer (R3)
    # ─────────────────────────────────────────────────────────────
    # Applies to detector files that import the priority API. These files
    # call check_and_claim_priority() but rely on detector_registry membership
    # to be discoverable by the unified-prompt-dispatcher.
    # CASCADE per Key Decision 4: if a file failed R2 (no registration at
    # all), skip it for R3 — R2's mismatch already implies the registry gap.
    detectors_missing_registry: set[str] = set()
    for name in detector_files:
        if name in files_missing_settings:
            # Already reported under R2; don't double-report
            continue
        detector_name_no_ext = name[:-3]  # drop ".py"
        if detector_name_no_ext not in registry_names:
            detectors_missing_registry.add(detector_name_no_ext)
    report.reverse_mismatches_registry = sorted(detectors_missing_registry)

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — priorities layer (R4)
    # ─────────────────────────────────────────────────────────────
    # For each entry in PRIORITY_GROUP, check if its name is in HOOK_PRIORITIES.
    priorities_missing: set[str] = set()
    for name in priority_names:
        if name not in hook_priority_keys:
            priorities_missing.add(name)
    report.reverse_mismatches_priorities = sorted(priorities_missing)

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — test layer (R4 second arm)
    # ─────────────────────────────────────────────────────────────
    # For each key in HOOK_PRIORITIES, check if it appears in the test's
    # expected_hooks set literal.
    test_missing: set[str] = set()
    for name in hook_priority_keys:
        if name not in test_expected:
            test_missing.add(name)
    report.reverse_mismatches_test = sorted(test_missing)

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — contract layer (rp1-home #862)
    # ─────────────────────────────────────────────────────────────
    # The unified dispatcher does getattr(module, "run_detection", None)
    # (unified-prompt-dispatcher.py). A PRIORITY_GROUP/INDEPENDENT_GROUP member
    # whose module defines no run_detection is registered but silently
    # non-functional — the rp1-home #852 class (github-project-detector had only
    # main()). The presence-only checks (#624) cannot catch this.
    # Path is home-anchored (hooks_dir/<name>.py), NOT the live get_detector_path()
    # (which resolves against detector_registry.py's __file__), so the pre-commit
    # --home $TMPDIR invocation validates the STAGED tree. Static AST parse only —
    # no detector module is imported or executed.
    # ─────────────────────────────────────────────────────────────
    # detector_registry FORWARD drift (rp1-home #2006 / KD4 / D7)
    # ─────────────────────────────────────────────────────────────
    # A PRIORITY_GROUP/INDEPENDENT_GROUP member whose on-disk file is ABSENT — the
    # unified dispatcher can never fire it (the private product repo #3590 Configuration
    # Porter-drop class). This is EXCLUSIVE of the contract layer below: a missing
    # file lands ONLY here, so reverse_mismatches_contract keeps its true meaning
    # ("registered without run_detection") for files that actually EXIST — the
    # misread that produced the private product repo #3567. Pure Path.exists(), no import/AST
    # (same no-import philosophy as the rest of the validator); home-anchored so the
    # pre-commit --home $TMPDIR invocation validates the staged tree.
    detector_registry_forward_missing: set[str] = set()
    for name in registry_names:
        if not (hooks_dir / f"{name}.py").exists():
            detector_registry_forward_missing.add(name)
    report.detector_registry_forward_mismatches = sorted(detector_registry_forward_missing)

    contract_missing: set[str] = set()
    for name in registry_names:
        # A missing file belongs to detector_registry_forward_mismatches (above),
        # NOT the contract class — skip it here (KD4/D7). _defines_run_detection would
        # otherwise return False for a missing file and misreport it as "no
        # run_detection" (the private product repo #3567 misread).
        if name in detector_registry_forward_missing:
            continue
        detector_path = hooks_dir / f"{name}.py"
        if not _defines_run_detection(detector_path):
            contract_missing.add(name)
    report.reverse_mismatches_contract = sorted(contract_missing)

    # ─────────────────────────────────────────────────────────────
    # Reverse direction — strict layer (--strict, rp1-home #638)
    # ─────────────────────────────────────────────────────────────
    # OPT-IN: only runs when strict=True. Broadens the reverse-settings audit
    # beyond *-detector.py to EVERY top-level hook file declaring intent (a
    # HOOK_EVENT annotation or a priority-API import) that is registered in
    # neither settings.json (any event, incl. the worktree union above) NOR
    # detector_registry. Catches the non-detector dead-code class
    # (session-edit-logger.py, lora-import-detector.py before #638) the narrow
    # R2 scope misses. CASCADE: *-detector.py files already flagged by R2 are
    # skipped here to avoid double-reporting — the strict list surfaces only the
    # net-new non-detector intent files. When strict=False the list stays empty,
    # so has_mismatches()/exit-code is byte-identical to the default path.
    if strict:
        strict_missing: set[str] = set()
        for name in walk_all_intent_files(hooks_dir):
            if name in files_missing_settings:
                # Already reported under R2 (unregistered *-detector.py)
                continue
            name_no_ext = name[:-3]  # drop ".py"
            # R5 (rp1-home #1732): mirror Layer 3 — a hook_registry.py (EVENT_REGISTRY)
            # member is dispatched by hook-dispatcher.py, so it counts as registered for
            # the strict audit too. Without this clause the strict layer false-flags the
            # exact migrants Layer 3 just cleared: once Layer 3 removes them from
            # files_missing_settings, the cascade-skip above no longer fires for them, so
            # this membership check is what must recognize their EVENT_REGISTRY registration.
            if (
                name not in registered_basenames
                and name_no_ext not in registry_names
                and name_no_ext not in hook_registry_names
            ):
                strict_missing.add(name)
        report.reverse_mismatches_strict_settings = sorted(strict_missing)

    # ─────────────────────────────────────────────────────────────
    # Plugin fragments (rp1-home #2213) — the second legitimate registration surface
    # ─────────────────────────────────────────────────────────────
    # REPO-TREE mode for every layer (the fragment under validation lives under `home`);
    # installed-set parity only under --plugins-installed (closer-only). Gated on a
    # fragment-bearing plugin, so a zero-fragment tree leaves report.plugins empty and the
    # output byte-identical (R8). The shadow rule keys on the SAME core sets the loader uses:
    # same-event EVENT_REGISTRY module basenames, and every detector_registry name.
    if plugins_installed or discover_repo_plugins(home):
        core_event_basenames = {
            event: {_strip_script_ext(m) for m in modules} for event, modules in registry_events.items()
        }
        report.plugins = validate_plugin_fragments(
            home, core_event_basenames, set(registry_names), known_events=set(registry_events)
        )
        if plugins_installed:
            apply_installed_parity(report.plugins)

    return report


# ═══════════════════════════════════════════════════════════════════════
# Logging helper
# ═══════════════════════════════════════════════════════════════════════


def _log_err(msg: str) -> None:
    """Log to stderr without raising. Honors fail-open philosophy."""
    try:
        sys.stderr.write(f"[hook_registration_validator] {msg}\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# CLI entry point (Task 2 / R7, R13)
# ═══════════════════════════════════════════════════════════════════════


REMEDIATION_FOOTERS = {
    "forward": (
        "Forward-direction drift (file registered but missing on disk):\n"
        "  - Restore the missing file at the registered absolute path, OR\n"
        "  - Remove the orphaned entry from settings.json.\n"
        "  See file:line references above for the specific settings.json indices."
    ),
    "reverse_settings": (
        "Reverse-direction drift — intent file registered on NO surface\n"
        "(not in settings.json, detector_registry.py, or EVENT_REGISTRY):\n"
        "  A hook/detector file exists but is registered on none of the three surfaces,\n"
        "  so the dispatcher never fires it. Do NOT add a standalone ~/.claude/settings.json\n"
        "  hooks block — that IS the single-surface violation this validator rejects (exit 1,\n"
        "  rp1-home #1735). Instead:\n"
        "  Register in .claude/hooks/lib/hook_registry.py (EVENT_REGISTRY) — non-detectors; lib/detector_registry.py — UserPromptSubmit detectors.\n"
        "  - Non-detector hooks: add an Entry('<matcher>', '<module>') to the appropriate\n"
        "    event list in the EVENT_REGISTRY dict (name the entry, never a line number);\n"
        "    then re-derive the hard-coded counts in\n"
        "    tests/test_hook_dispatcher.py::TestRegistry (test_pre_post_counts; and\n"
        "    test_bash_selects_exactly_38_pre_entries when the entry is Entry('Bash', …)\n"
        "    on PreToolUse).\n"
        "  - UserPromptSubmit detectors: add to PRIORITY_GROUP/INDEPENDENT_GROUP in\n"
        "    lib/detector_registry.py ({\"name\": \"<detector-name>\", \"priority\": <N>}); then add\n"
        "    the name to HOOK_PRIORITIES (lib/hook_priorities.py) and to expected_hooks in\n"
        "    tests/test_hook_priorities.py::TestPriorityAssignments::test_all_hooks_registered.\n"
        "    A detector add changes no test_hook_dispatcher.py count."
    ),
    "reverse_registry": (
        "Reverse-direction drift — detector_registry layer (detector imports priority API but not registered):\n"
        "  - Add an entry to PRIORITY_GROUP (or INDEPENDENT_GROUP) in:\n"
        "    ~/.claude/hooks/lib/detector_registry.py\n"
        "    Format: {\"name\": \"<detector-name>\", \"priority\": <N>}"
    ),
    "reverse_priorities": (
        "Reverse-direction drift — hook_priorities layer (in registry PRIORITY_GROUP but missing from priorities dict):\n"
        "  - Add the entry to HOOK_PRIORITIES dict in:\n"
        "    ~/.claude/hooks/lib/hook_priorities.py\n"
        "    Format: \"<detector-name>\": <priority>,"
    ),
    "reverse_test": (
        "Reverse-direction drift — test_hook_priorities layer (in HOOK_PRIORITIES but missing from test expected_hooks):\n"
        "  - Add the entry to expected_hooks set in:\n"
        "    ~/.claude/hooks/tests/test_hook_priorities.py::TestPriorityAssignments::test_all_hooks_registered"
    ),
    "reverse_contract": (
        "Reverse-direction drift — contract layer (registry member defines no run_detection):\n"
        "  - Every PRIORITY_GROUP/INDEPENDENT_GROUP detector must define a module-level\n"
        "    run_detection(prompt, input_data) function — the unified dispatcher invokes it\n"
        "    via getattr(module, 'run_detection'). A member without it is registered but never fires.\n"
        "  - Add `def run_detection(prompt, input_data):` to ~/.claude/hooks/<name>.py\n"
        "    (or restore the file if it is missing)."
    ),
    "reverse_strict": (
        "Reverse-direction drift — strict layer (--strict, rp1-home #638):\n"
        "  A hook file declares intent (HOOK_EVENT annotation or priority-API import)\n"
        "  but is registered on no surface (settings.json, detector_registry.py, or\n"
        "  EVENT_REGISTRY).\n"
        "  - If it should fire: register it on the single registration surface — add an\n"
        "    Entry('<matcher>', '<module>') to EVENT_REGISTRY in\n"
        "    .claude/hooks/lib/hook_registry.py (non-detectors), or add it to\n"
        "    PRIORITY_GROUP/INDEPENDENT_GROUP in lib/detector_registry.py (UserPromptSubmit\n"
        "    detectors); add a `# HOOK_EVENT:` annotation if missing. OR\n"
        "  - If it is intentionally not a hook: add it to KNOWN_NON_HOOK_FILES in\n"
        "    ~/.claude/hooks/lib/hook_registration_validator.py, OR\n"
        "  - If it is dead code: delete the file.\n"
        "  NOTE: --strict is advisory — a HOOK_EVENT mention in a comment is enough\n"
        "  to flag a file. Verify before acting."
    ),
    "dual": (
        "Dual-registration drift (rp1-home #953) — detector registered both directly in\n"
        "settings.json AND owned by the dispatcher via detector_registry.py:\n"
        "  - These detectors fire TWICE per UserPromptSubmit (once directly, once via the\n"
        "    dispatcher). Remove the DIRECT registration from ~/.claude/settings.json —\n"
        "    the dispatcher already runs every detector_registry.py member.\n"
        "  - Post-#1734, settings.json registers ONLY the per-event hook-dispatcher.py\n"
        "    entries; every other hook (incl. the former standalones pm-inbox-checker.py /\n"
        "    skill-creator-augment-detector.py) is a nested lib/hook_registry.py entry."
    ),
    "single_surface": (
        "Single-surface drift (rp1-home #1735) — a hook is registered directly in the global\n"
        "settings surface (~/.claude/settings.json or ~/.claude/settings.local.json) instead of\n"
        "in lib/hook_registry.py, the mechanically-enforced single registration surface:\n"
        "  - Move the hook to lib/hook_registry.py: add an `Entry('<matcher>', '<module>')` to\n"
        "    the appropriate event list in EVENT_REGISTRY, and REMOVE the direct settings.json\n"
        "    registration. settings.json must carry ONLY the per-event `hook-dispatcher.py <event>`\n"
        "    entries (DISPATCHER_ALLOWED_COMMANDS). A direct unified-prompt-dispatcher.py entry is\n"
        "    a violation under the #1734 nested shape — nest it in hook_registry.py instead.\n"
        "  - If the registration is a legitimate per-machine exception (e.g. a settings.local.json\n"
        "    helper), add its basename to SINGLE_SURFACE_EXCEPTIONS in\n"
        "    ~/.claude/hooks/lib/hook_registration_validator.py (per-machine extensible)."
    ),
    "hook_registry_forward": (
        "Registry forward drift (rp1-home #1735) — an EVENT_REGISTRY entry in\n"
        "lib/hook_registry.py names a module whose file is missing on disk (#600-class):\n"
        "  - Restore the missing hook file at ~/.claude/hooks/<module>.py (or <module>.sh), OR\n"
        "  - Remove the orphaned Entry(...) from EVENT_REGISTRY in\n"
        "    ~/.claude/hooks/lib/hook_registry.py."
    ),
    "detector_registry_forward": (
        "Detector registry forward drift (rp1-home #2006) — a detector_registry.py\n"
        "PRIORITY_GROUP/INDEPENDENT_GROUP member has NO file on disk, so the unified\n"
        "prompt dispatcher can never fire it (workflow phrases silently do nothing —\n"
        "the private product repo #3590 Configuration Porter-drop class):\n"
        "  - Restore the missing detector file at ~/.claude/hooks/<name>.py (most likely\n"
        "    a partial config sync / Configuration Porter drop), OR\n"
        "  - Remove the entry from PRIORITY_GROUP/INDEPENDENT_GROUP in\n"
        "    ~/.claude/hooks/lib/detector_registry.py if the detector is retired.\n"
        "  NOTE: this is distinct from the contract layer above — a member whose file\n"
        "  is ABSENT is reported here, NOT as 'registered without run_detection'."
    ),
    "dispatcher_wiring": (
        "Dispatcher wiring drift (rp1-home #1990) — an EVENT_REGISTRY event is not served\n"
        "by exactly one live hook-dispatcher.py line, so its registered hooks never fire:\n"
        "  - Restore the per-event dispatcher line under hooks.<event> in\n"
        "    ~/.claude/settings.json — the ONE sanctioned direct registration (#1732/#1734):\n"
        "      {\"type\": \"command\", \"command\": \"python3 $HOME/.claude/hooks/hook-dispatcher.py <event>\"}\n"
        "    The trailing <event> argument MUST equal the hooks.<event> it is registered under.\n"
        "  - A DUPLICATE dispatcher line for an event: remove the extra so exactly one remains.\n"
        "  - A STALE line (an event with no EVENT_REGISTRY members): remove the line, or add\n"
        "    the event's members to EVENT_REGISTRY in lib/hook_registry.py if it should fire."
    ),
    "invalid_event_keys": (
        "Invalid EVENT_REGISTRY event key (rp1-home #1990) — a key in lib/hook_registry.py's\n"
        "EVENT_REGISTRY is not a legal Claude Code hook event, so hook-dispatcher.py can never\n"
        "dispatch its members:\n"
        "  - Legal events: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,\n"
        "    PostToolUseFailure, Stop.\n"
        "  - Fix the typo'd key, or move its Entry(...) members under the correct event list."
    ),
    # Plugin-fragment footers (rp1-home #2213) — appended keys only; no existing key renamed.
    "plugin_forward": (
        "Plugin fragment forward drift (rp1-home #2213) — plugins/<name>/hooks/registry-fragment.py\n"
        "names a module, detector or NATIVE_SESSIONSTART_CHECK script with no file under that plugin's\n"
        "root, keys an entry under a non-dispatcher event, or cannot be read statically:\n"
        "  - Restore the missing script under plugins/<name>/ (an entry module resolves as <path>,\n"
        "    <path>.py or <path>.sh; a detector path must exist exactly), OR remove the declaration.\n"
        "  - A fragment is pure data (.claude/docs/plugin-registry-fragment-contract.md §1): keep\n"
        "    EVENT_ENTRIES, DETECTORS and NATIVE_SESSIONSTART_CHECK as literals — the validator reads\n"
        "    them with `ast` and never imports the fragment."
    ),
    "plugin_reverse": (
        "Plugin fragment reverse drift (rp1-home #2213) — a hook-shaped module under\n"
        "plugins/<name>/hooks/ (a *-detector.py, a HOOK_EVENT annotation, or a priority-API import)\n"
        "is named by neither the fragment's EVENT_ENTRIES nor its DETECTORS, so nothing runs it:\n"
        "  - Register it in plugins/<name>/hooks/registry-fragment.py — an Entry('<matcher>',\n"
        "    'hooks/<module>.py') in EVENT_ENTRIES, or an INDEPENDENT {\"name\": ..., \"path\": ...}\n"
        "    in DETECTORS, OR\n"
        "  - Delete it if it is dead code."
    ),
    "plugin_native_hook": (
        "Plugin native-hook drift (rp1-home #2213) — a plugin registers a hook OUTSIDE the dispatcher\n"
        "(native plugin hooks run in parallel, unordered, with no telemetry batch):\n"
        "  - register it in `<plugin-root>/hooks/registry-fragment.py` `EVENT_ENTRIES`; `hooks/hooks.json`\n"
        "    may carry only the `NATIVE_SESSIONSTART_CHECK` SessionStart command, in the WRAPPED shape\n"
        "    {\"hooks\": {\"SessionStart\": [{\"hooks\": [{\"type\": \"command\",\n"
        "     \"command\": \"\\\"${CLAUDE_PLUGIN_ROOT}\\\"/<NATIVE_SESSIONSTART_CHECK>\"}]}]}}.\n"
        "  - The same rule covers every native surface Claude Code 2.1.280 honours: a manifest\n"
        "    plugin.json `hooks` (path, inline object or array), an inline marketplace-entry `hooks`,\n"
        "    hooks.json `modules`, a plugin settings.json `hooks`, and skill / command / agent\n"
        "    frontmatter `hooks:` (.claude/docs/plugin-cli-surface-findings.md §6)."
    ),
    "plugin_detector": (
        "Plugin detector declares a position (rp1-home #2213, Position 2) — a fragment contributes\n"
        "INDEPENDENT detectors only; the loader rejects a DETECTORS entry carrying `priority`/`before`:\n"
        "  - Remove the `priority` / `before` key from that DETECTORS entry in\n"
        "    plugins/<name>/hooks/registry-fragment.py."
    ),
    "plugin_marketplace": (
        "Plugin not listed in the marketplace (rp1-home #2213) — nothing can install a\n"
        "fragment-bearing plugins/<name>/ that .claude-plugin/marketplace.json does not list:\n"
        "  - Add {\"name\": \"<name>\", \"source\": \"./plugins/<name>\"} to the `plugins` array of\n"
        "    .claude-plugin/marketplace.json (plugin-registry-fragment-contract.md §11)."
    ),
}

# Render order for the plugin footers (stable output).
_PLUGIN_FOOTER_ORDER = (
    "plugin_forward",
    "plugin_reverse",
    "plugin_native_hook",
    "plugin_detector",
    "plugin_marketplace",
)


def _render_plugins(report: ValidationReport) -> list[str]:
    """The human `plugins` block (rp1-home #2213, R6). Empty list when no plugin was validated,
    so a zero-fragment report renders exactly as v1.9.0 (R8)."""
    if not report.plugins:
        return []
    lines = [
        f"Plugin fragments (rp1-home #2213) — {len(report.plugins)} plugin(s), "
        f"{report.plugin_violation_count()} violation(s):"
    ]
    footers: set[str] = set()
    for p in report.plugins:
        lines.append(f"  - {p['name']}  [{p['root'] or 'installed only — no repo-tree fragment'}]")
        lines.append(
            f"      fragment: {p['entry_count']} entr{'y' if p['entry_count'] == 1 else 'ies'}, "
            f"{p['detector_count']} detector(s), native check: {p['native_check'] or 'none'}"
        )
        for v in p.get("violations") or []:
            where = f" ({v['path']})" if v.get("path") else ""
            lines.append(f"      VIOLATION {v['kind']}{where}: {v['detail']}")
            footers.add(PLUGIN_FOOTER_FOR_KIND.get(v["kind"], "plugin_native_hook"))
        for a in p.get("advisories") or []:
            where = f" ({a['path']})" if a.get("path") else ""
            lines.append(f"      advisory {a['kind']}{where}: {a['detail']}")
        for s in p.get("shadowed") or []:
            label = f"{s['event']}:{s['name']}" if s.get("event") else f"detector:{s['name']}"
            lines.append(f"      shadowed {label} (informational — the core copy wins)")
        installed = p.get("installed")
        if installed:
            differs = {True: "yes", False: "no", None: "n/a (no repo-tree fragment)"}[installed["differs"]]
            lines.append(
                f"      installed {installed['key']}: root={installed['root']} "
                f"kind={installed['root_kind']} differs={differs} (informational)"
            )
    lines.append("")
    for key in _PLUGIN_FOOTER_ORDER:
        if key in footers:
            lines.append(REMEDIATION_FOOTERS[key])
            lines.append("")
    return lines


def _render_human(report: ValidationReport) -> str:
    """Render a ValidationReport as human-readable prose for CLI output."""
    lines: list[str] = []
    total = sum(
        [
            len(report.forward_mismatches),
            len(report.reverse_mismatches_settings),
            len(report.reverse_mismatches_registry),
            len(report.reverse_mismatches_priorities),
            len(report.reverse_mismatches_test),
            len(report.reverse_mismatches_contract),
            len(report.reverse_mismatches_strict_settings),
            len(report.dual_registrations),
            len(report.dispatcher_wiring_mismatches),
            len(report.invalid_event_keys),
            len(report.single_surface_violations),
            len(report.hook_registry_forward_mismatches),
            len(report.detector_registry_forward_mismatches),
        ]
    ) + report.plugin_violation_count()
    # rp1-home #2213: empty for a zero-fragment report, so both branches below render
    # exactly as v1.9.0 when no plugin was validated (R8).
    plugin_lines = _render_plugins(report)
    # invalid_event_header_warnings is advisory — it is intentionally EXCLUDED from
    # `total`/has_mismatches so it never flips the exit code, but it is still surfaced.

    def _advisory_block() -> str:
        if not report.invalid_event_header_warnings:
            return ""
        out = "\nAdvisory — HOOK_EVENT header(s) name a non-legal event (not a failure):\n"
        for w in report.invalid_event_header_warnings:
            out += f"  - {w}\n"
        return out

    if total == 0:
        plugin_text = ("\n" + "\n".join(plugin_lines)) if plugin_lines else ""
        return "Hook registration validator: OK — no mismatches.\n" + plugin_text + _advisory_block()

    lines.append("Hook registration validator: MISMATCHES DETECTED")
    lines.append("=" * 60)
    lines.append(f"Forward (registered file missing):   {len(report.forward_mismatches)}")
    lines.append(f"Reverse — settings:                  {len(report.reverse_mismatches_settings)}")
    lines.append(f"Reverse — detector_registry:         {len(report.reverse_mismatches_registry)}")
    lines.append(f"Reverse — hook_priorities:           {len(report.reverse_mismatches_priorities)}")
    lines.append(f"Reverse — test_hook_priorities:      {len(report.reverse_mismatches_test)}")
    lines.append(f"Reverse — contract (run_detection):  {len(report.reverse_mismatches_contract)}")
    lines.append(f"Reverse — strict (intent, unregistered): {len(report.reverse_mismatches_strict_settings)}")
    lines.append(f"Dual registration (settings+dispatcher): {len(report.dual_registrations)}")
    lines.append(f"Dispatcher wiring (registry key not served by a dispatcher line): {len(report.dispatcher_wiring_mismatches)}")
    lines.append(f"Invalid event keys (EVENT_REGISTRY key not a legal event): {len(report.invalid_event_keys)}")
    lines.append(f"Single-surface (non-dispatcher settings reg): {len(report.single_surface_violations)}")
    lines.append(f"Registry forward (EVENT_REGISTRY file missing): {len(report.hook_registry_forward_mismatches)}")
    lines.append(
        f"Detector registry — {len(report.detector_registry_forward_mismatches)} "
        "registry detectors have no file"
    )
    if report.plugins:
        lines.append(f"Plugin fragments (violations):       {report.plugin_violation_count()}")
    lines.append("")

    if report.forward_mismatches:
        lines.append("Forward-direction mismatches:")
        for m in report.forward_mismatches:
            lines.append(
                f"  - event={m.event} block_index={m.setting_json_index}"
                f"  matcher={m.matcher!r}"
            )
            lines.append(f"    command:       {m.command}")
            lines.append(f"    resolved path: {m.resolved_path}   (does NOT exist)")
            if m.source_settings:
                lines.append(f"    source:        {m.source_settings}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["forward"])
        lines.append("")

    if report.reverse_mismatches_settings:
        lines.append("Reverse — these intent files are registered on NO surface (settings.json / detector_registry.py / EVENT_REGISTRY):")
        for name in report.reverse_mismatches_settings:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_settings"])
        lines.append("")

    if report.reverse_mismatches_registry:
        lines.append("Reverse — detector_registry.py missing these detectors:")
        for name in report.reverse_mismatches_registry:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_registry"])
        lines.append("")

    if report.reverse_mismatches_priorities:
        lines.append("Reverse — hook_priorities.py missing these names:")
        for name in report.reverse_mismatches_priorities:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_priorities"])
        lines.append("")

    if report.reverse_mismatches_test:
        lines.append("Reverse — test_hook_priorities.py expected_hooks missing these names:")
        for name in report.reverse_mismatches_test:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_test"])
        lines.append("")

    if report.reverse_mismatches_contract:
        lines.append("Reverse — registry members missing run_detection:")
        for name in report.reverse_mismatches_contract:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_contract"])
        lines.append("")

    if report.reverse_mismatches_strict_settings:
        lines.append("Reverse — strict: intent files declared but registered nowhere:")
        for name in report.reverse_mismatches_strict_settings:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["reverse_strict"])
        lines.append("")

    if report.dual_registrations:
        lines.append("Dual-registered detectors (settings.json + dispatcher):")
        for name in report.dual_registrations:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["dual"])
        lines.append("")

    if report.dispatcher_wiring_mismatches:
        lines.append("Dispatcher wiring — EVENT_REGISTRY keys not served by exactly one dispatcher line:")
        for entry in report.dispatcher_wiring_mismatches:
            lines.append(f"  - {entry}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["dispatcher_wiring"])
        lines.append("")

    if report.invalid_event_keys:
        lines.append("Invalid event keys — EVENT_REGISTRY keys that are not legal Claude Code events:")
        for entry in report.invalid_event_keys:
            lines.append(f"  - {entry}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["invalid_event_keys"])
        lines.append("")

    if report.single_surface_violations:
        lines.append("Single-surface violations (non-dispatcher settings registrations):")
        for entry in report.single_surface_violations:
            lines.append(f"  - {entry}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["single_surface"])
        lines.append("")

    if report.hook_registry_forward_mismatches:
        lines.append("Registry forward drift (EVENT_REGISTRY module file missing):")
        for name in report.hook_registry_forward_mismatches:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["hook_registry_forward"])
        lines.append("")

    if report.detector_registry_forward_mismatches:
        n = len(report.detector_registry_forward_mismatches)
        lines.append(f"Detector registry — members with no file ({n}):")
        for name in report.detector_registry_forward_mismatches:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(REMEDIATION_FOOTERS["detector_registry_forward"])
        lines.append("")

    lines.extend(plugin_lines)

    if report.errors:
        lines.append("Validator errors (non-fatal — see report contents above):")
        for e in report.errors:
            lines.append(f"  - {e}")
        lines.append("")

    lines.append(
        "Bypass for genuine exceptions: git commit --no-verify -m \"<message>\"\n"
        "  Note: bypassed commits are caught by the issue-closer Step 14.5 audit."
    )

    return "\n".join(lines) + "\n" + _advisory_block()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hook_registration_validator",
        description="Bidirectional consistency check for ~/.claude/ hook registrations.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Directory tree root containing .claude/ (default: $HOME)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human prose",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all output; communicate via exit code only",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "DEPRECATED (rp1-home #1990) — now a no-op. The default scan was widened "
            "in v1.5.0 (#1735) to cover the same intent files, so --strict adds no "
            "coverage; passing it only prints a deprecation warning. Retained for "
            "backward compatibility. (orig. rp1-home #638)"
        ),
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help=(
            "Also FORWARD-scan a project-level checkout's .claude/settings.json + "
            ".claude/settings.local.json for host-absent hook paths (the SL7 leak "
            "location, rp1-home #1943). When omitted AND --home is not given, "
            "auto-resolved from $CLAUDE_PROJECT_DIR, else `git rev-parse "
            "--show-toplevel`, else skipped. Ignored entirely when --home is given "
            "(hermetic: no project scan, no git call)."
        ),
    )
    parser.add_argument(
        "--plugins-installed",
        action="store_true",
        help=(
            "Closer-only (rp1-home #2213): also run the INFORMATIONAL installed-set plugin parity "
            "check — lib/plugin_fragments.discover_plugins() resolves each enabled fragment plugin's "
            "runtime root (source/cache, consolidated D1) and the report shows whether its fragment "
            "differs from the repo-tree one. Never affects the exit code."
        ),
    )
    args = parser.parse_args(argv)

    # --strict is a deprecated no-op (rp1-home #1990): warn once at the CLI boundary.
    # The default scan (#1735) already covers every intent file --strict would, so the
    # strict bucket is empty by construction; the flag is kept only for compatibility.
    if args.strict and not args.quiet:
        _log_err(
            "--strict is DEPRECATED and now a no-op (the default scan covers the same "
            "intent files since v1.5.0/#1735); it adds no coverage. The dispatcher-wiring "
            "check (#1990) now owns the registered-but-never-fires guarantee."
        )

    # Project-dir resolution + fallback (rp1-home #1943). Lives HERE only, and runs
    # ONLY when --home is absent: with --home given the validator must perform no
    # project scan and no git call (V3/R5 hermeticity — pre-commit's `--home $TMPDIR`
    # path is unchanged). validate_registrations never resolves a project dir itself.
    project_dir: Optional[Path] = None
    if args.home is None:
        if args.project_dir is not None:
            project_dir = args.project_dir
        else:
            env_pd = os.environ.get("CLAUDE_PROJECT_DIR")
            if env_pd:
                project_dir = Path(env_pd)
            else:
                try:
                    res = subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        project_dir = Path(res.stdout.strip())
                except Exception:  # noqa: BLE001 — fail-open: skip the project scan
                    project_dir = None

    try:
        report = validate_registrations(
            home=args.home,
            strict=args.strict,
            project_dir=project_dir,
            plugins_installed=args.plugins_installed,
        )
    except Exception as e:  # noqa: BLE001 — fail-open at the CLI boundary
        _log_err(f"internal error during validation: {e}")
        if not args.quiet:
            sys.stderr.write(f"hook_registration_validator: internal error: {e}\n")
        return 2

    if not args.quiet:
        if args.json:
            json.dump(report.to_dict(), sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(_render_human(report))

    return 1 if report.has_mismatches() else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
