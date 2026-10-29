# AGENTS.md

Instructions for AI agents (and humans) working in this repository.

## Read first

1. [Design spec](docs/design.md) — the source of truth for behavior.
2. [Implementation plan](docs/plan-v0.1.md) — the build order and exact
   interfaces.
3. [Support matrix](docs/SUPPORT.md) and
   [Troubleshooting](docs/TROUBLESHOOTING.md).
4. [Sandbox](docs/SANDBOX.md) — how every project command is executed.
5. [Publishing](docs/PUBLISHING.md) before any public push — history sanitization
   is mandatory.

## Current status

The v0.1 implementation is complete, final-review clean, and merged to `main`.
The manual release checklist has **not** been executed, so no platform carries
the "verified" label yet. Do not start v0.2 work (Bottles backend, Proton
runners, GNOME/KDE adapters, fonts, GPU policy) before v0.1 ships.

## Hard rules — do not negotiate these

- **Sandbox only.** Run project commands (unit tests, lint, types, packaging)
  through `scripts/sandbox`. Never run them on the host desktop. The only
  host-side exceptions are: building the image (`bash scripts/sandbox-build`),
  git version control in the worktree (the linked git dir is outside the
  worktree-only mount), Wine/desktop integration tests
  (`WINBRIDGE_INTEGRATION=1`), and human manual GUI QA. All are covered in
  [docs/SANDBOX.md](docs/SANDBOX.md). The sandbox refuses the main checkout and
  mounts only the current worktree.
- **App-agnostic, always.** No vendor names, binaries, installers, screenshots,
  logs, profiles, or "just for app X" branches. This is an NDA requirement.
- **No `shell=True`.** Every subprocess gets an argument list. Never run as root
  or call `sudo`.
- **XDG state only.** `~/.config/winbridge/` and `~/.local/share/winbridge/`.
  Never write into the repo or CWD.
- **Profile schema 1, additive only.** Unknown keys must survive a rewrite; back
  up before migrating. Never silently drop a field.
- **Bare desktop ids.** Store `winbridge-<id>`, never the `.desktop` suffix.
  omarchy-shell appends it. Double backslashes in `Exec`.
- **WebView2 before the app, detection in both registry views.** Detect in the
  32- and 64-bit views, install once, and apply the `msedgewebview2.exe` win7
  override. Default to the checksum-pinned catalog payload. `--webview2=latest`
  (Evergreen bootstrapper) is experimental; it is only proven under system Wine
  in the current QA environment.
- **`run` never kills a running app.** Only `run --restart` kills;
  setup/verify/update kill because they own the prefix. A pin click must never
  destroy unsaved work.
- **Strip Wine's `Z:` host-root link.** `--keep-z-drive` is the only exception;
  `expose` is the only supported host link.
- **No plugin API.** Backends and desktop adapters are internal protocols. Do
  not add dynamic loading or third-party extensions.
- **Wine-kind only in v0.1.** Do not add Proton/GE-Proton execution paths.
- **Print every detected value.** Silent magic makes bug reports useless.
- **Cursor is not verified.** Say "cursor not checked"; the default runner's
  invisible-cursor behavior is a documented limitation.
- **MIT inbound.** Contributions are MIT.

## Commands

Everything runs in the sandbox (see [docs/SANDBOX.md](docs/SANDBOX.md)):

```bash
bash scripts/sandbox-build      # once, and after Containerfile changes
scripts/sandbox uv sync --dev
scripts/sandbox uv run pytest -q
scripts/sandbox uv run ruff check .
scripts/sandbox uv run mypy src
bash scripts/sandbox-selftest   # verifies the isolation guarantees
```

`scripts/sandbox` refuses the main checkout and exits 2 outside a worktree. It
mounts the current worktree at `/work` and nothing else; no host home, display,
GPU, or audio is reachable. Host-native `uv` runs are for humans only, never for
an agent.

Git metadata for a linked worktree lives in the main checkout, outside the
mount, so `git status`, `git diff`, and `git commit` run on the host in the
worktree. The sandbox is for executing code, not version control.

Integration tests are skipped unless `WINBRIDGE_INTEGRATION=1` is set and a Wine
prefix + supported desktop are available. Those run on the host, not in the
sandbox: the image has no Wine and no desktop session.

## Architecture map

```
cli.py → core services → backends (Wine) / desktops (Hyprland, X11)
```

- `core/` — state, profiles, runners, install, detection, launcher, pin, run,
  expose, verify, update, unbridge, doctor, URL bridge, and `context.py` (the
  composition root).
- `backends/` — `Backend` protocol + runner-bound `WineBackend`.
- `desktops/` — `Desktop` protocol + `detect_desktop()` + Hyprland/X11.
- `util/` — subprocess, downloads, registry parsing, slugs, logging.
- `data/catalog.toml` — checksum-pinned runners and WebView2 payload.

## How to implement

- Work one plan task at a time. Write the failing test first, then the minimum
  implementation, then run the targeted test, then the full suite.
- Keep changes inside the task's file list unless the plan is wrong. If the spec
  and plan conflict, the spec wins; stop and report the conflict.
- No new runtime dependencies without a written reason in the PR. No `shell=True`.
- Add a test for every behavior. Unit tests must not require Wine, a compositor,
  or network.
- Update docs when behavior, flags, the support matrix, or the profile schema
  changes.
- Commit with `feat:`, `fix:`, `docs:`, `test:`, `build:`, or `chore:`.
- If a product decision is genuinely ambiguous, ask. Do not invent behavior and
  document it later.

## Adding capabilities

- **Desktop adapter:** implement `Desktop`, declare capabilities, degrade when
  tools are missing, add recorded-output tests, update `docs/SUPPORT.md`.
- **Backend:** implement `Backend`, never mutate shared Bottles/Steam installs,
  add a fake backend test.
- **Runner/WebView2:** link upstream, pin SHA-256, update the catalog; never
  mirror binaries.

## Reporting bugs

Use `winbridge doctor <app> --bundle`. Never attach vendor material. Redact
paths, titles, tokens, and callback URLs.
