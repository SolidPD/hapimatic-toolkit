#!/usr/bin/env bash
# Core-tier installer — deploys the core tier into ~/.claude via per-file symlinks.
#
#   ./install.sh --minimal                 install the core tier (the default)
#   ./install.sh --minimal --dry-run       show planned operations, change nothing
#
# Flags: --minimal | --dry-run | --force | --yes | --target DIR | --toolkit-root DIR
#        | --verbose | -h/--help
# (--full and --overlay are not available in this tree: the workflow tier and the
#  overlays are separate products. Both exit non-zero with their own message.)
#
# Idempotent: re-running reports no changes. A pre-existing non-symlink file at a
# target is never silently overwritten: you are prompted to keep / replace / skip /
# diff it (no terminal: skipped); --yes skips it and --force replaces it. A backup
# (under .toolkit-state/conflicts/) is taken only when a file is replaced.
# settings.json is MERGED, never replaced: only the per-event hook-dispatcher entries
# are owned by this tree. Uninstall with ./uninstall.sh.
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (install.sh) by the core-tier export (rp1-home#2627).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_fail() { printf '✖ %s\n' "$*" >&2; exit 2; }
usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ── Defaults ─────────────────────────────────────────────────────────────────
DRY_RUN=0
FORCE=0
ASSUME_YES=0
VERBOSE=0
TARGET="$HOME/.claude"
TOOLKIT_ROOT="$SCRIPT_DIR"
RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"

# Parsed BEFORE the helpers are sourced, so every refusal below prints its own
# message even when the tree is incomplete.
while [ $# -gt 0 ]; do
  case "$1" in
    --minimal) ;;
    --full)    _fail "--full is not available: this tree is the core tier only (the workflow tier ships as a separate plugin)." ;;
    --overlay) _fail "--overlay is not available: this tree is the core tier only (overlays ship separately)." ;;
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    --target)  shift; [ $# -gt 0 ] || _fail "--target needs a dir"; TARGET="$1" ;;
    --toolkit-root) shift; [ $# -gt 0 ] || _fail "--toolkit-root needs a dir"; TOOLKIT_ROOT="$1" ;;
    -h|--help) usage 0 ;;
    *) _fail "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[ -f "$SCRIPT_DIR/lib/installer/common.sh" ] || _fail "incomplete tree: lib/installer/common.sh is missing next to $0"
command -v jq >/dev/null 2>&1 || _fail "jq is required (it maintains the install manifest)"
command -v python3 >/dev/null 2>&1 || _fail "python3 is required (the hooks and the settings generator are Python)"

# shellcheck source=lib/installer/common.sh
. "$SCRIPT_DIR/lib/installer/common.sh"
export DRY_RUN FORCE ASSUME_YES VERBOSE TARGET RUN_TS

TIERS=("core")
VERSION_STR="$(tree_version "$TOOLKIT_ROOT")"

log "core-tier installer"
log "  toolkit-root : $TOOLKIT_ROOT"
log "  target       : $TARGET"
log "  version      : ${VERSION_STR:-unknown}"
log "  mode         : minimal  (tiers: ${TIERS[*]})"
[ "$DRY_RUN" = "1" ] && log "  DRY RUN — no changes will be made"

[ -d "$TOOLKIT_ROOT/core" ] || die "no core/ tier under $TOOLKIT_ROOT — is this a generated core-tier tree?"

# ── Init state + manifest ────────────────────────────────────────────────────
if [ "$DRY_RUN" != "1" ]; then
  manifest_init "$TARGET"
  manifest_set_header "$TOOLKIT_ROOT" "$TARGET" "$(IFS=,; echo "${TIERS[*]}")" "" "$RUN_TS" "$VERSION_STR"
  # First-install backup of a pre-existing settings.json.
  if [ -f "$TARGET/settings.json" ] && [ ! -L "$TARGET/settings.json" ] \
     && [ ! -f "$STATE_DIR/settings.json.pre-toolkit.bak" ]; then
    cp -a "$TARGET/settings.json" "$STATE_DIR/settings.json.pre-toolkit.bak"
    ok "backed up existing settings.json -> $STATE_DIR/settings.json.pre-toolkit.bak"
  fi
fi

# ── Link every file of the tier ──────────────────────────────────────────────
linked=0
install_dir() {
  local dir="$1" src dst
  [ -d "$dir" ] || { warn "nothing to install from $dir (empty/absent tier)"; return 0; }
  while IFS=$'\t' read -r src dst; do
    [ -z "$src" ] && continue
    link_one "$src" "$dst"
    linked=$((linked + 1))
  done < <(enumerate_tier "$dir" "$TARGET")
}

for tier in "${TIERS[@]}"; do
  log "→ tier: $tier"
  install_dir "$TOOLKIT_ROOT/$tier"
done

# ── settings.json (merged: only the hook-dispatcher entries are owned here) ───
GEN="$TOOLKIT_ROOT/settings/generate-settings.py"
if [ -f "$GEN" ]; then
  log "→ merging settings.json"
  gen_args=(--tier minimal --toolkit-root "$TOOLKIT_ROOT" --target "$TARGET" --output "$TARGET/settings.json")
  if [ -f "$TARGET/.toolkit-state/user-settings.partial.json" ]; then
    gen_args+=(--user-overrides "$TARGET/.toolkit-state/user-settings.partial.json")
  fi
  if [ "$DRY_RUN" = "1" ]; then
    log "  would run: python3 $GEN ${gen_args[*]}"
  else
    python3 "$GEN" "${gen_args[@]}" || die "settings.json generation failed (the previous file is unchanged)"
    ok "settings.json merged"
  fi
else
  warn "settings generator not found ($GEN) — skipping settings.json"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY RUN complete — $linked file(s) would be processed. No changes made."
else
  ok "install complete — $linked file(s) processed into $TARGET"
  log "  manifest: $MANIFEST"
  log "  uninstall with: $TOOLKIT_ROOT/uninstall.sh"
fi
