# Claude Code core hooks

The core tier of a [Claude Code](https://claude.com/claude-code) hook system: one hook
dispatcher, a plugin loader, and a small set of general-purpose hooks, installed into
`~/.claude/` with per-file symlinks.

## What is in it

- **The hook dispatcher.** `settings.json` registers one command per hook event
  (`hook-dispatcher.py <event>`). The dispatcher reads `hooks/lib/hook_registry.py` and
  runs every registered hook for that event in one Python process, in order.
- **The prompt dispatcher.** `UserPromptSubmit` runs `unified-prompt-dispatcher.py`, which
  runs the prompt detectors listed in `hooks/lib/detector_registry.py`: a priority group
  (the first match wins) and an independent group (every match fires).
- **The plugin loader.** `hooks/lib/plugin_fragments.py` finds enabled Claude Code plugins
  that ship a `hooks/registry-fragment.py` and merges their hooks and detectors after the
  core ones. The core always wins: a plugin can add, never replace. A SessionStart hook
  reports any plugin whose fragment targets a different library version.
- **The registration validator.** `hooks/lib/hook_registration_validator.py` checks that
  every registered hook exists and every hook on disk is registered.
- **General-purpose hooks.** A stall logger and a retry-loop warning for failing tools, an
  idle-gap warning, and lint-on-write (`ruff` for Python, ESLint for JavaScript and
  TypeScript, each only when it is installed).
- **Routing stubs.** Some detectors in the priority group are placeholders. Each keeps its
  name and its place in the order, and never matches unless the plugin that provides it is
  installed.
- **Guidance.** A few general documents (engineering principles, MCP server notes), placed
  at the root of `~/.claude/`.

## What is not in it

The issue workflow, the multi-session orchestration and the hardware tooling are separate,
paid plugins, installed through Claude Code's plugin system. This repository contains none
of their code. Without them, the routing stubs never match and nothing else changes.

## Install

Requirements: `bash`, `git`, `python3` (3.10 or newer) and `jq`.

```bash
git clone <this repository> ~/claude-core
cd ~/claude-core
./install.sh --minimal --dry-run   # show what would change
./install.sh --minimal             # install
```

`install.sh` links each file into `~/.claude/` and records it in
`~/.claude/.toolkit-state/manifest.json`. A file that already exists there and is not one
of these links is never silently overwritten: you are asked whether to keep, replace, skip
or diff it (with no terminal, it is skipped). `--yes` skips such files without asking and
`--force` replaces them. A file is backed up, under `~/.claude/.toolkit-state/conflicts/`,
only when it is replaced. `--target DIR` installs somewhere other than `~/.claude`.

`settings.json` is merged, not replaced: the installer adds one `hook-dispatcher.py` entry
per hook event and fills a few defaults (MCP and Bash timeouts, a short list of read-only
commands Claude may run without asking) only where your file does not set them. Everything
else in your `settings.json` is kept. To keep your own additions across re-installs, put
them in `~/.claude/.toolkit-state/user-settings.partial.json`; they are merged last.

`--full` and `--overlay` are not available in this repository and exit with an error.

## Update

```bash
cd ~/claude-core && git pull
```

The installed files are symlinks, so a pull updates them in place. Run
`./install.sh --minimal` again only if a release adds or removes files.

## Uninstall

```bash
cd ~/claude-core && ./uninstall.sh
```

The uninstaller removes only the links it recorded, and only while they still point here.
A file you replaced or re-pointed is reported and kept. It also removes the
`hook-dispatcher.py` entries from `settings.json`; the timeout and permission defaults the
installer filled in stay, since they may now be yours. A backup of the `settings.json` you
had before the first install is kept in `~/.claude/.toolkit-state/`.

## Versions

Each release is a tag `vX.Y.Z`. The `VERSION` file holds `X.Y.Z`, and `CHANGELOG.md`
records the source revision each release was generated from. The repository is generated:
every file is produced by an export pipeline, so changes are not made here directly.

## License

MIT — see [LICENSE](LICENSE).
