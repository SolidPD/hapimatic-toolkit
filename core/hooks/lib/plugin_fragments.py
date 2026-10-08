"""
plugin_fragments — discover installed Hapimatic plugin fragments, merge their hook
entries + detectors AFTER the core registries, and expose the shared discovery used
by the dispatcher, the prompt dispatcher, the SessionStart warner, the offline
shadow differ and Sub-Issue B's closer-only parity check.

VERSION: 1.1.0  (rp1-home#2613: per-process discovery + fragment memo; the activation
                 probe is pre-gated on ``hapi --version``, probed once per process and
                 cached ≤300 s across processes, gated only for a plugin with entries,
                 audited once per session to a durable JSONL, and its decision is
                 honored; cross-fragment duplicate detectors are logged against their
                 owner and reported; a ``file`` marketplace reads its own JSON)

Part of the plugin registry-fragment contract + loader (rp1-home#2212, epic #2211
Premium plugin packaging E3, Q9-A). This is the ONE module that knows which plugins
are installed and how to merge their registrations, so no consumer disagrees
(KD-3). It is imported LAZILY by its consumers (Task 2/4/6) so the standalone
registry self-checks (`python3 lib/hook_registry.py`) keep working.

Design invariants:

- **Byte no-op with zero fragment-bearing plugins (R2).** ``merged_event_entries``
  / ``merged_detectors`` return the core lists UNCHANGED when ``discover_plugins()``
  is empty and no shadow removal is active. The whole-suite order-diff test depends
  on this.
- **Core always wins (R5/R6).** A broken fragment is logged loudly and skipped for
  THAT plugin only; discovery itself is fail-open (any read error logs once and
  returns ``[]`` → core-only). A fragment entry whose module basename duplicates a
  core module for the same event, or a fragment detector whose name duplicates a
  core detector, is SHADOWED (core runs, fragment copy does not). The merged
  PRIORITY detector group is returned UNCHANGED by construction (KD-2, Position 2).
- **Root rule (R8 / consolidated D1, CONFIRMED by #2381 F-L1 in-place).** Two root
  kinds: ``source`` (a ``directory``/``file`` marketplace's relative-path entry runs
  in place from ``installLocation + entry.source``, no install record required) and
  ``cache`` (every other source runs the ``installPath`` the manifest records).
- **Advisory version (R7/OR-3).** ``LIB_API_VERSION`` mismatch is warn-only; the
  warner (``plugin-fragment-version-warner``) is the delivery channel — this module
  has no output channel of its own.
- **Offline shadow replay only (R10/KD-5).** ``PLUGIN_FRAGMENTS_SHADOW=1`` is
  honoured ONLY when ``HOME`` is not the login home; in a production session it is
  ignored + logged once, so a fragment entry a core entry shadows is NEVER executed
  live.
- **Activation gate seam, LOG-ONLY until the client exists (R11).** A premium fragment
  entry / premium skill invocation is routed through :func:`premium_activation_gate`,
  which — until the private product repo #3991 activation client exists — audits a
  ``would-deny`` line and proceeds (never a deny, never a block). The probe forks
  nothing until ``hapi --version`` reports at least ``ACTIVATION_CLIENT_MIN_HAPI_VERSION``
  (None today), because an unknown ``hapi`` verb falls through to the CLI's default
  command. Once the client is present the decision is HONORED: a deny drops the
  plugin's non-safety entries. Safety controls are never license-gated.
- **Per-process memos (rp1-home#2613).** Discovery and fragment import run once per
  process per config signature; the activation state is probed once per process and
  cached ≤300 s across processes. :func:`_reset_caches` clears every memo.

See: ``.claude/docs/plugin-registry-fragment-contract.md`` (R9) and the directive
``.github/issue-assets/2212/issue-2212-directives.md``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

# Dual-path import so this module loads both as ``lib.plugin_fragments`` (package)
# and as ``plugin_fragments`` (when lib/ is sys.path[0], e.g. a standalone
# self-check). Entry is the ONLY lib import at module level (2212-7).
try:  # pragma: no cover - import topology
    from lib.hook_registry import Entry  # noqa: F401
except ImportError:  # pragma: no cover - import topology
    from hook_registry import Entry  # noqa: F401

__version__ = "1.1.0"

# ─────────────────────────────────────────────────────────────────────────────
# Plugin order + premium set (A ships PLUGIN_ORDER EMPTY; each cut appends its own
# name in its C2/E/F registry-diff commit — names not in the list sort by name).
# ─────────────────────────────────────────────────────────────────────────────
PLUGIN_ORDER: list[str] = []

# The E3 premium plugin names (the three cuts). An entry/skill from one of these is
# premium and is routed through the LOG-ONLY activation gate (R11). None are
# installed at A — the seam is dormant in production but exercised by V11.
PREMIUM_PLUGINS: frozenset[str] = frozenset(
    {"hapimatic-orchestration", "hapimatic-issue-workflow", "hapimatic-hardware"}
)

# Moved P-B safety guards are NEVER license-gated (R11). If a premium fragment ever
# ships one of these, the gate passes it through so it fires with activation absent.
SAFETY_EXEMPT_MODULES: frozenset[str] = frozenset(
    {
        "single-agent-blocker",
        "pm-clean-gate-approval-guard",
        "block-merge-agent",
        "session-self-destruction-guard",
        "crontab-vs-loopcron-guard",
    }
)

_FRAGMENT_RELPATH = os.path.join("hooks", "registry-fragment.py")

# The dispatcher's two entry kinds (hook_registry.Entry.kind). A fragment entry with
# any other kind is dropped at load (R5): the dispatcher would otherwise mis-route it.
_VALID_ENTRY_KINDS = frozenset({"inprocess", "subprocess"})

# Mirror of hook-dispatcher.py's ``_EXACT_LIST_RE`` (that module's filename is not
# importable). ``matches()`` treats a matcher that fullmatches this charset as an
# exact-name alternation list and every other non-"", non-"*" matcher as a regex — so
# only the latter is compile-checked at load (#2212 R2-2). Keep the two in sync.
_EXACT_LIST_RE = re.compile(r"[A-Za-z0-9_|, ]+")

# Process-scoped dedupe for "log once" discovery warnings. Each hook-dispatcher.py
# invocation is a fresh process, so this naturally scopes a repeated warning to one
# line per process (per R5/R8).
_logged_once: set[str] = set()

# Per-process memos (rp1-home#2613 RP-S5 / OOS-6, K-MEMO). One UserPromptSubmit runs
# the loader three times and the dispatcher is one process per hook event, so without
# these every call re-read the config (incl. the ~645 KB install manifest on live RP1),
# re-imported each fragment and re-forked the activation probe. Discovery is keyed by
# the config files' stat signature, a fragment by its own; only the activation probe
# has a cross-process cache (a state file, ≤ ACTIVATION_CACHE_TTL_S).
_DISCOVERY_MEMO: dict[tuple, list] = {}
_FRAGMENT_MEMO: dict[tuple, object] = {}
_ACTIVATION_MEMO: dict[tuple, dict] = {}


def _reset_caches() -> None:
    """Clear every per-process memo and the log-once set. Production never needs this
    (each dispatch is a fresh process); tests and long-lived callers do."""
    _DISCOVERY_MEMO.clear()
    _FRAGMENT_MEMO.clear()
    _ACTIVATION_MEMO.clear()
    _logged_once.clear()


def _now() -> float:
    """Wall-clock seconds; one seam so the activation cache's expiry is testable."""
    return time.time()


def _stat_signature(path: str):
    """``(mtime_ns, size)`` of ``path``, or None when it does not exist / is unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ─────────────────────────────────────────────────────────────────────────────
# Logging (stderr loud + optional block-queue, fail-soft). This module has no
# output channel to the agent (R7) — the warner renders the report — so these logs
# are operator-facing (stderr is dead under the dispatcher on exit 0; the block
# queue is the durable loud channel).
# ─────────────────────────────────────────────────────────────────────────────
def _log(message: str) -> None:
    """Log loudly: stderr + block-queue (fail-soft). Always safe to call."""
    try:
        print(f"plugin_fragments: {message}", file=sys.stderr)
    except Exception:
        pass
    if os.environ.get("CLAUDE_BLOCK_LOGGER_DISABLED") == "1":
        return
    try:  # block_logger is optional; never let its absence or failure propagate
        try:
            from lib.block_logger import log_hook_block
        except ImportError:
            from block_logger import log_hook_block
        log_hook_block(
            hook="plugin_fragments",
            reason=message,
            reason_class="plugin_fragment_loader",
        )
    except Exception:
        pass


def _log_once(key: str, message: str) -> None:
    """Log ``message`` at most once per process, keyed by ``key``."""
    if key in _logged_once:
        return
    _logged_once.add(key)
    _log(message)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PluginInfo:
    """One discovered, enabled, fragment-bearing plugin.

    key: the ``<name>@<marketplace>`` settings key.
    name: the key's part before ``@``.
    root: the ABSOLUTE runtime root (the tree Claude Code runs, per D1).
    root_kind: ``"source"`` (directory/file marketplace, in place) or ``"cache"``
        (``installPath``).
    version: the root's ``.claude-plugin/plugin.json`` ``version`` (informational).
    """

    key: str
    name: str
    root: str
    root_kind: str
    version: str | None = None


@dataclass
class Fragment:
    """A successfully-imported ``registry-fragment.py`` module's contract surface.

    ``load_report`` is the warner's render source (R7): ``broken`` (import failed —
    never set here since :func:`load_fragment` returns None on import error),
    ``shadowed`` (module/detector names a core entry shadows), ``rejected_detectors``
    (a fragment detector carrying ``priority``/``before`` or duplicating a core name),
    ``malformed`` (each declaration :func:`_validate_fragment_surface` dropped, with
    the reason), ``version_mismatch`` (the :func:`check_fragment_version` text, or None).
    """

    plugin: PluginInfo
    event_entries: dict
    detectors: list
    lib_api_version: object  # the fragment's declared MAJOR (int), or None
    native_sessionstart_check: object  # "hooks/<script>" or None
    load_report: dict


# ─────────────────────────────────────────────────────────────────────────────
# JSON read helpers (fail-open; the caller decides what a failure means)
# ─────────────────────────────────────────────────────────────────────────────
def _read_json(path: str):
    """Read + parse JSON. Raises on any failure — the caller's try decides policy."""
    with open(path) as fh:
        return json.load(fh)


def _read_json_or(path: str, default):
    """Read + parse JSON, returning ``default`` on any failure (no raise)."""
    try:
        return _read_json(path)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# LIB_API_VERSION accessors (dual-path import of the tiny api_version module)
# ─────────────────────────────────────────────────────────────────────────────
def _api_version_module():
    try:
        from lib import api_version
    except ImportError:
        import api_version  # type: ignore
    return api_version


def lib_api_version() -> str:
    """The core ``LIB_API_VERSION`` string (``MAJOR.MINOR``)."""
    return str(_api_version_module().LIB_API_VERSION)


def api_major() -> int:
    """The MAJOR component of the core ``LIB_API_VERSION``."""
    try:
        return int(str(lib_api_version()).split(".")[0])
    except (ValueError, TypeError, IndexError):
        return 0


def _lib_api_modules() -> list[str]:
    try:
        return list(_api_version_module().LIB_API_MODULES)
    except Exception:
        return []


def check_fragment_version(fragment: "Fragment") -> str | None:
    """Return the mismatch text when a fragment targets a different lib API MAJOR.

    None when the declared MAJOR matches core (or the fragment declares nothing —
    an undeclared version is not a mismatch, just unversioned). The warner (Task 7)
    is the ONLY caller that renders this (R7/OR-3)."""
    declared = fragment.lib_api_version
    if declared is None:
        return None
    core_major = api_major()
    modules = ", ".join(_lib_api_modules())
    try:
        declared_major = int(declared)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return (
            f"Plugin {fragment.plugin.name}: fragment LIB_API_VERSION is not an int "
            f"({declared!r}); core lib API is {lib_api_version()}. Modules: {modules}"
        )
    if declared_major != core_major:
        return (
            f"Plugin {fragment.plugin.name}: fragment targets lib API MAJOR "
            f"{declared_major}, core LIB_API_VERSION is {lib_api_version()} "
            f"(MAJOR {core_major}). Modules: {modules}"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Discovery (R8) — fail-open: any read error logs once and returns [] (core-only).
# ─────────────────────────────────────────────────────────────────────────────
def _config_dir(config_dir: str | None) -> str:
    return (
        config_dir
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or os.path.expanduser("~/.claude")
    )


def _plugins_dir(config_dir: str) -> str:
    """The plugins dir: ``$CLAUDE_CODE_PLUGIN_CACHE_DIR`` (expanded) when set, else
    ``<config_dir>/plugins`` (R8 / #2381 F-CACHEDIR). ``CLAUDE_CODE_USE_COWORK_PLUGINS``
    renames the dir in the binary; v1 does not mirror that and logs once."""
    if os.environ.get("CLAUDE_CODE_USE_COWORK_PLUGINS"):
        _log_once(
            "cowork-plugins",
            "CLAUDE_CODE_USE_COWORK_PLUGINS is set; v1 does not mirror the "
            "cowork_plugins rename — discovery uses the standard plugins dir.",
        )
    cache = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    if cache:
        return os.path.expanduser(cache)
    return os.path.join(config_dir, "plugins")


def _is_marketplace_json(path: str) -> bool:
    """True iff ``path`` ends ``/marketplace.json`` — case-insensitively, as the binary's
    ``Isn = /[\\\\/]marketplace\\.json$/i`` matches (Claude Code 2.1.280)."""
    return os.path.basename(path).lower() == "marketplace.json"


def _marketplace_dir(install_location: str) -> str:
    """The marketplace directory — the ROOT BASE a relative entry ``source`` joins onto:
    ``installLocation``, or its parent when that is a ``…/marketplace.json`` file path
    (binary ``Osn`` returns ``dirname`` for that shape; re-read at 2.1.280 for
    rp1-home#2613 live check L1)."""
    if _is_marketplace_json(install_location):
        return os.path.dirname(install_location)
    return install_location


def _marketplace_json_path(install_location: str) -> str:
    """The marketplace JSON to read (rp1-home#2613 RP-S4): a ``file`` marketplace's
    ``installLocation`` IS that JSON, so read it in place; a directory marketplace keeps
    its catalog at ``<dir>/.claude-plugin/marketplace.json``. The pin always appended
    ``.claude-plugin/marketplace.json`` to the directory, which for a file marketplace
    named a file that does not exist — the entry was never found and the plugin dropped."""
    if _is_marketplace_json(install_location) or os.path.isfile(install_location):
        return install_location
    return os.path.join(install_location, ".claude-plugin", "marketplace.json")


def _find_marketplace_entry(marketplace_json, name: str):
    if not isinstance(marketplace_json, dict):
        return None
    for entry in marketplace_json.get("plugins", []) or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _within(child: str, parent: str) -> bool:
    """True iff ``child`` is ``parent`` or a path under it (post-normpath)."""
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def _resolve_root(key, name, mkt, known_marketplaces, get_manifest):
    """Resolve a plugin's runtime root per D1. Returns ``(root, root_kind)`` or
    ``(None, None)`` when unresolvable or refused."""
    mkt_entry = (
        known_marketplaces.get(mkt) if isinstance(known_marketplaces, dict) else None
    )
    if isinstance(mkt_entry, dict):
        src = mkt_entry.get("source")
        src_kind = src.get("source") if isinstance(src, dict) else None
        if src_kind in ("directory", "file"):
            install_location = mkt_entry.get("installLocation")
            if install_location:
                mkt_dir = _marketplace_dir(install_location)
                mp = _read_json_or(_marketplace_json_path(install_location), None)
                entry = _find_marketplace_entry(mp, name)
                entry_source = entry.get("source") if isinstance(entry, dict) else None
                if isinstance(entry_source, str):
                    rel = entry_source
                    if rel.startswith("./"):
                        rel = rel[2:]
                    if os.path.isabs(entry_source) or os.path.isabs(rel):
                        _log(
                            f"Plugin source path refused: {entry_source!r} for {key} "
                            f"is absolute (must stay inside the marketplace directory)."
                        )
                        return (None, None)
                    root = os.path.normpath(os.path.join(mkt_dir, rel))
                    if not _within(root, mkt_dir):
                        _log(
                            f"Plugin source path refused: {entry_source!r} for {key} "
                            f"does not stay inside its marketplace directory {mkt_dir!r}."
                        )
                        return (None, None)
                    return (root, "source")
                # object-source entry (or absent) → fall through to cache-kind.
    # cache kind: installPath from a scope=="user" manifest row (project rows ignored).
    manifest = get_manifest()
    if isinstance(manifest, dict):
        rows = manifest.get("plugins", {}).get(key, [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("scope") == "user":
                    install_path = row.get("installPath")
                    if install_path:
                        return (install_path, "cache")
    return (None, None)


def _read_plugin_json(root: str) -> dict:
    data = _read_json_or(os.path.join(root, ".claude-plugin", "plugin.json"), {})
    return data if isinstance(data, dict) else {}


def _normalize_dep(dep):
    """Normalize a dependency spec to ``(name, marketplace|None, version|None)``.

    Forms: ``"name"`` (bare), ``"name@marketplace"``, or ``{name, marketplace?,
    version?}`` (findings §3)."""
    if isinstance(dep, str):
        name, sep, mkt = dep.partition("@")
        return (name, (mkt if sep else None), None)
    if isinstance(dep, dict):
        return (dep.get("name"), dep.get("marketplace"), dep.get("version"))
    return (None, None, None)


def _parse_version(v) -> tuple[int, int, int]:
    core = str(v or "0").split("-")[0].split("+")[0]
    nums: list[int] = []
    for part in core.split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _version_satisfies(installed, constraint) -> bool:
    """Minimal version-constraint check mirroring the common npm-style operators the
    loader must honour (findings §3, F-D5). Unknown operators are permissive."""
    if not constraint or str(constraint).strip() in ("*", "latest", "any"):
        return True
    iv = _parse_version(installed)
    c = str(constraint).strip()
    op = ""
    for candidate in (">=", "<=", "^", "~", ">", "<", "="):
        if c.startswith(candidate):
            op = candidate
            c = c[len(candidate):].strip()
            break
    cv = _parse_version(c)
    if op in ("", "="):
        return iv == cv
    if op == ">=":
        return iv >= cv
    if op == "<=":
        return iv <= cv
    if op == ">":
        return iv > cv
    if op == "<":
        return iv < cv
    if op == "^":  # caret: same major, >= constraint
        return iv[0] == cv[0] and iv >= cv
    if op == "~":  # tilde: same major.minor, >= constraint
        return iv[0] == cv[0] and iv[1] == cv[1] and iv >= cv
    return True


def _dep_satisfied(dep, resolved: dict, demoted: set) -> tuple[bool, str]:
    """True iff ``dep`` is met by a present (enabled + root-resolved), non-demoted
    plugin. Returns ``(ok, reason_when_unmet)``."""
    dep_name, dep_mkt, dep_version = _normalize_dep(dep)
    if not dep_name:
        return (True, "")  # unparseable dep spec — do not demote on it (fail-open)
    for key, info in resolved.items():
        if key in demoted:
            continue
        if dep_mkt:
            if key != f"{dep_name}@{dep_mkt}":
                continue
        else:
            if info["name"] != dep_name:  # bare name: any enabled plugin of that name (R8)
                continue
        if dep_version and not _version_satisfies(info["version"], dep_version):
            continue
        return (True, "")
    constraint = f" {dep_version}" if dep_version else ""
    target = f"{dep_name}@{dep_mkt}" if dep_mkt else dep_name
    return (False, f"{target}{constraint}")


def _discovery_key(config_dir: str, plugins_dir: str) -> tuple:
    """K-MEMO key: the resolved dirs, the cowork rename switch, and the stat signature
    of the three files discovery always reads (a missing file ⇒ None), so an edit to
    any of them in the same process is seen."""
    return (
        os.path.abspath(config_dir),
        os.path.abspath(plugins_dir),
        os.environ.get("CLAUDE_CODE_USE_COWORK_PLUGINS"),
        _stat_signature(os.path.join(config_dir, "settings.json")),
        _stat_signature(os.path.join(plugins_dir, "known_marketplaces.json")),
        _stat_signature(os.path.join(plugins_dir, "installed_plugins.json")),
    )


def discover_plugins(config_dir: str | None = None) -> list[PluginInfo]:
    """Return enabled, root-resolved, fragment-bearing, non-demoted plugins (R8).

    Memoized per process per config signature (rp1-home#2613 RP-S5, K-MEMO): one
    UserPromptSubmit used to discover three times, each re-reading the install
    manifest. A hit returns a fresh list (the ``PluginInfo`` items are frozen).

    Fail-open: any exception while reading settings / known_marketplaces / a
    marketplace manifest / the install manifest logs once and returns ``[]``."""
    try:
        resolved = _config_dir(config_dir)
        key = _discovery_key(resolved, _plugins_dir(resolved))
    except Exception:  # an unbuildable key never blocks discovery itself
        return _discover_plugins_uncached(config_dir)
    cached = _DISCOVERY_MEMO.get(key)
    if cached is None:
        cached = _discover_plugins_uncached(resolved)
        _DISCOVERY_MEMO[key] = cached
    return list(cached)


def _discover_plugins_uncached(config_dir: str | None = None) -> list[PluginInfo]:
    """:func:`discover_plugins` without the memo (the #2212 discovery, unchanged)."""
    try:
        config_dir = _config_dir(config_dir)
        plugins_dir = _plugins_dir(config_dir)

        settings = _read_json(os.path.join(config_dir, "settings.json"))
        enabled_map = (
            settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
        )
        if not isinstance(enabled_map, dict):
            enabled_map = {}

        known_marketplaces = _read_json_or(
            os.path.join(plugins_dir, "known_marketplaces.json"), {}
        )

        # The ~360 KB install manifest is read lazily — only when an enabled key needs
        # a cache-kind root (source-kind resolves without it). Memoized per call.
        manifest_box: dict[str, object] = {}

        def get_manifest():
            if "m" not in manifest_box:
                manifest_path = os.path.join(plugins_dir, "installed_plugins.json")
                manifest = _read_json_or(manifest_path, None)
                if manifest is None:
                    _log_once(
                        f"manifest-absent:{manifest_path}",
                        f"install manifest absent/unreadable at {manifest_path}; "
                        "cache-kind plugins contribute nothing (source-kind still resolve).",
                    )
                    manifest = {}
                elif isinstance(manifest, dict) and manifest.get("version") not in (2, None):
                    _log_once(
                        f"manifest-version:{manifest_path}",
                        f"install manifest {manifest_path} has version "
                        f"{manifest.get('version')!r} (expected 2); reading defensively.",
                    )
                manifest_box["m"] = manifest
            return manifest_box["m"]

        # Phase 1: resolve the root of EVERY enabled key (needed to read dependencies
        # for demotion — a fragment plugin may depend on a non-fragment plugin).
        resolved: dict[str, dict] = {}
        for key, val in enabled_map.items():
            if val is not True:  # absent handled by not being iterated; array/false → skip
                continue
            name, _, mkt = key.partition("@")
            root, root_kind = _resolve_root(
                key, name, mkt, known_marketplaces, get_manifest
            )
            if root is None or not os.path.isdir(root):
                continue
            plugin_json = _read_plugin_json(root)
            resolved[key] = {
                "name": name,
                "root": root,
                "root_kind": root_kind,
                "version": plugin_json.get("version"),
                "deps": plugin_json.get("dependencies", []) or [],
            }

        # Phase 2: dependency demotion to a fixed point (mirror Claude Code, F-D3).
        demoted: set[str] = set()
        changed = True
        while changed:
            changed = False
            for key, info in resolved.items():
                if key in demoted:
                    continue
                for dep in info["deps"]:
                    ok, reason = _dep_satisfied(dep, resolved, demoted)
                    if not ok:
                        demoted.add(key)
                        _log(
                            f"plugin {key} demoted at load: unmet dependency {reason} "
                            "(its fragment will NOT load)."
                        )
                        changed = True
                        break

        # Phase 3: fragment-bearing + not demoted.
        results: list[PluginInfo] = []
        for key, info in resolved.items():
            if key in demoted:
                continue
            if not os.path.isfile(os.path.join(info["root"], _FRAGMENT_RELPATH)):
                continue  # not one of ours — silent (never a per-plugin log, R5)
            results.append(
                PluginInfo(
                    key=key,
                    name=info["name"],
                    root=info["root"],
                    root_kind=info["root_kind"],
                    version=info["version"],
                )
            )
        return results
    except Exception as exc:  # R5: discovery fail-open → core-only
        _log(f"plugin discovery failed ({exc!r}); returning core-only.")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Fragment loading (R1/R5) — side-effect-free import; None on any import error.
# ─────────────────────────────────────────────────────────────────────────────
def load_fragment(plugin: PluginInfo) -> "Fragment | None":
    """Import ``<root>/hooks/registry-fragment.py`` and return a :class:`Fragment`.

    On ANY import error: log loudly and return ``None`` (R5 isolation — a broken
    fragment contributes nothing; every core entry still runs). The warner reports a
    ``None`` return as a broken fragment for that plugin.key.

    Memoized per process by ``(plugin key, plugin root, the fragment file's stat
    signature)`` (rp1-home#2613 K-MEMO): the merge used to re-import every fragment on
    every call. Never the whole ``PluginInfo`` — its ``version`` comes verbatim from
    ``plugin.json`` and may be an unhashable JSON value. The returned :class:`Fragment`
    is shared — callers read it and never mutate it."""
    frag_path = os.path.join(plugin.root, _FRAGMENT_RELPATH)
    key = (plugin.key, plugin.root, _stat_signature(frag_path))
    if key not in _FRAGMENT_MEMO:
        _FRAGMENT_MEMO[key] = _load_fragment_uncached(plugin)
    return _FRAGMENT_MEMO[key]  # type: ignore[return-value]


def _load_fragment_uncached(plugin: PluginInfo) -> "Fragment | None":
    """:func:`load_fragment` without the memo (the #2212 import, unchanged)."""
    frag_path = os.path.join(plugin.root, _FRAGMENT_RELPATH)
    module_name = "plugin_fragment__" + "".join(
        ch if ch.isalnum() else "_" for ch in plugin.key
    )
    import importlib.util

    # Do not write a __pycache__/*.pyc beside the plugin's hooks — a fragment is pure
    # data (R1, side-effect-free at import); the dispatcher imports it every dispatch,
    # and polluting the plugin tree with bytecode is an unwanted write.
    saved_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(module_name, frag_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {frag_path}")
        module = importlib.util.module_from_spec(spec)
        module.__name__ = module_name  # suppress the __main__ guard on import
        # A fragment's import-time print must never reach the dispatcher's stdout —
        # that is its merged-JSON channel (R5, #2212 IR-1). Route it to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            spec.loader.exec_module(module)
    except BaseException as exc:
        # Everything but KeyboardInterrupt is caught (#2212 IR-1 / R2-3): a fragment
        # raising SystemExit, asyncio.CancelledError, GeneratorExit or any other
        # BaseException at import must not escape into the dispatcher, where it would
        # block every tool call (exit 2) or silently skip every core guard (exit 0/1).
        if isinstance(exc, KeyboardInterrupt):
            raise
        _log(f"fragment import failed for {plugin.key} ({frag_path}): {exc!r}")
        return None
    finally:
        sys.dont_write_bytecode = saved_dont_write

    raw_entries = getattr(module, "EVENT_ENTRIES", None)
    raw_detectors = getattr(module, "DETECTORS", None)
    event_entries, detectors, malformed = _validate_fragment_surface(
        {} if raw_entries is None else raw_entries,
        [] if raw_detectors is None else raw_detectors,
    )
    for problem in malformed:
        _log_once(
            f"malformed:{plugin.key}:{problem}",
            f"malformed declaration in {plugin.key}'s registry-fragment: {problem}",
        )
    lib_api = getattr(module, "LIB_API_VERSION", None)
    native_ss = getattr(module, "NATIVE_SESSIONSTART_CHECK", None)
    fragment = Fragment(
        plugin=plugin,
        event_entries=event_entries,
        detectors=detectors,
        lib_api_version=lib_api,
        native_sessionstart_check=native_ss,
        load_report={
            "plugin": plugin.name,
            "key": plugin.key,
            "broken": None,
            "shadowed": [],
            "rejected_detectors": [],
            "malformed": malformed,
            "version_mismatch": None,
        },
    )
    fragment.load_report["version_mismatch"] = check_fragment_version(fragment)
    return fragment


def _matcher_problem(matcher: str) -> str | None:
    """Why a (string) matcher cannot be evaluated by ``matches()``, or ``None``
    (#2212 R2-2).

    ``matches()`` runs at ``hook-dispatcher.py`` ``dispatch()``, OUTSIDE per-module
    crash isolation, and swallows only ``re.error``. A matcher whose compile raises
    anything else (``a{99999999999}`` → OverflowError, deep nesting → RecursionError)
    would crash the whole dispatch and lose every core guard; one that raises
    ``re.error`` would silently never fire. Both are caught here, at load."""
    if matcher in ("", "*") or _EXACT_LIST_RE.fullmatch(matcher):
        return None
    try:
        re.compile(matcher)
    except Exception as exc:
        shown = matcher if len(matcher) <= 60 else matcher[:57] + "..."
        return f"matcher {shown!r} is not a valid regex ({type(exc).__name__}: {exc})"
    return None


def _entry_problem(entry) -> str | None:
    """Why a fragment ``EVENT_ENTRIES`` item cannot be merged, or ``None`` if it is
    well formed (#2212 IR-2 / R2-2).

    Checked by FIELD, not by ``isinstance(entry, Entry)``: the dual import paths
    (``lib.hook_registry`` vs ``hook_registry``) can yield two distinct ``Entry``
    classes, and either is valid. The dispatcher reads ``matcher``/``module``/``kind``
    and the merge calls ``dataclasses.replace``; ``flags`` is carry-only metadata,
    required to be a dict so a fragment entry has the same shape as a core one. A
    non-string or uncompilable matcher would otherwise reach ``matches()`` in ``dispatch()``
    (the dispatcher's guarded ``_entry_matches`` is the second layer) outside
    its per-module isolation and skip every core hook."""
    if not dataclasses.is_dataclass(entry) or isinstance(entry, type):
        return f"not an Entry instance ({type(entry).__name__})"
    matcher = getattr(entry, "matcher", None)
    if not isinstance(matcher, str):
        return f"matcher is {type(matcher).__name__}, expected str"
    matcher_problem = _matcher_problem(matcher)
    if matcher_problem:
        return matcher_problem
    module = getattr(entry, "module", None)
    if not isinstance(module, str) or not module:
        return "module must be a non-empty str"
    kind = getattr(entry, "kind", None)
    if kind not in _VALID_ENTRY_KINDS:
        return f"kind {kind!r} is not one of {sorted(_VALID_ENTRY_KINDS)}"
    flags = getattr(entry, "flags", None)
    if not isinstance(flags, dict):
        return f"flags is {type(flags).__name__}, expected dict"
    return None


def _detector_problem(det) -> str | None:
    """Why a fragment ``DETECTORS`` item cannot be merged, or ``None`` (#2212 IR-3)."""
    if not isinstance(det, dict):
        return f"not a dict ({type(det).__name__})"
    name = det.get("name")
    if not isinstance(name, str) or not name:
        return "name must be a non-empty str"
    if "path" in det and (not isinstance(det["path"], str) or not det["path"]):
        return f"detector {name!r}: path must be a non-empty str"
    return None


def _known_events() -> frozenset | None:
    """The events hook-dispatcher.py is wired for — the ``EVENT_REGISTRY`` keys of the
    same hook_registry module that supplied ``Entry`` (#2212 R2-4). ``None`` when that
    registry cannot be read, so the check is skipped rather than dropping entries."""
    try:
        registry = getattr(sys.modules.get(Entry.__module__), "EVENT_REGISTRY", None)
        return frozenset(registry) if registry else None
    except Exception:
        return None


def _entry_script_exists(abs_module: str) -> bool:
    """True when hook_registry.get_hook_path would load a script for this absolute entry
    module: the path itself, or the path plus ``.py`` / ``.sh`` (#2212 R3-2 — the R2-5
    check used the literal path and wrongly skipped extensionless modules, which the
    dispatcher resolves). Detectors have no such fallback, so they keep an exact check."""
    return any(os.path.isfile(abs_module + ext) for ext in ("", ".py", ".sh"))


def _detector_path(det: dict, plugin_root: str) -> str:
    """A fragment detector's absolute script path: its ``path`` (relative to the plugin
    root unless already absolute), defaulting to ``hooks/<name>.py``."""
    rel = det.get("path") or os.path.join("hooks", f"{det.get('name')}.py")
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(plugin_root, rel))


def _validate_fragment_surface(raw_entries, raw_detectors) -> tuple[dict, list, list[str]]:
    """Return ``(event_entries, detectors, malformed)`` with every malformed item
    dropped and described (#2212 IR-3).

    Validation happens ONCE, here at load, so the merge and the report never see a
    shape that would raise mid-loop and take a healthy sibling plugin down with it.
    Each dropped item is named in ``malformed`` — logged by the caller and rendered by
    the SessionStart warner — never discarded silently."""
    malformed: list[str] = []
    entries: dict = {}
    known_events = _known_events()
    if not isinstance(raw_entries, dict):
        malformed.append(
            f"EVENT_ENTRIES is {type(raw_entries).__name__}, expected a dict — ignored"
        )
        raw_entries = {}
    for event, items in raw_entries.items():
        if not isinstance(event, str):
            malformed.append(f"EVENT_ENTRIES key {event!r} is not an event name — ignored")
            continue
        if known_events is not None and event not in known_events:
            # #2212 R2-4: the dispatcher is wired only for the EVENT_REGISTRY events, so
            # entries under any other key (e.g. a "PreToolUSE" typo) would never run.
            malformed.append(
                f"EVENT_ENTRIES key {event!r} is not a dispatcher event "
                f"(expected one of {sorted(known_events)}) — ignored"
            )
            continue
        if not isinstance(items, (list, tuple)):
            malformed.append(
                f"EVENT_ENTRIES[{event!r}] is {type(items).__name__}, expected a list — ignored"
            )
            continue
        kept = []
        for index, entry in enumerate(items):
            problem = _entry_problem(entry)
            if problem:
                malformed.append(f"EVENT_ENTRIES[{event!r}][{index}] dropped: {problem}")
            else:
                kept.append(entry)
        entries[event] = kept
    detectors: list = []
    if not isinstance(raw_detectors, (list, tuple)):
        malformed.append(
            f"DETECTORS is {type(raw_detectors).__name__}, expected a list — ignored"
        )
        raw_detectors = []
    for index, det in enumerate(raw_detectors):
        problem = _detector_problem(det)
        if problem:
            malformed.append(f"DETECTORS[{index}] dropped: {problem}")
        else:
            detectors.append(det)
    return entries, detectors, malformed


# ─────────────────────────────────────────────────────────────────────────────
# Shadow (R10) — offline replay only; honoured iff HOME is not the login home.
# ─────────────────────────────────────────────────────────────────────────────
def _login_home() -> str | None:
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return None


def _shadow_active() -> bool:
    if os.environ.get("PLUGIN_FRAGMENTS_SHADOW") != "1":
        return False
    login = _login_home()
    home = os.environ.get("HOME")
    if home and login and os.path.realpath(home) == os.path.realpath(login):
        _log_once(
            "shadow-ignored",
            "PLUGIN_FRAGMENTS_SHADOW=1 ignored: HOME is the login home. The offline "
            "shadow differ runs under a fresh HOME; fragment entries are never "
            "executed live by the shadow machinery.",
        )
        return False
    return True


def _shadow_remove_names() -> set[str]:
    """The core module/detector names the shadow removal set drops before merging
    (R10). Empty unless the shadow is active AND the env names some."""
    if not _shadow_active():
        return set()
    raw = os.environ.get("PLUGIN_FRAGMENTS_SHADOW_REMOVE", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


# ─────────────────────────────────────────────────────────────────────────────
# Ordering + basename helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ordered(plugins: list[PluginInfo]) -> list[PluginInfo]:
    order = {name: i for i, name in enumerate(PLUGIN_ORDER)}
    return sorted(plugins, key=lambda p: (order.get(p.name, len(PLUGIN_ORDER)), p.name))


def _basename(module: str) -> str:
    """The bare module basename (no dir, no extension) used for the shadow rule."""
    base = os.path.basename(module)
    for ext in (".py", ".sh"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _resolve_entry_module(entry, plugin_root: str) -> str:
    """Resolve a fragment entry's relative ``module`` to an absolute path under the
    plugin root (KD-1). An already-absolute module is returned unchanged."""
    module = entry.module
    if os.path.isabs(module):
        return module
    return os.path.normpath(os.path.join(plugin_root, module))


# ─────────────────────────────────────────────────────────────────────────────
# Merge (R3/R4/R6) — core first, then each enabled plugin in PLUGIN_ORDER-then-name.
# ─────────────────────────────────────────────────────────────────────────────
def merged_event_entries(event: str, core_entries: list[Entry]) -> list[Entry]:
    """Append enabled plugins' ``EVENT_ENTRIES[event]`` AFTER ``core_entries`` (R3).

    Byte no-op when ``discover_plugins()`` is empty and no shadow removal is active
    (R2). A fragment entry whose module basename duplicates a core module for this
    event is SHADOWED (R6). Fail-open: any error returns ``core_entries`` unchanged."""
    try:
        remove = _shadow_remove_names()
        if remove:
            core = [e for e in core_entries if _basename(e.module) not in remove]
        else:
            core = list(core_entries)

        plugins = discover_plugins()
        if not plugins:
            return core_entries if not remove else core

        core_basenames = {_basename(e.module) for e in core}
        merged = list(core)
        for plugin in _ordered(plugins):
            # Per-plugin isolation (#2212 IR-3): an error inside ONE plugin's merge
            # skips that plugin only — healthy siblings and every core entry survive.
            try:
                fragment = load_fragment(plugin)
                if fragment is None:
                    continue  # broken → skip this plugin only (R5)
                plugin_entries = []
                for entry in fragment.event_entries.get(event, []) or []:
                    base = _basename(entry.module)
                    if base in core_basenames:
                        continue  # SHADOWED: core wins (R6); reported by the warner
                    abs_module = _resolve_entry_module(entry, plugin.root)
                    if not _entry_script_exists(abs_module):
                        # #2212 R2-5: a missing script would silently never run.
                        _log_once(
                            f"missing:{plugin.key}:{abs_module}",
                            f"{plugin.key}: {event} entry {entry.module!r} skipped — "
                            f"file not found ({abs_module})",
                        )
                        continue
                    plugin_entries.append(dataclasses.replace(entry, module=abs_module))
                # R11 seam (rp1-home#2613 R4/R6): a premium plugin that CONTRIBUTES
                # entries to this event is routed through the activation gate — probed
                # once per process, cached ≤300 s across processes, audited once per
                # session — and the decision is HONORED: on deny only the entries the
                # gate exempts as safety controls still merge. While the activation
                # client is absent the gate is log-only and every entry merges.
                if plugin.name in PREMIUM_PLUGINS and plugin_entries:
                    decision = _gate_for_plugin(plugin.name, os.environ)
                    if decision.get("decision") == "deny":
                        plugin_entries = [
                            e for e in plugin_entries
                            if _basename(e.module) in SAFETY_EXEMPT_MODULES
                            and premium_activation_gate(
                                _basename(e.module), subject_kind="entry",
                                plugin_name=plugin.name, is_safety=True,
                            ).get("decision") == "allow"
                        ]
                merged.extend(plugin_entries)
            except BaseException as exc:  # all but KeyboardInterrupt (#2212 R2-3)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                _log(
                    f"plugin {plugin.key} skipped for {event!r} ({exc!r}); other "
                    "plugins and every core entry still run."
                )
        return merged
    except Exception as exc:
        _log(f"merged_event_entries({event!r}) failed ({exc!r}); core-only.")
        return core_entries


def merged_detectors(core_priority: list, core_independent: list) -> tuple[list, list]:
    """Return ``(priority, independent)`` with the PRIORITY group UNCHANGED (KD-2,
    Position 2) and the INDEPENDENT group = core + each fragment's ``DETECTORS`` in
    ``PLUGIN_ORDER``-then-name order (R6).

    A fragment detector carrying ``priority``/``before`` is REJECTED by name (loud). One
    whose name duplicates a CORE detector (either group) is SHADOWED — core wins, and
    the SessionStart warner reports it once per session; this per-dispatch merge stays
    silent, exactly as for a shadowed entry (rp1-home#2613: C1's detectors are shadowed
    by design until C2, and a log here wrote a block-queue row on every prompt). A name
    an EARLIER fragment already contributed (or one declared twice in the same
    fragment) keeps the first copy in ``PLUGIN_ORDER``-then-name order and is logged
    once per process against its owner (OOS-5). Byte no-op when ``discover_plugins()``
    is empty and no shadow removal is active. Fail-open."""
    try:
        remove = _shadow_remove_names()
        core_prio_names = {d.get("name") for d in core_priority}
        if remove:
            safe_remove: set[str] = set()
            for name in remove:
                if name in core_prio_names:
                    _log(
                        f"shadow removal refused for priority detector {name!r}: "
                        "Position 2 forbids moving a priority detector."
                    )
                else:
                    safe_remove.add(name)
            independent = [
                d for d in core_independent if d.get("name") not in safe_remove
            ]
        else:
            independent = list(core_independent)

        plugins = discover_plugins()
        if not plugins:
            # PRIORITY group is ALWAYS returned unchanged (identity), by construction.
            return (core_priority, core_independent if not remove else independent)

        core_indep_names = {d.get("name") for d in independent}
        # name → the plugin whose copy runs; kept apart from the CORE names so a
        # cross-fragment duplicate is attributed to its owner, never to core (OOS-5).
        fragment_owner: dict[str, str] = {}
        merged_independent = list(independent)
        for plugin in _ordered(plugins):
            # Per-plugin isolation (#2212 IR-3): one plugin's error drops only its own
            # detectors; names are committed to the owner map only on success.
            try:
                fragment = load_fragment(plugin)
                if fragment is None:
                    continue
                plugin_dets: list[dict] = []
                plugin_names: set[str] = set()
                for det in fragment.detectors or []:
                    if not isinstance(det, dict):
                        continue
                    name = det.get("name")
                    if not name:
                        continue
                    if "priority" in det or "before" in det:
                        _log(
                            f"rejected fragment detector {name!r} from {plugin.name}: a "
                            "fragment contributes INDEPENDENT detectors only (KD-2); a "
                            "priority/before key is not yet supported."
                        )
                        continue
                    if name in core_prio_names or name in core_indep_names:
                        continue  # SHADOWED: core wins (R6); reported by the warner
                    if name in plugin_names:
                        _log_once(
                            f"dup-detector:{plugin.key}:{name}",
                            f"rejected fragment detector {name!r} from {plugin.name}: "
                            f"declared twice in {plugin.name} (the first declaration wins).",
                        )
                        continue
                    if name in fragment_owner:
                        _log_once(
                            f"dup-detector:{plugin.key}:{name}",
                            f"rejected fragment detector {name!r} from {plugin.name}: "
                            f"duplicates detector {name!r} already contributed by "
                            f"{fragment_owner[name]} (first in PLUGIN_ORDER-then-name wins).",
                        )
                        continue
                    abs_path = _detector_path(det, plugin.root)
                    if not os.path.isfile(abs_path):
                        # #2212 R2-5: a missing detector script would silently never run.
                        _log_once(
                            f"missing:{plugin.key}:{abs_path}",
                            f"{plugin.key}: detector {name!r} skipped — file not found "
                            f"({abs_path})",
                        )
                        continue
                    plugin_dets.append({"name": name, "path": abs_path})
                    plugin_names.add(name)
                merged_independent.extend(plugin_dets)
                for accepted in plugin_names:
                    fragment_owner[accepted] = plugin.name
            except BaseException as exc:  # all but KeyboardInterrupt (#2212 R2-3)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                _log(
                    f"plugin {plugin.key} detectors skipped ({exc!r}); other plugins "
                    "and every core detector still run."
                )
        return (core_priority, merged_independent)
    except Exception as exc:
        _log(f"merged_detectors failed ({exc!r}); core-only.")
        return (core_priority, core_independent)


# ─────────────────────────────────────────────────────────────────────────────
# Per-plugin load reports (R7) — the warner's render source.
# ─────────────────────────────────────────────────────────────────────────────
def collect_fragment_reports(
    event_registry: dict,
    core_priority: list,
    core_independent: list,
    config_dir: str | None = None,
) -> list[dict]:
    """Assemble one report dict per discovered plugin that has something to report
    (broken fragment, version mismatch, shadowed entry/detector, rejected detector, a
    duplicate detector, or a malformed declaration — including an entry/detector whose
    script file is missing). Empty list when nothing is wrong. Used by the SessionStart
    warner.

    ``duplicates`` (rp1-home#2613 OOS-5, K-DUP) lists ``{"detector", "owner"}`` for each
    fragment detector a copy from ``owner`` already provides — an EARLIER plugin in
    ``PLUGIN_ORDER``-then-name order, or this plugin itself when it declares the name
    twice — with the merge's acceptance rules, so the report names exactly the copies
    the merge drops."""
    reports: list[dict] = []
    try:
        plugins = discover_plugins(config_dir)
    except Exception as exc:  # defensive — discover is already fail-open
        _log(f"collect_fragment_reports discovery failed ({exc!r}).")
        return reports

    core_event_basenames = {
        event: {_basename(e.module) for e in entries}
        for event, entries in (event_registry or {}).items()
    }
    core_detector_names = {d.get("name") for d in core_priority} | {
        d.get("name") for d in core_independent
    }
    fragment_owner: dict[str, str] = {}  # detector name → the plugin whose copy runs

    def _broken_report(plugin: PluginInfo, error: str | None = None) -> dict:
        report = {
            "plugin": plugin.name,
            "key": plugin.key,
            "broken": True,
            "shadowed": [],
            "rejected_detectors": [],
            "duplicates": [],
            "malformed": [],
            "version_mismatch": None,
        }
        if error:
            report["error"] = error
        return report

    for plugin in _ordered(plugins):
        # Per-plugin isolation (#2212 IR-3): one plugin that cannot be reported on is
        # reported as broken — it never silences the reports for its siblings.
        try:
            fragment = load_fragment(plugin)
            if fragment is None:
                reports.append(_broken_report(plugin))
                continue
            shadowed: list[str] = []
            # Declarations dropped at load, plus (#2212 R2-5) every entry/detector the
            # merge would run whose script file does not exist — the merge skips those.
            malformed: list[str] = list(fragment.load_report.get("malformed") or [])
            for event, entries in (fragment.event_entries or {}).items():
                core_bn = core_event_basenames.get(event, set())
                for entry in entries or []:
                    base = _basename(entry.module)
                    if base in core_bn:
                        shadowed.append(f"{event}:{base}")
                        continue
                    abs_module = _resolve_entry_module(entry, plugin.root)
                    if not _entry_script_exists(abs_module):
                        malformed.append(
                            f"{event} entry {entry.module!r} skipped: file not found "
                            f"({abs_module})"
                        )
            rejected: list[str] = []
            duplicates: list[dict] = []
            accepted: set[str] = set()  # this plugin's detector names the merge keeps
            for det in fragment.detectors or []:
                if not isinstance(det, dict):
                    continue
                name = det.get("name")
                if not name:
                    continue
                if "priority" in det or "before" in det:
                    rejected.append(f"{name} (priority/before not allowed)")
                elif name in core_detector_names:
                    shadowed.append(f"detector:{name}")
                elif name in accepted or name in fragment_owner:
                    owner = plugin.name if name in accepted else fragment_owner[name]
                    duplicates.append({"detector": name, "owner": owner})
                elif not os.path.isfile(_detector_path(det, plugin.root)):
                    malformed.append(
                        f"detector {name!r} skipped: file not found "
                        f"({_detector_path(det, plugin.root)})"
                    )
                else:
                    accepted.add(name)
            for name in accepted:
                fragment_owner[name] = plugin.name
            report = {
                "plugin": plugin.name,
                "key": plugin.key,
                "broken": None,
                "shadowed": shadowed,
                "rejected_detectors": rejected,
                "duplicates": duplicates,
                "malformed": malformed,
                "version_mismatch": fragment.load_report.get("version_mismatch"),
            }
            if (shadowed or rejected or duplicates or report["malformed"]
                    or report["version_mismatch"]):
                reports.append(report)
        except BaseException as exc:  # all but KeyboardInterrupt (#2212 R2-3)
            if isinstance(exc, KeyboardInterrupt):
                raise
            _log(f"collect_fragment_reports: report for {plugin.key} failed ({exc!r}).")
            reports.append(_broken_report(plugin, error=repr(exc)))
    return reports


# ─────────────────────────────────────────────────────────────────────────────
# R11 — activation-gate seam (LOG-ONLY until the private product repo #3991 client).
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_hapi_binary(env) -> str | None:
    """Resolve this instance's OWN Core binary (the ``hapi`` that owns ``$HAPI_HOME``).

    A ``HAPI_BIN`` override wins (hermetic tests set it); otherwise ``hapi`` is found
    on the env's ``PATH``. None when no ``hapi`` resolves."""
    explicit = env.get("HAPI_BIN")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    return shutil.which("hapi", path=env.get("PATH"))


# K-PROBE (rp1-home#2613 RP-S3, OD-1 = A — ruled 2026-10-05): the probe never runs
# ``hapi activation status`` unless ``hapi --version`` reports at least this Core
# version. ``--version`` exits before the CLI resolves a command, while an UNKNOWN verb
# does not exit non-zero: it falls through to the default ``claude`` command (token
# init, server auto-start, a detached daemon, a session). None until the activation
# client ships: until then, the probe forks nothing at all.
# ⚠️ Before setting it: ``hapi --version`` prints the CLI package's version, which has
# read 0.6.0 since 2026-01-05 while releases are numbered separately (1.0.NNNN), so no
# value here can tell a build with the activation client from one without it. The
# client's release must first make ``--version`` report a version that moves with it,
# or provide another side-effect-free presence signal (OD-1 option B) — otherwise a
# 1.0.NNNN minimum reads every build as client-absent and 0.6.0 re-opens the fallthrough.
# Decide with it: past the pre-gate a non-zero exit or schema-less output of
# ``activation status`` still reads client-absent (the #2212 contract); once the gate
# proves the verb exists, the client's contract may want those to deny instead.
ACTIVATION_CLIENT_MIN_HAPI_VERSION: str | None = None

# K-CACHE: within one process the activation state is probed at most once; across
# processes a probed state is reused for this long, and never past a reported
# ``grant_exp``.
ACTIVATION_CACHE_TTL_S = 300
_ACTIVATION_VERSION_TIMEOUT_S = 5
_ACTIVATION_PROBE_TIMEOUT_S = 10
_AUDIT_MARKER_MAX_AGE_S = 7 * 24 * 3600
_HAPI_VERSION_RE = re.compile(r"hapi version:\s*v?(\d+(?:\.\d+){0,2})")
_ALLOW_STATES = frozenset({"active", "gateway_unreachable_grant_valid"})
_ACTIVATION_STATES = frozenset(
    {
        "client-absent", "active", "gateway_unreachable_grant_valid", "grant-expired",
        "unregistered", "state_invalid", "probe_timeout", "probe_failed",
    }
)


def _grant_epoch(grant_exp) -> float | None:
    """``grant_exp`` (epoch seconds, or an ISO-8601 string; naive ⇒ UTC) as epoch
    seconds, or None when it cannot be read."""
    if isinstance(grant_exp, bool):
        return None
    if isinstance(grant_exp, (int, float)):
        return float(grant_exp)
    try:
        from datetime import datetime, timezone

        exp_dt = datetime.fromisoformat(str(grant_exp).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        return exp_dt.timestamp()
    except Exception:
        return None


def _grant_valid(grant_exp) -> bool:
    epoch = _grant_epoch(grant_exp)
    return epoch is not None and time.time() < epoch


def _hapi_version(hapi: str, env) -> str | None:
    """The ``X.Y.Z`` that ``hapi --version`` prints (``hapi version: X.Y.Z``), or None."""
    try:
        proc = subprocess.run(
            [hapi, "--version"],
            capture_output=True,
            text=True,
            timeout=_ACTIVATION_VERSION_TIMEOUT_S,
            env={k: v for k, v in env.items()},
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    match = _HAPI_VERSION_RE.search(proc.stdout or "")
    return match.group(1) if match else None


def _probe_activation_detail(env) -> dict:
    """Probe the activation state (R11(a)/(b)) → ``{"state", "grant_exp"}``. ``state`` is
    one of ``client-absent``, ``active``, ``gateway_unreachable_grant_valid``,
    ``grant-expired``, ``unregistered``, ``state_invalid``, ``probe_timeout``,
    ``probe_failed``.

    The MODE is selected by the ABSENCE of the activation client — never by state-file
    absence. K-PROBE (rp1-home#2613): the client is ABSENT unless ``hapi`` resolves, the
    minimum version is set, and ``hapi --version`` reports at least that version; only
    then does ``hapi activation status --json`` run (exit 0 + JSON with a ``schema`` key
    ⇒ client PRESENT, follow the reported state). Once ``--version`` has proven the
    client present, a hung or failing probe is a DENY state (``probe_timeout`` /
    ``probe_failed``, KD-1), never "client absent"."""
    absent = {"state": "client-absent", "grant_exp": None}
    hapi = _resolve_hapi_binary(env)
    minimum = ACTIVATION_CLIENT_MIN_HAPI_VERSION
    if not hapi or minimum is None:
        return absent
    version = _hapi_version(hapi, env)
    if version is None:
        # ``--version`` timed out, failed or printed nothing parsable: client-absent for
        # THIS call only — ``transient`` keeps it out of the cross-process cache, so one
        # slow ``--version`` never reads a present client as absent for 300 s.
        return {"state": "client-absent", "grant_exp": None, "transient": True}
    if _parse_version(version) < _parse_version(minimum):
        return absent
    try:
        proc = subprocess.run(
            [hapi, "activation", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_ACTIVATION_PROBE_TIMEOUT_S,
            env={k: v for k, v in env.items()},
        )
    except subprocess.TimeoutExpired:
        return {"state": "probe_timeout", "grant_exp": None}
    except Exception:
        return {"state": "probe_failed", "grant_exp": None}
    if proc.returncode != 0:
        return absent  # non-zero ⇒ not the client (the #2212 contract, unchanged)
    try:
        data = json.loads(proc.stdout or "")
    except Exception:
        return absent
    if not isinstance(data, dict) or "schema" not in data:
        return absent
    # Client PRESENT — follow the reported state (state.json).
    state = data.get("state")
    grant_exp = data.get("grant_exp")
    if state in _ALLOW_STATES:
        if grant_exp is not None and not _grant_valid(grant_exp):
            return {"state": "grant-expired", "grant_exp": grant_exp}
        return {"state": state, "grant_exp": grant_exp}
    if state in ("unregistered", "state_invalid"):
        return {"state": state, "grant_exp": grant_exp}
    return {"state": "state_invalid", "grant_exp": grant_exp}  # unknown/missing ⇒ deny


def _probe_activation(env) -> str:
    """The probed activation state (uncached) — see :func:`_probe_activation_detail`."""
    return _probe_activation_detail(env)["state"]


def _activation_config_dir(env) -> str:
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".claude"
    )


def _activation_state_dir(env) -> str:
    return os.path.join(_activation_config_dir(env), "state", "plugin-activation")


def _probe_cache_path(key: tuple, env) -> str:
    digest = hashlib.sha1("|".join(key).encode()).hexdigest()[:12]
    return os.path.join(_activation_state_dir(env), f"probe-{digest}.json")


def _read_probe_cache(path: str, now: float) -> dict | None:
    """A cached state still fresh at ``now`` — expiry is ``checked_at + TTL``, and for an
    ALLOW state also never past its ``grant_exp`` — or None (absent, unreadable,
    malformed, stale, or dated after ``now``). A deny state's ``grant_exp`` (e.g. the
    past one behind ``grant-expired``) does not bound it: the state is already a deny."""
    data = _read_json_or(path, None)
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    checked_at = data.get("checked_at")
    if state not in _ACTIVATION_STATES or isinstance(checked_at, bool) or not isinstance(
        checked_at, (int, float)
    ):
        return None
    if checked_at > now + 5:  # written "in the future" (clock step): never trust it
        return None
    expires = checked_at + ACTIVATION_CACHE_TTL_S
    grant_exp = data.get("grant_exp")
    if state in _ALLOW_STATES and grant_exp is not None:
        grant_epoch = _grant_epoch(grant_exp)
        if grant_epoch is None:
            return None
        expires = min(expires, grant_epoch)
    if now >= expires:
        return None
    return {"state": state, "grant_exp": grant_exp}


def _write_probe_cache(path: str, result: dict, now: float) -> None:
    """Atomically record a probed state (fail-soft: the cache is an optimisation)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(
                {"state": result.get("state"), "grant_exp": result.get("grant_exp"),
                 "checked_at": now},
                fh,
            )
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _activation_state(env) -> dict:
    """The activation state the MERGE acts on (rp1-home#2613 R4, K-CACHE): probed at
    most once per process per ``(hapi path, HAPI_HOME)``, and reused across processes
    from ``<config>/state/plugin-activation/probe-<hash>.json`` while fresh. With no
    ``hapi`` or no minimum version there is nothing to probe — no fork, no file."""
    hapi = _resolve_hapi_binary(env)
    if not hapi or ACTIVATION_CLIENT_MIN_HAPI_VERSION is None:
        return {"state": "client-absent", "grant_exp": None}
    key = (hapi, env.get("HAPI_HOME") or "")
    memo = _ACTIVATION_MEMO.get(key)
    if memo is not None:
        return memo
    path = _probe_cache_path(key, env)
    result = _read_probe_cache(path, _now())
    if result is None:
        result = _probe_activation_detail(env)
        if not result.get("transient"):
            _write_probe_cache(path, result, _now())
    _ACTIVATION_MEMO[key] = result
    return result


def _activation_decision(state: str, subject_name: str, subject_kind: str,
                         plugin_name: str | None) -> dict:
    """The gate's decision for a probed ``state`` (no side effects). A ``log-only`` or
    ``deny`` decision carries the ``audit`` record its caller emits."""
    if state == "client-absent":
        audit = {
            "event": "would-deny",
            "subject_kind": subject_kind,
            "plugin": plugin_name,
            "subject": subject_name,
            "missing_state": "activation-client-absent",
        }
        return {"decision": "allow", "mode": "log-only", "state": state,
                "subject": subject_name, "audit": audit}
    if state in _ALLOW_STATES:
        return {"decision": "allow", "mode": "enforced", "state": state,
                "subject": subject_name}
    # Client present + no valid grant (or a hung/failing probe, KD-1) → deny.
    reason = f"premium {subject_kind} {subject_name!r} denied: activation {state}"
    audit = {
        "event": "deny",
        "subject_kind": subject_kind,
        "plugin": plugin_name,
        "subject": subject_name,
        "state": state,
        "reason": reason,
    }
    return {"decision": "deny", "mode": "enforced", "state": state,
            "subject": subject_name, "reason": reason, "audit": audit}


def _activation_session_key(env) -> str:
    """The session an audit line belongs to: ``HAPI_SESSION_ID``, else
    ``CLAUDE_SESSION_ID``, else a one-hour bucket (the dispatcher's payload is not
    plumbed into the loader)."""
    for var in ("HAPI_SESSION_ID", "CLAUDE_SESSION_ID"):
        value = env.get(var)
        if value:
            return str(value)
    return f"hour-{int(_now() // 3600)}"


def _safe_token(text) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(text))[:120] or "_"


def _emit_activation_audit(audit: dict, env, session: str | None = None) -> None:
    """Emit one audit line: stderr (live only for a direct caller — dead under the
    dispatcher) + the durable JSONL ``<config>/logs/plugin-activation-audit.jsonl``,
    which ``PLUGIN_ACTIVATION_AUDIT_LOG`` overrides (rp1-home#2613 R4)."""
    try:
        print(f"plugin-activation {audit.get('event', 'audit')}: "
              + json.dumps(audit, sort_keys=True), file=sys.stderr)
    except Exception:
        pass
    record = dict(audit)
    record["session"] = session or _activation_session_key(env)
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now()))
    log_path = env.get("PLUGIN_ACTIVATION_AUDIT_LOG") or os.path.join(
        _activation_config_dir(env), "logs", "plugin-activation-audit.jsonl"
    )
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def _prune_audit_markers(state_dir: str) -> None:
    """Drop audit-dedupe markers older than a week (fail-soft; runs only when a new
    marker is created, i.e. at most once per session per plugin)."""
    cutoff = time.time() - _AUDIT_MARKER_MAX_AGE_S
    try:
        with os.scandir(state_dir) as entries:
            for item in entries:
                if item.name.startswith("audit-") and item.stat().st_mtime < cutoff:
                    with contextlib.suppress(OSError):
                        os.remove(item.path)
    except OSError:
        pass


def _emit_activation_audit_once(audit: dict, env, plugin_name: str) -> None:
    """Emit ``audit`` at most once per session per plugin per audit event (R4): an
    exclusively-created marker under ``<config>/state/plugin-activation/`` is the dedupe
    record, so concurrent processes of one session still audit once. When the marker
    cannot be created or written the line is emitted anyway — over-auditing beats a
    lost line. Never raises."""
    session = _activation_session_key(env)
    state_dir = _activation_state_dir(env)
    marker = os.path.join(
        state_dir,
        f"audit-{_safe_token(session)}-{_safe_token(plugin_name)}-"
        f"{_safe_token(audit.get('event'))}.json",
    )
    with contextlib.suppress(OSError):  # e.g. the path exists as a regular file
        os.makedirs(state_dir, exist_ok=True)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return  # this session already audited this plugin's decision
    except OSError:
        fd = None
    if fd is not None:
        try:
            os.write(fd, json.dumps({"session": session, "plugin": plugin_name}).encode())
        except OSError:
            pass  # an empty marker still dedupes; the audit line below is what matters
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        _prune_audit_markers(state_dir)
    _emit_activation_audit(audit, env, session=session)


def premium_activation_gate(
    subject_name: str,
    *,
    subject_kind: str = "entry",
    plugin_name: str | None = None,
    is_safety: bool = False,
    env=None,
) -> dict:
    """Route a premium fragment entry / premium skill invocation through the
    activation gate (R11). Returns a decision dict; NEVER raises.

    Posture until the activation client exists: ``decision="allow"``,
    ``mode="log-only"`` with one ``would-deny`` audit line. Safety controls are never
    gated (``is_safety=True`` ⇒ ``allow`` unconditionally). When the client IS present
    the gate enforces: a valid T-ACT grant (``active`` /
    ``gateway_unreachable_grant_valid``) allows; an expired/absent grant, an
    ``unregistered``/``state_invalid`` state, or a hung/failing probe denies, with one
    ``deny`` audit line.

    This direct form probes on every call (KD-3 — tests drive it with a per-test fake
    ``hapi``); the per-dispatch merge uses :func:`_gate_for_plugin`, which caches the
    probe and dedupes the audit per session."""
    env = env if env is not None else os.environ
    if is_safety:
        return {"decision": "allow", "mode": "safety-exempt", "subject": subject_name}
    try:
        state = _probe_activation(env)
    except Exception as exc:  # fail-open to log-only on any probe error
        _log(f"activation probe failed ({exc!r}); treating as log-only.")
        state = "client-absent"
    decision = _activation_decision(state, subject_name, subject_kind, plugin_name)
    if decision.get("audit"):
        _emit_activation_audit(decision["audit"], env)
    return decision


def _gate_for_plugin(plugin_name: str, env) -> dict:
    """The merge's per-plugin gate (rp1-home#2613 R4/R6): the cached activation state
    (:func:`_activation_state`), and its audit line once per session per plugin. A
    failure of the gate machinery itself is logged once and fails open to log-only."""
    try:
        state = _activation_state(env)["state"]
    except Exception as exc:
        _log_once("activation-state", f"activation state lookup failed ({exc!r}); treating as log-only.")
        state = "client-absent"
    decision = _activation_decision(state, plugin_name, "plugin-hooks", plugin_name)
    if decision.get("audit"):
        try:
            _emit_activation_audit_once(decision["audit"], env, plugin_name)
        except Exception as exc:  # an audit failure never changes the decision
            _log_once("activation-audit", f"activation audit failed ({exc!r}).")
    return decision
