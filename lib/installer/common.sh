#!/usr/bin/env bash
# Core-tier installer — shared helpers (sourced by install.sh + uninstall.sh).
#
# Ported (files only, never history) from the private SolidPD/claude-toolkit at
# 1480f3a9a5e6 (lib/installer/common.sh) by the core-tier export (rp1-home#2627).
# Changes from the port source: the manifest header records the tree's `version`
# (read from VERSION), and a dry run never writes the manifest. The copy delivery
# (rp1-home#2717) adds copy_one, a per-entry `delivery` and fail-closed backups.
#
# Per-file model (NOT GNU Stow): every managed file is deployed individually into the
# target (~/.claude), either as a regular-file COPY (the default delivery) or as a
# SYMLINK into this tree (--symlink). Every deployed file is recorded in the manifest as
# a (target, source, sha256, delivery) entry so uninstall touches ONLY what the installer
# deployed and never user files. For a copy, sha256 is the hash of the bytes the installer
# wrote: it is how an unmodified copy (ours) is told apart from one the user edited
# (theirs). An entry can also carry `adopted: true`: a file that was already there with
# the same content. It is recorded with its own hash but stays the user's (manifest_append).
#
# shellcheck shell=bash

# ── Logging ──────────────────────────────────────────────────────────────────
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s%s%s\n' "$(_c 36)" "$*" "$(_c 0)"; }
ok()   { printf '%s✓ %s%s\n' "$(_c 32)" "$*" "$(_c 0)"; }
warn() { printf '%s⚠ %s%s\n' "$(_c 33)" "$*" "$(_c 0)" >&2; }
err()  { printf '%s✖ %s%s\n' "$(_c 31)" "$*" "$(_c 0)" >&2; }
die()  { err "$*"; exit 1; }

# ── Run counters (read by install.sh for its summary) ────────────────────────
DEPLOY_SKIPPED=0      # files left alone on purpose: conflicts kept, skipped or not backed up,
                      # directories, and the toolkit source itself
DEPLOY_FAILED=0       # files that could not be copied, re-moded or hashed: the run fails
LINKS_CONVERTED=0     # the installer's own symlinks replaced by copies
COPIES_CONVERTED=0    # the installer's own copies replaced by symlinks

# ── Target path ──────────────────────────────────────────────────────────────
# Print $1 as one canonical absolute path, so every spelling of the same target (a
# trailing or doubled slash, a relative path, `.` or `..`) records and looks up the same
# manifest entries. The nearest existing directory is canonicalized by `cd` + `pwd`
# (logical, so a symlinked ~/.claude keeps its name); the part that does not exist yet is
# re-appended with "." dropped and ".." resolved lexically. An empty value is refused
# (status 1): it would otherwise resolve to the current directory.
normalize_target() {
  local t="$1" rest="" part parts=()
  [ -n "$t" ] || return 1
  case "$t" in /*) ;; *) t="$PWD/$t" ;; esac
  while [ ! -d "$t" ]; do
    rest="/$(basename "$t")$rest"
    t="$(dirname "$t")"
  done
  t="$(cd "$t" && pwd)" || return 1
  IFS=/ read -r -a parts <<<"$rest"
  for part in ${parts[@]+"${parts[@]}"}; do
    case "$part" in
      ''|.) ;;
      ..)   t="$(dirname "$t")" ;;
      *)    t="${t%/}/$part" ;;
    esac
  done
  printf '%s' "$t"
}

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

# ── Hashing ──────────────────────────────────────────────────────────────────
# sha256sum (Linux) or shasum -a 256 (macOS). sha_tool_init dies when neither exists,
# because an empty hash would make every copy compare equal to every recorded one.
_SHA_TOOL=""
sha_tool_init() {
  if command -v sha256sum >/dev/null 2>&1; then _SHA_TOOL=sha256sum
  elif command -v shasum >/dev/null 2>&1; then _SHA_TOOL=shasum
  else die "sha256sum or shasum is required (the manifest records file hashes)"
  fi
}

# Print the sha256 of $1; return 1 (printing nothing) when it cannot be computed.
# The file is read on stdin, so a path holding a backslash or newline cannot change
# the output (GNU sha256sum escapes such names and prefixes the hash with "\").
_sha() {
  local h=""
  [ -r "$1" ] && [ ! -d "$1" ] || return 1
  case "$_SHA_TOOL" in
    sha256sum) h="$(sha256sum < "$1" 2>/dev/null | cut -d' ' -f1)" ;;
    shasum)    h="$(shasum -a 256 < "$1" 2>/dev/null | cut -d' ' -f1)" ;;
  esac
  [ -n "$h" ] || return 1
  printf '%s' "$h"
}

# ── Manifest (JSON via jq) ───────────────────────────────────────────────────
STATE_DIR=""        # <target>/.toolkit-state
MANIFEST=""         # <STATE_DIR>/manifest.json

# Set the state paths without creating anything (a dry run reads, never writes).
manifest_paths() {
  STATE_DIR="$1/.toolkit-state"
  MANIFEST="$STATE_DIR/manifest.json"
}

manifest_init() {
  manifest_paths "$1"
  mkdir -p "$STATE_DIR"
  if [ ! -f "$MANIFEST" ]; then
    printf '{"entries":[]}\n' > "$MANIFEST"
  fi
}

# The manifest decides what is the installer's and what is yours, so a malformed one
# stops install and uninstall before they change anything.
manifest_valid() {
  jq -e 'type == "object" and (.entries | type) == "array"
         and all(.entries[]; type == "object" and (.target | type) == "string")' "$MANIFEST" >/dev/null 2>&1
}

# Rewrite the manifest through jq. The temp file sits next to the manifest, so the
# final mv is a rename on one filesystem (never a cross-device copy from /tmp).
_manifest_write() {
  local tmp
  tmp="$(mktemp "$MANIFEST.XXXXXX")" || return 1
  if jq "$@" "$MANIFEST" > "$tmp"; then
    mv -f "$tmp" "$MANIFEST"
  else
    rm -f "$tmp"
    return 1
  fi
}

manifest_set_header() {
  # $1 toolkit_root  $2 target  $3 tiers-csv  $4 overlays-csv  $5 iso-timestamp
  # $6 version (the tree's VERSION first line — the tag a doctor reports)
  # $7 delivery (copy | symlink) of this run
  _manifest_write --arg tr "$1" --arg tg "$2" --arg ti "$3" --arg ov "$4" --arg ts "$5" \
    --arg v "$6" --arg d "$7" \
    '. + {toolkit_root:$tr, target:$tg, tiers:$ti, overlays:$ov, installed_at:$ts, version:$v, delivery:$d}'
}

# A manifest written under another spelling of this target (a v1.0.0 install recorded
# --target as given: a trailing or doubled slash, a relative path) holds entries such as
# ".../.claude//hooks/a.py" that the canonical lookups never match. The manifest sits
# under the canonical target, so its header `target` names this same directory: on load,
# each entry under that spelling is rewritten under $TARGET, and the last entry for a
# path wins (as in manifest_append). A dry run rewrites a temp copy, removed on exit, and
# leaves the manifest itself untouched.
manifest_load_canonical() {
  local old
  [ -f "$MANIFEST" ] || return 0
  old="$(jq -r '.target // empty' "$MANIFEST")" || return 1
  { [ -n "$old" ] && [ "$old" != "$TARGET" ]; } || return 0
  if [ "$DRY_RUN" = "1" ]; then
    _DRY_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/toolkit-manifest.XXXXXX")" || return 1
    trap 'rm -f "$_DRY_MANIFEST"' EXIT
    cp "$MANIFEST" "$_DRY_MANIFEST" || return 1
    MANIFEST="$_DRY_MANIFEST"
  fi
  _manifest_write --arg o "$old" --arg n "$TARGET" '
    .entries |= (map(.target as $t | ($t | ltrimstr($o)) as $r
                     | if ($t | startswith($o)) and $r != ""
                          and (($r | startswith("/")) or ($o | endswith("/")))
                       then .target = $n + "/" + ($r | until(startswith("/") | not; .[1:]))
                       else . end)
                 | reduce .[] as $e ([]; map(select(.target != $e.target)) + [$e]))
    | .target = $n'
}

# The tree's version: the first line of <toolkit-root>/VERSION ("X.Y.Z"), or "" when absent.
tree_version() {
  [ -f "$1/VERSION" ] || { printf ''; return 0; }
  head -n 1 "$1/VERSION" | tr -d '[:space:]'
}

manifest_append() {
  # $1 target  $2 source  $3 sha256  $4 delivery (copy | symlink)  $5 adopted (true | "")
  # — dedupe by target. An ADOPTED entry is a file that was already there, byte-identical,
  # before the install: it is recorded but stays the user's (its content is never updated
  # without the conflict rules and uninstall never removes it; copy_one does correct an
  # executable bit that differs from the source's, to mode 0755 or 0644).
  _manifest_write --arg t "$1" --arg s "$2" --arg h "$3" --arg d "${4:-symlink}" --arg a "${5:-}" \
    '.entries |= (map(select(.target != $t))
       + [{target:$t, source:$s, sha256:$h, delivery:$d} + (if $a == "true" then {adopted:true} else {} end)])'
}

manifest_targets() { jq -r '.entries[].target' "$MANIFEST" 2>/dev/null; }

# Print one field of the target's entry ("" when there is no entry or no such field).
# An entry written by a v1.0.0 installer has no `delivery`: callers read "" as symlink.
manifest_entry_field() {
  [ -f "$MANIFEST" ] || return 0
  jq -r --arg t "$1" --arg f "$2" \
    'first(.entries[] | select(.target == $t) | .[$f] // empty) // empty' "$MANIFEST" 2>/dev/null
}

manifest_source_for() { manifest_entry_field "$1" source; }

# ── Conflict handling (R6) ───────────────────────────────────────────────────
# A pre-existing file the installer did not write: --force replaces it (after a backup),
# --yes skips it, otherwise prompt keep / replace / skip / diff (a replace is backed up
# first; no tty → skip). Returns 0 to proceed with the replace, 1 to leave the file alone.
# Fail-closed: when the backup cannot be taken, the file is left unchanged (return 1).
# Every step is checked explicitly, because callers run this on the left of `||`, where
# bash ignores `set -e` inside the function.
_backup_conflict() {
  # $1 the file to back up  $2 the backup path
  if mkdir -p "$(dirname "$2")" && cp -a "$1" "$2"; then
    return 0
  fi
  warn "could not back up $1 — left unchanged"
  return 1
}

handle_conflict() {
  local dst="$1" src="$2" backup_root="$STATE_DIR/conflicts/$RUN_TS"
  local rel choice
  rel="${dst#"$TARGET"/}"
  if [ "$FORCE" = "1" ]; then
    _backup_conflict "$dst" "$backup_root/$rel" || return 1
    return 0
  fi
  if [ "$ASSUME_YES" = "1" ]; then
    warn "conflict at $dst (not installed by this toolkit) — skipping (non-interactive; use --force to replace)"
    return 1
  fi
  while true; do
    printf 'Conflict: %s already exists (not installed by this toolkit).\n' "$dst" >&2
    printf '  [k]eep local | [r]eplace with toolkit | [s]kip | [d]iff ? ' >&2
    read -r choice </dev/tty || { warn "no tty — skipping $dst"; return 1; }
    case "$choice" in
      k|keep) return 1 ;;
      s|skip) return 1 ;;
      r|replace)
        _backup_conflict "$dst" "$backup_root/$rel" || return 1
        ok "backed up local $dst -> $backup_root/$rel"; return 0 ;;
      d|diff) diff -u "$dst" "$src" >&2 || true ;;
      *) printf 'Please answer k, r, s, or d.\n' >&2 ;;
    esac
  done
}

# Is $1 a copy this installer wrote and nobody has changed since? (manifest-recorded as
# delivery copy, not adopted, still a regular file, and its sha256 equals the recorded one)
_is_own_unmodified_copy() {
  local dst="$1" rec cur
  [ -f "$dst" ] && [ ! -L "$dst" ] || return 1
  [ "$(manifest_entry_field "$dst" delivery)" = "copy" ] || return 1
  [ "$(manifest_entry_field "$dst" adopted)" != "true" ] || return 1
  rec="$(manifest_entry_field "$dst" sha256)"
  cur="$(_sha "$dst")" || return 1
  [ -n "$rec" ] && [ "$cur" = "$rec" ]
}

# ── Link one file (symlink delivery) ─────────────────────────────────────────
# $1 source-abs  $2 target-abs. Honors DRY_RUN. Records to manifest on success.
link_one() {
  local src="$1" dst="$2" cur
  if [ -L "$dst" ]; then
    cur="$(readlink "$dst")"
    if [ "$cur" = "$src" ]; then
      [ "$VERBOSE" = "1" ] && log "  = $dst (already linked)"
      # Keep the manifest authoritative — but a dry run has no manifest to write.
      [ "$DRY_RUN" = "1" ] || manifest_append "$dst" "$src" "$(_sha "$src")" symlink
      return 0
    fi
    # symlink pointing elsewhere — upgrade case (R7): update without prompting
    if [ "$DRY_RUN" = "1" ]; then log "  ~ $dst (would re-point $cur -> $src)"; return 0; fi
    ln -sfn "$src" "$dst"
    manifest_append "$dst" "$src" "$(_sha "$src")" symlink
    ok "re-pointed $dst"
    return 0
  fi
  if [ -d "$dst" ]; then
    warn "skip $dst — it is a directory (never replaced)"
    DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1))
    return 0
  fi
  if [ -e "$dst" ] && [ "$dst" -ef "$src" ]; then
    # A parent directory links into this tree, so the destination already IS the source.
    [ "$VERBOSE" = "1" ] && log "  = $dst (already in place through a linked parent directory)"
    return 0
  fi
  if [ -e "$dst" ]; then
    if _is_own_unmodified_copy "$dst"; then
      # this installer's own unmodified copy: convert it to a link without a prompt
      if [ "$DRY_RUN" = "1" ]; then
        log "  ~ $dst (would replace the copy with a symlink)"
        COPIES_CONVERTED=$((COPIES_CONVERTED + 1))
        return 0
      fi
      rm -f "$dst"
      COPIES_CONVERTED=$((COPIES_CONVERTED + 1))
    else
      # regular file at target — conflict (R6)
      if [ "$DRY_RUN" = "1" ]; then log "  ! $dst (conflict: existing file)"; return 0; fi
      handle_conflict "$dst" "$src" || { DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1)); return 0; }
      rm -f "$dst"
    fi
  fi
  if [ "$DRY_RUN" = "1" ]; then log "  + $dst -> $src"; return 0; fi
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"
  manifest_append "$dst" "$src" "$(_sha "$src")" symlink
  [ "$VERBOSE" = "1" ] && ok "linked $dst"
  return 0
}

# ── Copy one file (copy delivery, the default) ───────────────────────────────
# The deployed mode follows the source's executable bit only (0755 or 0644), never the
# raw bits of the tree, so a clone made under any umask installs the same modes.
_deploy_mode() { if [ -x "$1" ]; then printf '0755'; else printf '0644'; fi; }

# Write $1 over $2 atomically: a hidden temp file in the destination's directory, then
# chmod, then one rename. A hook that fires mid-install never reads a half-written file.
_atomic_copy() {
  local src="$1" dst="$2" tmp
  tmp="$(mktemp "$(dirname "$dst")/.$(basename "$dst").XXXXXX")" || return 1
  if ! cp "$src" "$tmp" || ! chmod "$(_deploy_mode "$src")" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  # mv onto a symlink to a directory would move the file INTO that directory: drop that
  # link first. A symlink to a file (or a dangling one) is replaced by the rename itself.
  if [ -L "$dst" ] && [ -d "$dst" ] && ! rm -f "$dst"; then
    rm -f "$tmp"
    return 1
  fi
  if ! mv -f "$tmp" "$dst"; then
    rm -f "$tmp"
    return 1
  fi
}

# Copy $1 to $2 and record it; on failure report it, count a failure and return 1. A
# failure is not a skip: install.sh stops with a non-zero exit before settings.json is merged.
_copy_into_place() {
  local src="$1" dst="$2" h
  if ! mkdir -p "$(dirname "$dst")" || ! _atomic_copy "$src" "$dst"; then
    err "could not copy $src to $dst — not installed"
    DEPLOY_FAILED=$((DEPLOY_FAILED + 1))
    return 1
  fi
  h="$(_sha "$dst")" || die "could not hash $dst after copying it"
  manifest_append "$dst" "$src" "$h" copy || die "could not record $dst in $MANIFEST"
}

# $1 source-abs  $2 target-abs. Honors DRY_RUN. Records to manifest on success.
copy_one() {
  local src="$1" dst="$2" cur rec_src h adopted
  if [ -L "$dst" ]; then
    cur="$(readlink "$dst")"
    rec_src="$(manifest_entry_field "$dst" source)"
    if [ "$cur" = "$src" ] || { [ -n "$rec_src" ] && [ "$cur" = "$rec_src" ]; }; then
      # this installer's own symlink (from a --symlink install): replace it, no prompt
      if [ "$DRY_RUN" = "1" ]; then
        log "  ~ $dst (would replace the symlink with a copy)"
        LINKS_CONVERTED=$((LINKS_CONVERTED + 1))
        return 0
      fi
      _copy_into_place "$src" "$dst" || return 0
      LINKS_CONVERTED=$((LINKS_CONVERTED + 1))
      [ "$VERBOSE" = "1" ] && ok "converted $dst to a copy"
      return 0
    fi
    # any other symlink, dangling or not, is one the installer did not create — conflict (R6)
    if [ "$DRY_RUN" = "1" ]; then log "  ! $dst (conflict: a symlink this toolkit did not create)"; return 0; fi
    handle_conflict "$dst" "$src" || { DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1)); return 0; }
    _copy_into_place "$src" "$dst" || return 0
    return 0
  fi
  if [ -d "$dst" ]; then
    warn "skip $dst — it is a directory (never replaced)"
    DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1))
    return 0
  fi
  if [ -e "$dst" ] && [ "$dst" -ef "$src" ]; then
    warn "skip $dst — it is the toolkit source itself (a parent directory links into the tree)"
    DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1))
    return 0
  fi
  if [ -f "$dst" ] && cmp -s "$dst" "$src"; then
    # same content already: nothing to write (R2); repair only a wrong executable bit
    if { [ -x "$dst" ] && [ ! -x "$src" ]; } || { [ ! -x "$dst" ] && [ -x "$src" ]; }; then
      if [ "$DRY_RUN" = "1" ]; then log "  ~ $dst (would set mode $(_deploy_mode "$src"))"; return 0; fi
      chmod "$(_deploy_mode "$src")" "$dst" || { err "could not set the mode of $dst"; DEPLOY_FAILED=$((DEPLOY_FAILED + 1)); return 0; }
    fi
    [ "$DRY_RUN" = "1" ] && return 0
    # Our own copy keeps its entry (and its adopted flag); a file that was already there,
    # unrecorded, is recorded as ADOPTED: it stays the user's file (see manifest_append).
    adopted=""
    if [ "$(manifest_entry_field "$dst" delivery)" = "copy" ]; then
      adopted="$(manifest_entry_field "$dst" adopted)"
      [ "$VERBOSE" = "1" ] && log "  = $dst (already current)"
    else
      adopted="true"
      log "  = $dst (already present with the same content — recorded as yours, never removed)"
    fi
    h="$(_sha "$dst")" || { err "could not hash $dst — left unrecorded"; DEPLOY_FAILED=$((DEPLOY_FAILED + 1)); return 0; }
    manifest_append "$dst" "$src" "$h" copy "$adopted"
    return 0
  fi
  if [ -e "$dst" ]; then
    if _is_own_unmodified_copy "$dst"; then
      # this installer's copy of an older source: update it (R3)
      if [ "$DRY_RUN" = "1" ]; then log "  ~ $dst (would update the copy)"; return 0; fi
      _copy_into_place "$src" "$dst" || return 0
      ok "updated $dst"
      return 0
    fi
    # a file the installer did not write, or a copy the user edited — conflict (R4)
    if [ "$DRY_RUN" = "1" ]; then log "  ! $dst (conflict: existing file)"; return 0; fi
    handle_conflict "$dst" "$src" || { DEPLOY_SKIPPED=$((DEPLOY_SKIPPED + 1)); return 0; }
    _copy_into_place "$src" "$dst" || return 0
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then log "  + $dst (copy of $src)"; return 0; fi
  _copy_into_place "$src" "$dst" || return 0
  [ "$VERBOSE" = "1" ] && ok "copied $dst"
  return 0
}
