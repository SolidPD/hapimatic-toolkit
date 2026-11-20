#!/usr/bin/env python3
# HOOK_VERSION: 2.0.0
# HOOK_EVENT: PostToolUse
# HOOK_MATCHER: Edit|Write
# HOOK_GROUP: lint
"""
PostToolUse hook: Run linting after Edit/Write operations on source files.

Provides immediate lint feedback to Claude after file modifications, enabling
self-correction before code is committed.

BEHAVIOR:
---------
1. Triggers on Edit/Write tool calls (matched by settings.json "Edit|Write")
2. Filters by file extension (.py, .ts, .tsx, .js, .jsx, .mjs, .cjs)
3. Python: runs ruff on the written file
4. JS/TS: runs ESLint only for a project root the user has listed (see TRUST)
5. Feeds lint errors back to Claude via decision:"block" + reason JSON
6. Silent pass (exit 0, no output) when clean or nothing may run

LINTER SELECTION:
-----------------
- Python (.py): ruff check --no-fix --no-cache <file>
- JS/TS in a listed root: <root>/node_modules/.bin/eslint --config <matched file>
- Anything else: no ESLint run

TRUST (rp1-home#3344, 2.0.0):
-----------------------------
A project's ESLint config is code: ESLint loads `eslint.config.js` and its
siblings by running them, and a project's `node_modules/.bin/eslint` is a
program the project ships. So ESLint runs only for a root the user listed.
Before 2.0.0 every project with an ESLint config was linted; now none is until
its root is listed (default deny).

- The list is the union of two files, read on every JS/TS write:
  `~/.claude/lint-on-write-trusted-roots.txt` (the user's own list) and
  `lint-on-write-trusted-roots.txt` beside this hook (a seed an installer may
  ship; a public install ships none). Nothing comes from the project, the hook
  payload or the session's working directory.
- Format: one directory per line, absolute or starting with `~/`; blank lines
  and `#` lines are skipped. A line ending in `/*` lists the direct children of
  that directory, not the directory itself and not a grandchild. A listed
  directory that does not exist is skipped; any other unusable line is skipped
  and logged.
- Flat configs only, in ESLint's own order: eslint.config.js, .mjs, .cjs, .ts,
  .mts, .cts. The hook walks up from the written file (after resolving links)
  to the nearest directory holding one of them; ESLint runs only when that
  directory is exactly a listed root (or a direct child of a `/*` root), and
  only with `--config <the file the hook matched>`, so a nested config the hook
  did not match cannot load, however ESLint itself looks configs up. A listed
  root whose only config is a legacy `.eslintrc*` is not linted.
- The hook starts `<root>/node_modules/.bin/eslint` itself, with no package
  runner (no npx, no bun x) and no PATH lookup of `eslint`, so a lint run
  installs nothing and contacts no registry. A listed root without that file is
  not linted.
- Inside a listed root, a file under a `node_modules`, `UserFileUploads` or
  `GeneratedFiles` directory is not linted. Every other JS/TS file under a
  listed root is linted with that root's config, including one in a
  subdirectory that has no config of its own. A subdirectory with its own flat
  config is a root of its own: it is linted only when it is listed.

Saying so: every ESLint non-run with a reason appends one JSON line to
`~/.claude/logs/lint-on-write.jsonl` (rotated to `.1` past 1 MiB). When a listed
root was not linted (a legacy config, no local ESLint, a runner failure, a
timeout, an ESLint error with no output, an unlisted nested config below it,
no config found within MAX_WALK_DEPTH of the file, or a nearest config that sits
above the root in an unlisted directory), the session also gets one
note per session for that root and reason, kept in
`~/.claude/state/lint-on-write-notes.json`; the note names only the listed root.
An unlisted root is only logged: it never puts text in front of the model.

Ruff reads the project's TOML config and runs no plugin code. It runs with
`--no-cache`, so a project's `cache-dir` setting cannot direct a write outside
the project.

Residuals (documented, not closed): an outside contributor's pull request
checked out inside a listed root runs its own config and its own ESLint binary,
as installing or testing that pull request would; any checkout placed under a
root listed with `/*` is trusted; a session that edits either list widens trust;
a write inside a listed root is code execution: writing that root's
`eslint.config.*`, or a module it imports, runs the new code on the same event;
and a plugin or parser in a listed root's config that loads its own config file
relative to the linted file (for example eslint-plugin-prettier with a Prettier
config, or @babel/eslint-parser with a Babel config) can run such a file from
any subdirectory of the root, including an extracted archive, so list only roots
whose config you have read.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LINT_TIMEOUT = 10  # seconds

# File extensions that trigger linting
PYTHON_EXTENSIONS = {".py"}
JS_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
ALL_LINT_EXTENSIONS = PYTHON_EXTENSIONS | JS_TS_EXTENSIONS

# Flat ESLint config names, in ESLint's own search order (the first one present wins).
FLAT_CONFIG_NAMES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
)

# Legacy config names: never a match; a listed root holding one of them and no flat config gets a note.
LEGACY_CONFIG_NAMES = (
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)

TRUSTED_ROOTS_NAME = "lint-on-write-trusted-roots.txt"
LOG_NAME = "lint-on-write.jsonl"
NOTES_NAME = "lint-on-write-notes.json"

# Directory names inside a listed root whose files are never linted.
EXCLUDED_COMPONENTS = frozenset({"node_modules", "UserFileUploads", "GeneratedFiles"})

# Reasons that also give the session a note (a listed root that did not lint).
NOTE_REASONS = frozenset({
    "legacy-eslintrc", "no-local-eslint", "runner-failed", "timeout", "eslint-error",
    "nested-config", "config-not-found",
})

LOG_MAX_BYTES = 1024 * 1024
NOTE_SESSIONS_KEPT = 200

# Max directory levels to walk up when searching for a config
MAX_WALK_DEPTH = 10


def _is_file(path: Path) -> bool:
    """`Path.is_file()` that answers False instead of raising (e.g. on EACCES)."""
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def trusted_roots_files() -> tuple[Path, Path]:
    """The two list files: the user's own, then the seed beside this hook (read at call time)."""
    return (
        Path.home() / ".claude" / TRUSTED_ROOTS_NAME,
        Path(__file__).resolve().parent / TRUSTED_ROOTS_NAME,
    )


def load_trusted_roots(files=None) -> tuple[list[tuple[Path, bool]], list[tuple[str, str]]]:
    """Read the listed roots from the list files. Never raises.

    Returns:
        (roots, skips) — roots is a list of (resolved directory, child_flag) pairs without
        duplicates; skips is a list of (reason, list file) for the caller to log.
    """
    roots: list[tuple[Path, bool]] = []
    skips: list[tuple[str, str]] = []
    try:
        if files is None:
            files = trusted_roots_files()
        for list_file in files:
            try:
                text = Path(list_file).read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError, ValueError):
                skips.append(("trust-file-unreadable", str(list_file)))
                continue
            for raw in text.splitlines():
                entry = raw.strip()
                if not entry or entry.startswith("#"):
                    continue
                if not (entry.startswith("/") or entry.startswith("~/")):
                    skips.append(("trust-entry-skipped", str(list_file)))
                    continue
                child = False
                if entry.endswith("/*"):
                    child = True
                    entry = entry[:-2]
                if entry in ("", "/"):
                    skips.append(("trust-entry-skipped", str(list_file)))
                    continue
                try:
                    root = Path(entry).expanduser().resolve(strict=True)
                except FileNotFoundError:
                    continue  # a listed directory this node does not hold: expected, silent
                except (OSError, RuntimeError, ValueError):
                    skips.append(("trust-entry-skipped", str(list_file)))
                    continue
                if root == Path(root.anchor):
                    skips.append(("trust-entry-skipped", str(list_file)))
                    continue
                if (root, child) not in roots:
                    roots.append((root, child))
    except Exception:
        pass
    return roots, skips


def is_listed(directory: Path, roots) -> bool:
    """True when `directory` is a listed root, or a direct child of a root listed with `/*`."""
    for root, child in roots:
        if child:
            if directory.parent == root and directory != root:
                return True
        elif directory == root:
            return True
    return False


def _walk_up(written_real: Path):
    """Yield the directories from the written file's parent upward, at most MAX_WALK_DEPTH."""
    current = written_real.parent
    for _ in range(MAX_WALK_DEPTH):
        yield current
        parent = current.parent
        if parent == current:
            break  # reached filesystem root
        current = parent


def find_flat_config(written_real: Path) -> tuple[Path, Path] | None:
    """The nearest directory holding a flat ESLint config, and the config file that matched."""
    for directory in _walk_up(written_real):
        for name in FLAT_CONFIG_NAMES:
            candidate = directory / name
            if _is_file(candidate):
                return directory, candidate
    return None


def find_listed_ancestor(path: Path, roots) -> Path | None:
    """The nearest listed directory strictly above `path`, however deep (no walk limit)."""
    for directory in path.parents:
        if is_listed(directory, roots):
            return directory
    return None


def find_listed_legacy_root(written_real: Path, roots) -> Path | None:
    """The first listed directory on the walk that holds a legacy `.eslintrc*` config and no flat config."""
    for directory in _walk_up(written_real):
        if (is_listed(directory, roots)
                and any(_is_file(directory / name) for name in LEGACY_CONFIG_NAMES)
                and not any(_is_file(directory / name) for name in FLAT_CONFIG_NAMES)):
            return directory
    return None


def local_eslint(config_dir: Path) -> Path | None:
    """The root's own `node_modules/.bin/eslint` when it is an executable file (not resolved)."""
    binary = config_dir / "node_modules" / ".bin" / "eslint"
    try:
        if _is_file(binary) and os.access(binary, os.X_OK):
            return binary
    except (OSError, ValueError):
        pass
    return None


def run_ruff(file_path: str) -> str | None:
    """Run ruff check on a Python file. Returns lint output or None if clean."""
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-fix", "--no-cache", file_path],
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT,
        )
        if result.returncode != 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass  # ruff not installed, not runnable, or timed out — silent pass
    return None


def run_eslint(binary: Path, config_file: Path, config_dir: Path, written_real: Path) -> tuple[str | None, str | None, dict]:
    """Run the listed root's own ESLint on one file, pinned to the matched config.

    Returns:
        (lint_output, reason, detail) — lint_output is the error report to feed back;
        reason names a non-run (runner-failed, timeout, eslint-error) or is None.
    """
    # Flat mode always: an inherited ESLINT_USE_FLAT_CONFIG=false would make ESLint 9 cascade
    # nested legacy .eslintrc* files past the --config pin.
    child_env = dict(os.environ)
    child_env["ESLINT_USE_FLAT_CONFIG"] = "true"
    try:
        proc = subprocess.Popen(
            [str(binary), "--config", str(config_file), "--no-error-on-unmatched-pattern", "--", str(written_real)],
            cwd=str(config_dir),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "runner-failed", {}
    try:
        stdout, stderr = proc.communicate(timeout=LINT_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Kill the whole process group, reap the leader, and close the pipes. A second
        # communicate() could wait on a descendant that left the group and holds a pipe.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
        return None, "timeout", {}
    if proc.returncode != 0:
        if (stdout or "").strip():
            return stdout.strip(), None, {}
        return None, "eslint-error", {"exit_code": proc.returncode, "stderr": (stderr or "")[:200]}
    return None, None, {}


def record_non_run(reason: str, session_id, config_dir, file, detail=None) -> None:
    """Append one JSON line for a non-run to ~/.claude/logs/lint-on-write.jsonl. Never raises."""
    try:
        log = Path.home() / ".claude" / "logs" / LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log.stat().st_size > LOG_MAX_BYTES:
                os.replace(log, log.with_name(LOG_NAME + ".1"))
        except FileNotFoundError:
            pass
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": session_id if isinstance(session_id, str) else None,
            "reason": reason,
            "config_dir": str(config_dir) if config_dir is not None else None,
            "file": str(file),
        }
        if reason == "eslint-error" and detail:
            row["exit_code"] = detail.get("exit_code")
            row["stderr"] = detail.get("stderr")
        if reason == "nested-config" and detail:
            row["nested_config_dir"] = detail.get("nested_config_dir")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def emit_note(session_id, root, reason: str) -> None:
    """Print one note per session per (root, reason) for a listed root that did not lint.

    The marker is one bounded file (the NOTE_SESSIONS_KEPT most recent sessions), replaced
    atomically. With no session id, or when the marker cannot be read or written, nothing
    is printed.
    """
    if reason not in NOTE_REASONS or not isinstance(session_id, str) or not session_id:
        return
    key = f"{root}|{reason}"
    marker = Path.home() / ".claude" / "state" / NOTES_NAME
    try:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (ValueError, UnicodeDecodeError):
            data = {}  # an unparsable marker counts as empty
        if not isinstance(data, dict):
            data = {}
        keys = data.get(session_id)
        if not isinstance(keys, list):
            keys = []
        if key in keys:
            return
        data.pop(session_id, None)
        data[session_id] = keys + [key]
        while len(data) > NOTE_SESSIONS_KEPT:
            data.pop(next(iter(data)))
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(marker.parent), prefix=".lint-on-write-notes.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, marker)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        record_non_run("note-unwritable", session_id, root, marker)  # the note is suppressed; say why
        return
    note = (
        f"lint-on-write: ESLint did not run for {root} ({reason}). "
        f"Details: ~/.claude/logs/{LOG_NAME}"
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))


def lint_js_ts(file_path: str, session_id) -> str | None:
    """The JS/TS branch: lint a listed root, or record why not. Returns lint output or None."""
    try:
        written_real = Path(file_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    roots, skips = load_trusted_roots()
    for reason, list_file in skips:
        record_non_run(reason, session_id, None, list_file)

    match = find_flat_config(written_real)
    if match is not None and is_listed(match[0], roots):
        config_dir, config_file = match
        try:
            parts = written_real.relative_to(config_dir).parts
        except ValueError:
            parts = ()
        if EXCLUDED_COMPONENTS.intersection(parts):
            record_non_run("excluded-path", session_id, config_dir, written_real)
            return None
        binary = local_eslint(config_dir)
        if binary is None:
            record_non_run("no-local-eslint", session_id, config_dir, written_real)
            emit_note(session_id, config_dir, "no-local-eslint")
            return None
        lint_output, reason, detail = run_eslint(binary, config_file, config_dir, written_real)
        if reason:
            record_non_run(reason, session_id, config_dir, written_real, detail)
            emit_note(session_id, config_dir, reason)
            return None
        return lint_output

    legacy_root = find_listed_legacy_root(written_real, roots)
    if legacy_root is not None:
        record_non_run("legacy-eslintrc", session_id, legacy_root, written_real)
        emit_note(session_id, legacy_root, "legacy-eslintrc")
        return None

    # A listed root above an unlisted nested config, or above a file whose walk found no config
    # (deeper than MAX_WALK_DEPTH, or a listed root with no flat config), or a listed root BELOW
    # the matched config dir (its nearest config sits above it, in an unlisted directory):
    # not linted, and the session is told, naming only the listed root.
    listed_below = None
    if match is not None:
        for directory in written_real.parents:
            if directory == match[0]:
                break
            if is_listed(directory, roots):
                listed_below = directory
                break
    listed_above = listed_below or find_listed_ancestor(match[0] if match is not None else written_real, roots)
    if listed_above is not None:
        try:
            parts = written_real.relative_to(listed_above).parts
        except ValueError:
            parts = ()
        if EXCLUDED_COMPONENTS.intersection(parts):
            record_non_run("excluded-path", session_id, listed_above, written_real)
            return None
        if match is not None and listed_below is None:
            record_non_run("nested-config", session_id, listed_above, written_real,
                           {"nested_config_dir": str(match[0])})
            emit_note(session_id, listed_above, "nested-config")
        else:
            record_non_run("config-not-found", session_id, listed_above, written_real)
            emit_note(session_id, listed_above, "config-not-found")
        return None

    if match is not None:
        record_non_run("unlisted-root", session_id, match[0], written_real)  # log only
    # No config found and no listed root above: nothing to do and nothing to record.
    return None


def main():
    # Telemetry: fire-and-forget usage event
    try:
        import os as _tos
        _tdir = _tos.path.dirname(_tos.path.abspath(__file__))
        import sys as _tsys
        if _tdir not in _tsys.path:
            _tsys.path.insert(0, _tdir)
        from lib.telemetry_logger import log_telemetry_event
        log_telemetry_event(__file__, "hook")
    except Exception:
        pass

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)
    if not isinstance(hook_input, dict):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id")

    # Only process Edit and Write
    if tool_name not in ("Edit", "Write") or not isinstance(tool_input, dict):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path or not isinstance(file_path, str):
        sys.exit(0)

    # Check file extension
    ext = Path(file_path).suffix.lower()
    if ext not in ALL_LINT_EXTENSIONS:
        sys.exit(0)

    # Run appropriate linter
    lint_output = None

    if ext in PYTHON_EXTENSIONS:
        lint_output = run_ruff(file_path)
    elif ext in JS_TS_EXTENSIONS:
        lint_output = lint_js_ts(file_path, session_id)

    # Report lint errors back to Claude
    if lint_output:
        result = {
            "decision": "block",
            "reason": f"Lint errors in {file_path}:\n{lint_output}",
        }
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
