#!/usr/bin/env bash
# Core-tier installer — shared helpers (sourced by install.sh + uninstall.sh).
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (lib/installer/common.sh) by the core-tier export (rp1-home#2627).
# Changes from the port source: the manifest header records the tree's `version`
# (read from VERSION), and a dry run never writes the manifest.
#
# Per-file symlink model (NOT GNU Stow): every managed file is symlinked
# individually into the target (~/.claude), and every (target, source, sha256)
# tuple is recorded in the manifest so uninstall touches ONLY toolkit-created
# links and never user files.
#
# shellcheck shell=bash

# ── Logging ──────────────────────────────────────────────────────────────────
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s%s%s\n' "$(_c 36)" "$*" "$(_c 0)"; }
ok()   { printf '%s✓ %s%s\n' "$(_c 32)" "$*" "$(_c 0)"; }
warn() { printf '%s⚠ %s%s\n' "$(_c 33)" "$*" "$(_c 0)" >&2; }
err()  { printf '%s✖ %s%s\n' "$(_c 31)" "$*" "$(_c 0)" >&2; }
die()  { err "$*"; exit 1; }

# ── Category → target-subdir mapping ─────────────────────────────────────────
# A tier is laid out as <tier>/<category>/<rest>. `guidance/` files land at the
# target root (~/.claude/FOO.md); every other category maps to a same-named
# subdirectory (~/.claude/hooks/…, ~/.claude/templates/…).
map_category() {
  case "$1" in
    guidance) printf '' ;;      # target root
    *)        printf '%s' "$1" ;;
  esac
}

# Emit "<source-abs>\t<target-abs>" for every file in a tier directory.
# $1 = tier dir (absolute), $2 = target root (absolute).
enumerate_tier() {
  local tier_dir="$1" target_root="$2" src category rest mapped dst
  [ -d "$tier_dir" ] || return 0
  # Skip VCS/cache noise defensively (tiers shouldn't contain them, but be safe).
  while IFS= read -r src; do
    rest="${src#"$tier_dir"/}"          # e.g. hooks/lib/telemetry.py
    category="${rest%%/*}"              # e.g. hooks
    mapped="$(map_category "$category")"
    if [ "$category" = "$rest" ]; then
      # file directly under the tier dir (no category) — land at target root
      dst="$target_root/$rest"
    elif [ -z "$mapped" ]; then
      dst="$target_root/${rest#"$category"/}"
    else
      dst="$target_root/$mapped/${rest#"$category"/}"
    fi
    printf '%s\t%s\n' "$src" "$dst"
  done < <(find "$tier_dir" -type f \
             -not -path '*/__pycache__/*' -not -name '*.pyc' | sort)
}

# ── Manifest (JSON via jq) ───────────────────────────────────────────────────
STATE_DIR=""        # set by install.sh: <target>/.toolkit-state
MANIFEST=""         # <STATE_DIR>/manifest.json

manifest_init() {
  STATE_DIR="$1/.toolkit-state"
  MANIFEST="$STATE_DIR/manifest.json"
  mkdir -p "$STATE_DIR"
  if [ ! -f "$MANIFEST" ]; then
    printf '{"entries":[]}\n' > "$MANIFEST"
  fi
}

manifest_set_header() {
  # $1 toolkit_root  $2 target  $3 tiers-csv  $4 overlays-csv  $5 iso-timestamp
  # $6 version (the tree's VERSION first line — the tag a doctor reports)
  local tmp; tmp="$(mktemp)"
  jq --arg tr "$1" --arg tg "$2" --arg ti "$3" --arg ov "$4" --arg ts "$5" --arg v "$6" \
    '. + {toolkit_root:$tr, target:$tg, tiers:$ti, overlays:$ov, installed_at:$ts, version:$v}' \
    "$MANIFEST" > "$tmp" && mv "$tmp" "$MANIFEST"
}

# The tree's version: the first line of <toolkit-root>/VERSION ("X.Y.Z"), or "" when absent.
tree_version() {
  [ -f "$1/VERSION" ] || { printf ''; return 0; }
  head -n 1 "$1/VERSION" | tr -d '[:space:]'
}

manifest_append() {
  # $1 target  $2 source  $3 sha256 — dedupe by target (upgrade rewrites).
  local tmp; tmp="$(mktemp)"
  jq --arg t "$1" --arg s "$2" --arg h "$3" \
    '.entries |= (map(select(.target != $t)) + [{target:$t, source:$s, sha256:$h}])' \
    "$MANIFEST" > "$tmp" && mv "$tmp" "$MANIFEST"
}

manifest_targets() { jq -r '.entries[].target' "$MANIFEST" 2>/dev/null; }
manifest_source_for() { jq -r --arg t "$1" '.entries[] | select(.target==$t) | .source' "$MANIFEST" 2>/dev/null; }

_sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

# ── Conflict handling (R6) ───────────────────────────────────────────────────
# A pre-existing NON-symlink at the target: --force replaces it (after a backup),
# --yes skips it, otherwise prompt keep / replace / skip / diff (a replace is backed
# up first; no tty → skip). Returns 0 to proceed with linking, 1 to skip this file.
handle_conflict() {
  local dst="$1" src="$2" backup_root="$STATE_DIR/conflicts/$RUN_TS"
  local rel choice
  if [ "$FORCE" = "1" ]; then
    mkdir -p "$backup_root"; rel="${dst#"$TARGET"/}"
    mkdir -p "$backup_root/$(dirname "$rel")"
    cp -a "$dst" "$backup_root/$rel"
    return 0
  fi
  if [ "$ASSUME_YES" = "1" ]; then
    warn "conflict at $dst (regular file) — skipping (non-interactive; use --force to replace)"
    return 1
  fi
  while true; do
    printf 'Conflict: %s already exists (not a toolkit symlink).\n' "$dst" >&2
    printf '  [k]eep local | [r]eplace with toolkit | [s]kip | [d]iff ? ' >&2
    read -r choice </dev/tty || { warn "no tty — skipping $dst"; return 1; }
    case "$choice" in
      k|keep) return 1 ;;
      s|skip) return 1 ;;
      r|replace)
        mkdir -p "$backup_root"; rel="${dst#"$TARGET"/}"
        mkdir -p "$backup_root/$(dirname "$rel")"; cp -a "$dst" "$backup_root/$rel"
        ok "backed up local $dst -> $backup_root/$rel"; return 0 ;;
      d|diff) diff -u "$dst" "$src" >&2 || true ;;
      *) printf 'Please answer k, r, s, or d.\n' >&2 ;;
    esac
  done
}

# ── Link one file (the 4 cases) ──────────────────────────────────────────────
# $1 source-abs  $2 target-abs. Honors DRY_RUN. Records to manifest on success.
link_one() {
  local src="$1" dst="$2" cur
  if [ -L "$dst" ]; then
    cur="$(readlink "$dst")"
    if [ "$cur" = "$src" ]; then
      [ "$VERBOSE" = "1" ] && log "  = $dst (already linked)"
      # Keep the manifest authoritative — but a dry run has no manifest to write.
      [ "$DRY_RUN" = "1" ] || manifest_append "$dst" "$src" "$(_sha "$src")"
      return 0
    fi
    # symlink pointing elsewhere — upgrade case (R7): update without prompting
    if [ "$DRY_RUN" = "1" ]; then log "  ~ $dst (would re-point $cur -> $src)"; return 0; fi
    ln -sfn "$src" "$dst"
    manifest_append "$dst" "$src" "$(_sha "$src")"
    ok "re-pointed $dst"
    return 0
  fi
  if [ -e "$dst" ]; then
    # regular file / dir at target — conflict (R6)
    if [ "$DRY_RUN" = "1" ]; then log "  ! $dst (conflict: existing file)"; return 0; fi
    handle_conflict "$dst" "$src" || return 0
    rm -f "$dst"
  fi
  if [ "$DRY_RUN" = "1" ]; then log "  + $dst -> $src"; return 0; fi
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"
  manifest_append "$dst" "$src" "$(_sha "$src")"
  [ "$VERBOSE" = "1" ] && ok "linked $dst"
  return 0
}
