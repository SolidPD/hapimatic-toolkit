#!/usr/bin/env bash
# Core-tier uninstaller — removes ONLY what install.sh created.
#
#   ./uninstall.sh                 remove the core tier from the default target
#   ./uninstall.sh --target DIR    operate on a non-default target
#   ./uninstall.sh --dry-run       show what would be removed
#
# Defense-in-depth: a recorded target is removed ONLY if it is still a symlink
# pointing at the recorded source. A target replaced by a regular file or
# re-pointed elsewhere is LOGGED AND SKIPPED — user state is never destroyed.
# Directories are pruned only when they held a removed link and are now empty.
# The hook-dispatcher entries install.sh merged into settings.json are removed;
# every other settings.json key is left as it is.
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (uninstall.sh) by the core-tier export (rp1-home#2627).
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
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

STATE_DIR="$TARGET/.toolkit-state"
MANIFEST="$STATE_DIR/manifest.json"
[ -f "$MANIFEST" ] || die "no manifest at $MANIFEST — nothing to uninstall (was the core tier installed to $TARGET?)"

log "core-tier uninstaller (target: $TARGET)"
[ "$DRY_RUN" = "1" ] && log "  DRY RUN — no changes will be made"

removed=0 skipped=0
removed_parents=()
while IFS= read -r dst; do
  [ -z "$dst" ] && continue
  src="$(manifest_source_for "$dst")"
  if [ -L "$dst" ]; then
    cur="$(readlink "$dst")"
    if [ "$cur" = "$src" ]; then
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
    else
      warn "skip $dst — symlink re-pointed to $cur (not managed by this tree anymore)"
      skipped=$((skipped + 1))
    fi
  elif [ -e "$dst" ]; then
    warn "skip $dst — replaced by a regular file (user state preserved)"
    skipped=$((skipped + 1))
  fi
  # (already-absent targets: nothing to do)
done < <(manifest_targets)

# Prune directories that held a removed link and are now empty, walking up toward
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
  tmp="$(mktemp)"
  jq --arg ts "$(date -u +%Y%m%dT%H%M%SZ)" \
    '. + {uninstalled_at:$ts} | .entries = []' "$MANIFEST" > "$tmp" && mv "$tmp" "$MANIFEST"
fi

ok "uninstall complete — $removed removed, $skipped skipped (preserved)."
[ "$skipped" -gt 0 ] && log "  Skipped entries were user-modified; review them manually if desired."
log "  Conflict backups (if any) remain under $STATE_DIR/conflicts/ for manual restore."
