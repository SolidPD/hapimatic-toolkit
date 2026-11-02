#!/usr/bin/env bash
# Core-tier installer — deploys the core tier into ~/.claude as per-file copies (the
# default) or as per-file symlinks into this tree (--symlink).
#
#   ./install.sh --minimal                 install the core tier (the default)
#   ./install.sh --minimal --dry-run       show planned operations, change nothing
#   ./install.sh --minimal --symlink       link each file to this tree instead of copying it
#
# Flags: --minimal | --copy | --symlink | --dry-run | --force | --yes | --target DIR
#        | --toolkit-root DIR | --verbose | -h/--help
# (--full and --overlay are not available in this tree: the workflow tier and the
#  overlays are separate products. Both exit non-zero with their own message.)
#
# --copy (the default) writes each file as a regular file: mode 0755 when the source is
# executable, else 0644. The installed files keep working if this tree moves or is
# deleted; after `git pull`, re-run ./install.sh --minimal to update them. A copy you
# edited is treated as your file. --symlink links each file to this tree instead, so a
# pull updates it in place. Re-running with the other flag converts the files this
# installer wrote, without asking. The last of --copy / --symlink wins.
#
# Idempotent: re-running reports no changes. A pre-existing file this toolkit did not
# install is never silently overwritten: you are prompted to keep / replace / skip /
# diff it (no terminal: skipped); --yes skips it and --force replaces it. The one
# exception is --symlink, which re-points a symlink already at a target to this tree
# without asking. With --copy, a file that is already byte-identical is recorded but
# stays yours: its content is never updated without asking and uninstall never removes
# it (only an executable bit that differs from the source's is corrected, to mode 0755
# or 0644). A backup (under .toolkit-state/conflicts/) is taken only when a file is
# replaced, and a file that cannot be backed up is left unchanged (not a failure). A
# directory is never replaced. A file that cannot be written fails the run (non-zero
# exit) before settings.json is touched.
# settings.json is MERGED, never replaced: only the per-event hook-dispatcher entries
# are owned by this tree. Uninstall with ./uninstall.sh.
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (install.sh) by the core-tier export (rp1-home#2627); the copy delivery
# by rp1-home#2717.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_fail() { printf '✖ %s\n' "$*" >&2; exit 2; }
# The help is the header comment, from line 2 up to (not including) the "Ported" note.
usage() { awk 'NR == 1 { next } /^# Ported/ { exit } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }

# ── Defaults ─────────────────────────────────────────────────────────────────
DRY_RUN=0
FORCE=0
ASSUME_YES=0
VERBOSE=0
DELIVERY="copy"
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
    --copy)    DELIVERY="copy" ;;
    --symlink) DELIVERY="symlink" ;;
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
sha_tool_init
TARGET="$(normalize_target "$TARGET")" || _fail "cannot resolve the target directory"
export DRY_RUN FORCE ASSUME_YES VERBOSE TARGET RUN_TS DELIVERY

TIERS=("core")
VERSION_STR="$(tree_version "$TOOLKIT_ROOT")"

log "core-tier installer"
log "  toolkit-root : $TOOLKIT_ROOT"
log "  target       : $TARGET"
log "  version      : ${VERSION_STR:-unknown}"
log "  mode         : minimal  (tiers: ${TIERS[*]})"
log "  delivery     : $DELIVERY"
[ "$DRY_RUN" = "1" ] && log "  DRY RUN — no changes will be made"

[ -d "$TOOLKIT_ROOT/core" ] || die "no core/ tier under $TOOLKIT_ROOT — is this a generated core-tier tree?"

# ── Init state + manifest ────────────────────────────────────────────────────
# A dry run reads an existing manifest (to tell this installer's files from yours) but
# never creates or writes one.
manifest_paths "$TARGET"
if [ -f "$MANIFEST" ] && ! manifest_valid; then
  die "the manifest at $MANIFEST is unreadable (not a JSON object with an entries list of objects) — nothing was changed"
fi
manifest_load_canonical || die "could not rewrite the manifest at $MANIFEST for the target $TARGET — nothing was changed"
if [ "$DRY_RUN" != "1" ]; then
  manifest_init "$TARGET"
  manifest_set_header "$TOOLKIT_ROOT" "$TARGET" "$(IFS=,; echo "${TIERS[*]}")" "" "$RUN_TS" "$VERSION_STR" "$DELIVERY"
  # First-install backup of a pre-existing settings.json.
  if [ -f "$TARGET/settings.json" ] && [ ! -L "$TARGET/settings.json" ] \
     && [ ! -f "$STATE_DIR/settings.json.pre-toolkit.bak" ]; then
    cp -a "$TARGET/settings.json" "$STATE_DIR/settings.json.pre-toolkit.bak"
    ok "backed up existing settings.json -> $STATE_DIR/settings.json.pre-toolkit.bak"
  fi
fi

# ── Deploy every file of the tier ────────────────────────────────────────────
processed=0
install_dir() {
  local dir="$1" src dst
  [ -d "$dir" ] || { warn "nothing to install from $dir (empty/absent tier)"; return 0; }
  while IFS=$'\t' read -r src dst; do
    [ -z "$src" ] && continue
    case "$DELIVERY" in
      copy) copy_one "$src" "$dst" ;;
      *)    link_one "$src" "$dst" ;;
    esac
    processed=$((processed + 1))
  done < <(enumerate_tier "$dir" "$TARGET")
}

for tier in "${TIERS[@]}"; do
  log "→ tier: $tier"
  install_dir "$TOOLKIT_ROOT/$tier"
done

# A delivery switch converts the installer's own files without a prompt; say so once.
if [ "$LINKS_CONVERTED" -gt 0 ]; then
  if [ "$DRY_RUN" = "1" ]; then
    log "would convert $LINKS_CONVERTED symlink(s) from an earlier install to copies (re-run with --symlink to restore links)"
  else
    ok "converted $LINKS_CONVERTED symlink(s) from an earlier install to copies (re-run with --symlink to restore links)"
  fi
fi
if [ "$COPIES_CONVERTED" -gt 0 ]; then
  if [ "$DRY_RUN" = "1" ]; then
    log "would convert $COPIES_CONVERTED copy(ies) from an earlier install to symlinks (re-run with --copy to restore copies)"
  else
    ok "converted $COPIES_CONVERTED copy(ies) from an earlier install to symlinks (re-run with --copy to restore copies)"
  fi
fi

# A file that could not be written fails the run here, before settings.json would wire
# the dispatcher to hooks that are not all in place. The files already written stay
# recorded, so a re-run after the cause is fixed completes the install.
if [ "$DEPLOY_FAILED" -gt 0 ]; then
  die "install FAILED — $DEPLOY_FAILED file(s) could not be written into $TARGET (see above); settings.json was not merged. Fix the cause, then re-run the same command"
fi

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
  log "DRY RUN complete — $processed file(s) would be processed. No changes made."
else
  ok "install complete — $processed file(s) processed into $TARGET ($DELIVERY; $DEPLOY_SKIPPED left unchanged)"
  log "  manifest: $MANIFEST"
  log "  uninstall with: $TOOLKIT_ROOT/uninstall.sh"
fi
