#!/usr/bin/env bash
# Core-tier uninstaller — removes ONLY what install.sh created.
#
#   ./uninstall.sh                 remove the core tier from the default target
#   ./uninstall.sh --target DIR    operate on a non-default target
#   ./uninstall.sh --dry-run       show what would be removed
#
# Defense-in-depth: a recorded COPY is removed only while it is unchanged since install
# (its sha256 still equals the recorded one); a recorded SYMLINK is removed only while it
# still points at the recorded source. A copy you edited, a file that was already there
# before the install (adopted), and a file replaced or re-pointed since, is LOGGED AND
# SKIPPED — user state is never destroyed.
# Directories are pruned only when they held a removed file and are now empty.
# The hook-dispatcher entries install.sh merged into settings.json are removed;
# every other settings.json key is left as it is.
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (uninstall.sh) by the core-tier export (rp1-home#2627); copy removal
# by rp1-home#2717.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/lib/installer/common.sh" ] || { printf '✖ incomplete tree: lib/installer/common.sh is missing\n' >&2; exit 2; }
# shellcheck source=lib/installer/common.sh
. "$SCRIPT_DIR/lib/installer/common.sh"
command -v jq >/dev/null 2>&1 || die "jq is required (it reads the install manifest)"

DRY_RUN=0
TARGET="$HOME/.claude"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --target)  shift; [ $# -gt 0 ] || die "--target needs a dir"; TARGET="$1" ;;
    -h|--help) awk 'NR == 1 { next } /^# Ported/ { exit } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

sha_tool_init
TARGET="$(normalize_target "$TARGET")" || die "cannot resolve the target directory"
manifest_paths "$TARGET"
[ -f "$MANIFEST" ] || die "no manifest at $MANIFEST — nothing to uninstall (was the core tier installed to $TARGET?)"
manifest_valid \
  || die "the manifest at $MANIFEST is unreadable (not a JSON object with an entries list of objects) — nothing was removed"
manifest_load_canonical || die "could not rewrite the manifest at $MANIFEST for the target $TARGET — nothing was removed"

log "core-tier uninstaller (target: $TARGET)"
[ "$DRY_RUN" = "1" ] && log "  DRY RUN — no changes will be made"

removed=0 skipped=0
removed_parents=()

# Remove one recorded file (or, in a dry run, report it), with its stale bytecode.
remove_recorded() {
  local dst="$1"
  if [ "$DRY_RUN" = "1" ]; then log "  - would remove $dst"; else rm -f "$dst"; fi
  removed=$((removed + 1))
  removed_parents+=("$(dirname "$dst")")
  case "$dst" in
    *.py)  # bytecode Python wrote for the removed module is stale now
      if [ "$DRY_RUN" != "1" ]; then
        rm -f "$(dirname "$dst")/__pycache__/$(basename "$dst" .py)".*.pyc
      fi
      removed_parents+=("$(dirname "$dst")/__pycache__") ;;
  esac
}

while IFS= read -r dst; do
  [ -z "$dst" ] && continue
  delivery="$(manifest_entry_field "$dst" delivery)"
  if [ "$delivery" = "copy" ] && [ "$(manifest_entry_field "$dst" adopted)" = "true" ]; then
    # it was already there, with the same content, before the install: it stays yours
    if [ -e "$dst" ]; then
      log "  kept $dst (it was there before the install)"
      skipped=$((skipped + 1))
    fi
  elif [ "$delivery" = "copy" ]; then
    if [ -L "$dst" ]; then
      warn "skip $dst — replaced by a symlink (user state preserved)"
      skipped=$((skipped + 1))
    elif [ -f "$dst" ]; then
      rec="$(manifest_entry_field "$dst" sha256)"
      cur="$(_sha "$dst")" || cur=""
      if [ -n "$rec" ] && [ "$cur" = "$rec" ]; then
        remove_recorded "$dst"
      else
        warn "skip $dst — modified since install (user state preserved)"
        skipped=$((skipped + 1))
      fi
    elif [ -e "$dst" ]; then
      warn "skip $dst — replaced by something that is not a file (user state preserved)"
      skipped=$((skipped + 1))
    fi
  else
    # a symlink entry (an entry with no delivery was written by a symlink-only installer)
    src="$(manifest_entry_field "$dst" source)"
    if [ -L "$dst" ]; then
      cur="$(readlink "$dst")"
      if [ "$cur" = "$src" ]; then
        remove_recorded "$dst"
      else
        warn "skip $dst — symlink re-pointed to $cur (not managed by this tree anymore)"
        skipped=$((skipped + 1))
      fi
    elif [ -e "$dst" ]; then
      warn "skip $dst — replaced by a regular file (user state preserved)"
      skipped=$((skipped + 1))
    fi
  fi
  # (already-absent targets: nothing to do)
done < <(manifest_targets)

# Prune directories that held a removed file and are now empty, walking up toward
# (never including) the target root. A directory the tree never wrote into is left
# alone even when it is empty.
if [ "$DRY_RUN" != "1" ] && [ "${#removed_parents[@]}" -gt 0 ]; then
  while IFS= read -r d; do
    while [ -n "$d" ] && [ "$d" != "$TARGET" ] && [ "${d#"$TARGET"/}" != "$d" ]; do
      rmdir "$d" 2>/dev/null || break
      log "  pruned empty dir $d"
      d="$(dirname "$d")"
    done
  done < <(printf '%s\n' "${removed_parents[@]}" | sort -ru)
fi

# Remove the hook-dispatcher entries install.sh merged into settings.json.
GEN="$SCRIPT_DIR/settings/generate-settings.py"
if [ -f "$TARGET/settings.json" ] && [ -f "$GEN" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    log "  would remove the hook-dispatcher entries from $TARGET/settings.json"
  else
    python3 "$GEN" --remove --target "$TARGET" --output "$TARGET/settings.json" \
      || warn "could not remove the hook-dispatcher entries from $TARGET/settings.json — remove them by hand"
  fi
fi

# Record the uninstall in the manifest (audit trail) rather than deleting it.
if [ "$DRY_RUN" != "1" ]; then
  _manifest_write --arg ts "$(date -u +%Y%m%dT%H%M%SZ)" '. + {uninstalled_at:$ts} | .entries = []' \
    || warn "could not record the uninstall in $MANIFEST"
fi

ok "uninstall complete — $removed removed, $skipped skipped (preserved)."
[ "$skipped" -gt 0 ] && log "  Skipped entries were changed since the install or were yours before it; review them manually if desired."
log "  Conflict backups (if any) remain under $STATE_DIR/conflicts/ for manual restore."
