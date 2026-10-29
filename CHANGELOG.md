# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The first public release is 0.1.0. Nothing is available from PyPI or the AUR
until the `v0.1.0` tag runs the release workflow; at that point this section
becomes `## [0.1.0] - <date>`.

### Added

- `winbridge setup <installer.exe>`: zero-touch pipeline that creates a pinned
  Wine prefix, installs the WebView2 Evergreen runtime before the app, silently
  installs the installer, detects the main exe and registered URL schemes,
  writes a launcher, pins it to the Omarchy taskbar, and proves first paint.
  `--force` backs up the profile first; `--manual` falls back to the GUI
  installer; `--silent-args`, `--exe`, `--scheme`, `--runner`, and `--id`
  override detection.
- `winbridge run`: launches an installed app without disturbing a running
  instance. `--restart` is the only kill path; `--url` forwards a URL instead of
  restarting.
- `winbridge run --wait` and `--json`: wait for two visible frames and exit 0
  only on a paint the command observed itself (4 with `transparent`,
  `background`, `timeout`, or `already-running`), and stream `stage`/`result`
  NDJSON events for scripts. A default launch reports `no-wait`; a launch or
  backend error reports `launch`; every mode emits exactly one terminal result.
- `winbridge run --cursor=force`: experimental, opt-in X11/XWayland fallback for
  Wine bug 58922. A detached helper reads X11's own `_NET_WM_PID` values,
  selects the prefix's windows by PID, and applies the standard X11 arrow to
  them and their descendants. It is single-instance per app, exits when the
  prefix is empty, never holds the caller's pipe, and logs its activation to the
  per-app run log. The default (`--cursor=off`) is unchanged, and `verify` still
  reports "cursor not checked".
- `WINBRIDGE_WINEDEBUG`: explicit trace escape, used verbatim for `WINEDEBUG`.
  An inherited `WINEDEBUG` is still ignored and the default stays `-all`.
- `winbridge verify`: launches, waits for first paint, captures evidence frames,
  and reports PASS/FAIL. It prints "cursor not checked"; the cursor is not
  verified.
- `winbridge update <app>`: finds the newest installer in the recorded folder,
  reinstalls in place, preserves the profile, and re-verifies.
- `winbridge expose` / `unexpose`: make one host folder visible inside an app,
  and remove it without touching the host folder.
- `winbridge unbridge <app>`: removes the prefix, profile, launcher, pin, URL
  handler entry, and scheme registration. Idempotent.
- `winbridge doctor [app] --bundle`: prints every detected value and writes a
  redacted diagnostic bundle for bug reports.
- `winbridge list` and `winbridge inspect`: read-only views of app profiles.
  `list` shows id, name, runner, and verification state; `inspect` prints the
  full profile, including the recorded WebView2 and keep-Z-drive metadata.
- `winbridge open-url` plus the experimental `login-bridge install|status`:
  forwards a login callback to a running app.
- Runner store: checksum-pinned catalog entries with `runners list`,
  `runners install`, `runners import`, and `runners pin`. Wine's `Z:` host-root
  link is stripped unless `--keep-z-drive` is passed.
- `--webview2=latest` (Evergreen bootstrapper) or `--webview2=pinned`
  (checksummed standalone installer, also usable offline).
- Desktop adapters: Hyprland (launcher, pin, screenshot; release-checklist
  target, pending verification) and X11 (experimental:
  `wmctrl`/`xdotool`/`xprop`, ImageMagick capture).
- Packaging: PyPI trusted publishing from a `v*` tag and an AUR `winbridge`
  PKGBUILD built from the release sdist.
- Design spec covering intent, architecture, profile schema, command contracts,
  support matrix, and risks.
- Task-by-task v0.1 implementation plan.
- Repository docs: README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, SUPPORT,
  TROUBLESHOOTING, GLOSSARY, PUBLISHING, SANDBOX, release checklist, and
  issue/PR templates.
- Agent and editor hygiene: AGENTS.md, .editorconfig, .gitattributes,
  .python-version.

### Fixed

- App launches no longer inherit the caller's stdin/stdout/stderr: output
  appends to a private per-app run log
  (`~/.local/share/winbridge/logs/run-<app-id>.log`, mode `0600`), so a
  surviving app or bridge child cannot hold a terminal or script pipe open and
  make `run --restart` look hung. If the log cannot be opened, launch warns and
  discards output instead of failing.
- App and WebView2 installers are staged inside the prefix and invoked by guest
  path, so a stripped `Z:` no longer breaks installation.
- Per-user installs under `AppData/Local` and `AppData/Roaming` are detected;
  install success requires a genuinely changed executable.
- `winbridge update --force` handles same-build reinstalls; failed installer
  probes can no longer supply evidence for a later attempt.
- App detection prefers the installer's shortcut/GUI executable, excludes
  bundled sidecars, and trusts the recorded exe on update.
- Omarchy pins support the current `bar.layout.<section>` schema as well as the
  legacy `bar.<section>` layout.
- Verify rejects transparent/background-identical frames, keeps the pre-launch
  background as evidence, and reports `transparent`/`background` distinctly;
  setup retries transparency once.
- Verify matches reparented Wine windows and prefers the main window.
- Setup retries verification once after a cold-start timeout.
- Doctor reads Wine registry headers with timestamps.

### Notes

Known limitations:

- The default runner can render with an invisible cursor for WebView2 apps.
  WebView2 child processes set cursors the window's process cannot resolve, Wine
  never calls `XDefineCursor`, and upstream `cursoricon.c` is unchanged through
  Wine 11.15 (Wine bug 58922). This is a known limitation, not a WinBridge
  regression. WebView2 152+ can also hide the cursor while typing; the HKLM
  `--disable-features=HideCursorWhileTyping` policy may not change it and is not
  a default flag.
- Hyprland is the release-checklist target; the label stays "pending
  verification" until that manual run is recorded. X11 and other Arch desktops
  are experimental; other Wayland compositors are unsupported for `verify`.
- Login-callback forwarding is experimental.
- Wine-kind runners only; Proton/GE-Proton is not supported in v0.1.
- An app whose main executable uses a known sidecar name, or whose installer
  creates a shortcut to an unlisted helper, needs `--exe` at setup.

[Unreleased]: https://github.com/TYRF1/winbridge/commits/main
