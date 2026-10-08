"""
LIB_API_VERSION — the versioned contract surface of ``.claude/hooks/lib/`` that a
plugin registry fragment (``<plugin-root>/hooks/registry-fragment.py``) may import.

VERSION: 1.0.0  (this module's own file version; distinct from LIB_API_VERSION below)

Part of the plugin registry-fragment contract + loader (rp1-home#2212, epic #2211
Premium plugin packaging E3). A fragment declares the MAJOR of this API it targets
via ``LIB_API_VERSION: int``; the loader (:mod:`plugin_fragments`) warns — never
blocks — when that MAJOR disagrees with :data:`LIB_API_VERSION`'s major here
(rendered once per session by the ``plugin-fragment-version-warner`` SessionStart
hook, R7).

Why MAJOR.MINOR here but only MAJOR in a fragment (OR-3):

- ``LIB_API_VERSION`` is a ``"MAJOR.MINOR"`` string. MAJOR changes when a listed
  module's SIGNATURE or BEHAVIOUR changes (a break for importers); MINOR changes
  when a NEW module is ADDED to :data:`LIB_API_MODULES` (additive — old fragments
  keep working). A fragment declares only the MAJOR it was written against, so a
  MINOR bump never trips its warning.
- The warning is advisory because ``LIB_API_VERSION`` cannot MAKE a broken import
  succeed: a genuine breaking change still crashes the plugin's hook at import,
  which the dispatcher's ``execute_entry`` contains as fail-OPEN for that one
  module (OR-3). For a P-B safety guard that fails open, the warner is the ONLY
  signal — hence warn-loud, once per session.

Kept as a SEPARATE tiny module (not a constant inside :mod:`plugin_fragments`) so a
fragment can ``from lib.api_version import LIB_API_VERSION`` (dual-path) without
importing the whole loader, and so a ``lib/`` break-vs-add decision has one obvious
home to bump.
"""

from __future__ import annotations

__version__ = "1.0.0"

# The versioned API surface, MAJOR.MINOR. Bump MAJOR on any signature/behaviour
# change to a listed module; bump MINOR when adding a module to LIB_API_MODULES.
LIB_API_VERSION = "1.0"

# The lib/ modules a plugin fragment's hooks are permitted to import as the stable
# contract surface. The warner names this list when a fragment's declared MAJOR
# disagrees with LIB_API_VERSION's major, so an author knows exactly which surface
# the version covers. (Order is the documented surface order, not load order.)
LIB_API_MODULES = [
    "telemetry_logger",
    "block_logger",
    "hook_priorities",
    "meta_detection",
    "issue_reference",
    "orchestration_markers",
    "approval_dual_accept",
    "branch_config",
    "prod_merge_binding",
    "session_linking",
    "session_role",
    "workflow_utils",
]
