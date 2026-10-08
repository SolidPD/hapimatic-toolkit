#!/usr/bin/env python3
"""Core-tier settings generator — merges the core tier's hook registrations into
~/.claude/settings.json.

Ships in the generated core-tier tree as ``settings/generate-settings.py``; the tree's
``install.sh`` runs it at install time and ``uninstall.sh`` runs it with ``--remove``.
Source: rp1-home ``toolkit-export/gen-core-settings.py`` (rp1-home#2627).

The core tier registers its hooks the way rp1-home does (the dispatcher-only model):
``settings.json`` carries exactly ONE ``hook-dispatcher.py <event>`` command per event,
and the dispatcher reads ``hooks/lib/hook_registry.py`` for everything else. Plugins
add their hooks through registry fragments, never through ``settings.json``.

MERGE, never replace. A tenant's ``settings.json`` already carries state this tree does
not own — ``enabledPlugins`` (which the fragment loader reads to find installed
plugins), permissions, env, the tenant's own hooks. So:

  * the existing file is the base, and every key in it survives;
  * the dispatcher entries are the only thing this generator owns: for each event it
    replaces any ``hook-dispatcher.py`` command with the canonical one and leaves
    every other hook group alone (idempotent — a re-run changes nothing);
  * the baseline env and permissions below are DEFAULTS: a key the file already sets
    wins, and the permission allow-list only gains entries it lacks;
  * ``--user-overrides`` (``.toolkit-state/user-settings.partial.json``) is merged last
    and wins;
  * an existing file that is not a JSON object is a hard error — it is never
    overwritten.

``--remove`` strips the dispatcher commands (and any hook group or event left empty)
and keeps everything else.

Usage:
  generate-settings.py [--tier minimal] [--toolkit-root DIR] [--target DIR] --output PATH
                       [--user-overrides FILE]
  generate-settings.py --remove [--target DIR] --output PATH

Exit codes: 0 written / 2 usage or unreadable input (the output file is unchanged).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DISPATCHER = "hook-dispatcher.py"

# Event -> matcher, the shape rp1-home's own settings.json registers.
DISPATCH_EVENTS: dict[str, str] = {
    "PreToolUse": "*",
    "PostToolUse": "*",
    "PostToolUseFailure": "",
    "SessionStart": "",
    "Stop": "",
    "UserPromptSubmit": "",
}

# Universal, non-personal defaults (from the port source's env/permission partials):
# the stall/timeout guardrails the resilience hooks pair with, and a deliberately
# minimal read-only allow-list.
DEFAULTS: dict = {
    "env": {
        "MCP_TIMEOUT": "120000",
        "MCP_TOOL_TIMEOUT": "120000",
        "BASH_DEFAULT_TIMEOUT_MS": "120000",
        "BASH_MAX_TIMEOUT_MS": "600000",
    },
    "permissions": {
        "allow": [
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(ls:*)",
            "Bash(cat:*)",
            "Bash(rg:*)",
        ],
    },
}


def _home_subst(path: str) -> str:
    """Render an absolute path with a literal $HOME prefix when it is under the home
    directory (Claude Code expands it), keeping the file portable."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "$HOME" + path[len(home):]
    return path


def dispatcher_command(target: Path, event: str) -> str:
    return f"python3 {_home_subst(str(target / 'hooks' / DISPATCHER))} {event}"


def _is_dispatcher(command: str) -> bool:
    return any(os.path.basename(tok) == DISPATCHER for tok in command.split())


def merge_defaults(base, defaults):
    """Fill `defaults` UNDER `base`: dicts recurse, lists gain only missing items
    (base order first), and a scalar already in `base` wins."""
    if isinstance(base, dict) and isinstance(defaults, dict):
        out = dict(base)
        for k, v in defaults.items():
            out[k] = merge_defaults(out[k], v) if k in out else v
        return out
    if isinstance(base, list) and isinstance(defaults, list):
        return list(base) + [d for d in defaults if d not in base]
    return base


def merge_overrides(base, over):
    """Merge `over` ON TOP of `base` (the user's partial wins): dicts recurse, lists
    union with de-dup, scalars are replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge_overrides(out[k], v) if k in out else v
        return out
    if isinstance(base, list) and isinstance(over, list):
        return list(base) + [o for o in over if o not in base]
    return over


def strip_dispatcher(hooks: dict) -> dict:
    """Remove every hook-dispatcher.py command; drop groups and events left empty."""
    out: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"]
                    if not (isinstance(h, dict) and _is_dispatcher(str(h.get("command", ""))))]
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            out[event] = kept_groups
    return out


def add_dispatcher(hooks: dict, target: Path) -> dict:
    """Ensure exactly one canonical dispatcher command per event."""
    out = strip_dispatcher(hooks)
    for event, matcher in DISPATCH_EVENTS.items():
        entry = {"type": "command", "command": dispatcher_command(target, event)}
        out.setdefault(event, []).append({"matcher": matcher, "hooks": [entry]})
    return out


def load_object(path: Path) -> dict:
    """Read a JSON object; {} when the file is absent. Raises ValueError otherwise."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    data.pop("_comment", None)
    return data


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    real = path.resolve() if path.is_symlink() else path
    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="merge the core tier's hook registrations into settings.json")
    ap.add_argument("--tier", choices=("minimal",), default="minimal")
    ap.add_argument("--toolkit-root", type=Path, help="the tree root (accepted for installer compatibility)")
    ap.add_argument("--target", type=Path, default=Path.home() / ".claude")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--user-overrides", type=Path)
    ap.add_argument("--remove", action="store_true", help="remove the dispatcher entries instead")
    args = ap.parse_args(argv)

    try:
        settings = load_object(args.output)
        overrides = load_object(args.user_overrides) if args.user_overrides else {}
    except (OSError, ValueError) as exc:  # includes json.JSONDecodeError
        sys.stderr.write(f"generate-settings: refusing to touch settings ({exc}); the file is unchanged.\n")
        return 2

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        sys.stderr.write("generate-settings: settings.json `hooks` is not an object; the file is unchanged.\n")
        return 2

    if args.remove:
        stripped = strip_dispatcher(hooks)
        if stripped:
            settings["hooks"] = stripped
        else:
            settings.pop("hooks", None)
        write_atomic(args.output, settings)
        print(f"removed the hook-dispatcher entries from {args.output}")
        return 0

    settings["hooks"] = add_dispatcher(hooks, args.target)
    merged = merge_defaults(settings, DEFAULTS)
    if overrides:
        merged = merge_overrides(merged, overrides)
    if not isinstance(merged, dict):  # both merges keep a dict base a dict
        sys.stderr.write("generate-settings: merge produced a non-object; the file is unchanged.\n")
        return 2
    write_atomic(args.output, merged)
    print(f"merged the core tier's hook-dispatcher entries into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
