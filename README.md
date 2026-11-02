# Claude Code core hooks

The core tier of a [Claude Code](https://claude.com/claude-code) hook system: one hook
dispatcher, a plugin loader, and a small set of general-purpose hooks, installed into
`~/.claude/` as plain file copies (or, with `--symlink`, as links to this repository).

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

Requirements: `bash`, `git`, `python3` (3.10 or newer), `jq`, and `sha256sum` or
`shasum` (Linux and macOS ship one).

```bash
git clone <this repository> ~/claude-core
cd ~/claude-core
./install.sh --minimal --dry-run   # show what would change
./install.sh --minimal             # install
```

`install.sh` copies each file into `~/.claude/` and records it, with its hash, in
`~/.claude/.toolkit-state/manifest.json`. The copies are ordinary files (executable where
the source is), so the installed hooks keep working if you move or delete this clone.
`--symlink` links each file to this repository instead, so edits made here are live at
once; re-running with the other flag converts the files the installer wrote.

A file that already exists there and was not written by the installer is never silently
overwritten: you are asked whether to keep, replace, skip, or diff it (with no terminal, it
is skipped). The one exception is `--symlink`, which re-points a symlink already in place
to this repository without asking. A copy you edited after the install counts as your file
too. With copies, so does a file that was already there with the same content: it is
recorded, but its content is never updated without asking and the uninstaller never
removes it (only an executable bit that differs from the source's is corrected, to mode
0755 or 0644). `--yes` skips such files without asking and `--force` replaces them. A file
is backed up, under `~/.claude/.toolkit-state/conflicts/`, only when it is replaced, and a
file that cannot be backed up is left as it is (that does not fail the install). A
directory is never replaced. If a file cannot be written (a
read-only directory, a full disk), the install stops with an error before `settings.json`
is changed; fix the cause and run it again. `--target DIR` installs somewhere other than
`~/.claude`.

`settings.json` is merged, not replaced: the installer adds one `hook-dispatcher.py` entry
per hook event and fills a few defaults (MCP and Bash timeouts, a short list of read-only
commands Claude may run without asking) only where your file does not set them. Everything
else in your `settings.json` is kept. To keep your own additions across re-installs, put
them in `~/.claude/.toolkit-state/user-settings.partial.json`; they are merged last.

`--full` and `--overlay` are not available in this repository and exit with an error.

## Update

```bash
cd ~/claude-core && git pull && ./install.sh --minimal
```

The installed files are copies, so pull and then re-run the installer: it updates every
copy whose source changed, adds the files new in this release, and corrects an executable
bit that differs from the source's. A copy you edited is treated as your file, so you are
asked before it is replaced.

The earlier update step, `cd ~/claude-core && git pull` on its own, now updates only a
`--symlink` install, whose links follow this repository. For a copy install it leaves the
installed files as they were, so always run `./install.sh --minimal` after the pull.

## Uninstall

```bash
cd ~/claude-core && ./uninstall.sh
```

The uninstaller removes only the files it recorded: copies you have not edited, and links
that still point here. A file you edited, replaced, or re-pointed is reported and kept,
and so is a file that was already there, with the same content, before the install. It
also removes the `hook-dispatcher.py` entries from `settings.json`; the timeout and
permission defaults the installer filled in stay, since they may now be yours. A backup of the `settings.json` you
had before the first install is kept in `~/.claude/.toolkit-state/`.

## Versions

Each release is a tag `vX.Y.Z`. The `VERSION` file holds `X.Y.Z`, and `CHANGELOG.md`
records the source revision each release was generated from. The repository is generated:
every file is produced by an export pipeline, so changes are not made here directly.

## License

MIT — see [LICENSE](LICENSE).
