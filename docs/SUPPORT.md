# Support

## What support means

- **Verified**: the release checklist has been executed on the primary target
  (Omarchy/Hyprland). A regression here blocks a release. Unit CI is green; the
  headless Wine integration workflow is manual while the synthetic fixture's
  WebView2 first-paint issue is being fixed, and it is not a PR gate yet.
- **Experimental**: expected to work, not covered by CI or the checklist. Bug
  reports welcome; fixes are best-effort.
- **Unsupported**: may work by accident. Do not file "it should work" issues.

## Matrix

| Platform | Status | Notes |
|---|---|---|
| Arch / Omarchy + Hyprland | **release-checklist target — pending verification** | Primary target; NVIDIA/XWayland is the checklist target |
| Arch + X11 | experimental | X11 adapter, best-effort verification |
| Other Arch desktops (GNOME/KDE) | experimental | launcher/run work; verify degrades |
| Debian/Fedora/Ubuntu via `uv tool` | experimental | WinBridge manages its own runners, not distro Wine |
| Other Wayland compositors | unsupported for `verify` | `run` may work; no screenshot/window adapter |
| Flatpak-packaged WinBridge | unsupported | host-native by design |

The "verified" label is not earned yet: the manual release checklist has not
been executed for v0.1.0. Until it is recorded on the primary target, the
Omarchy/Hyprland row is a release target only.

## Requirements

- Linux, Python 3.11+, `uv` for the portable install.
- Network access for the first runner (SHA-256 pinned), which is cached after
  the first verified install. The default checksummed WebView2 payload is also
  cached after the first verified download. `--webview2=latest` downloads
  Microsoft's current Evergreen bootstrapper, is **not** cached, and is
  experimental: in the current QA environment it only installs under system
  Wine, not the default `soda@11.0-10` runner. Use the default pinned payload
  unless you are testing that path.
- A desktop with a supported adapter for `verify`: Hyprland (`hyprctl`, `grim`)
  or X11 (`wmctrl`/`xdotool`/`xprop`, ImageMagick `import`).
- A runner with 32-bit (WoW64) support. A 64-bit-only runner leaves the
  prefix's `syswow64` empty, so WebView2/Tauri apps cannot install; setup
  detects that immediately after prefix creation and fails with the runner
  reference instead of waiting for the installer timeout. 64-bit-only runners
  are not supported for WebView2/Tauri apps.
- No root. WinBridge never elevates.

## Before filing an issue

1. Run `winbridge doctor <app> --bundle`.
2. Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
3. Reproduce with the generic fixture app if possible.
4. Include the bundle, the command, and the exact error.

Do not attach vendor names, installers, screenshots, or proprietary logs. Redact
paths and window titles.

## Known limitations

- `--webview2=latest` (Evergreen bootstrapper) is experimental and was only
  proven under system Wine in the current QA environment; the pinned catalog
  payload is the supported default.
- `verify` ignores windows smaller than 200x150 (or 30,000 px²) and prefers the
  largest main window, so helper/splash strips are not used as evidence. An app
  whose only real surface is smaller than that threshold fails verification.
- A runner-generic `WM_CLASS` (for example `steam_proton`) is not recorded as
  `StartupWMClass`; the launcher and pin match use the exe stem instead.
- Exe auto-detection uses exact installer-shortcut targets, PE subsystem, and a
  known sidecar/helper list. The list is not exhaustive. If setup picks an
  unlisted helper or cannot disambiguate several GUI candidates, rerun with
  `winbridge setup <installer.exe> --exe <guest-path>`. The app's own shortcut
  is the strongest automatic signal; a shortcut must target the candidate path
  exactly.
- An app whose main executable is named like a known sidecar (for example
  `node.exe` or `browser.exe`) needs `--exe` at setup and cannot be auto-updated;
  `update` has no `--exe`.
- First paint under Wine can take 1–4 minutes; setup retries a timed-out or
  `transparent` automatic verify once (an explicit `--verify` and standalone
  `verify` do not retry).
- `run --wait` uses the same desktop adapter and paint detection as `verify`.
  It exits 0 only after two visible frames, 4 for `transparent`, `background`,
  `timeout`, or `already-running`, and 3 when the desktop cannot be observed.
  `--timeout` defaults to 300 seconds; `--json` emits NDJSON.
- `run --cursor=force` is an experimental X11/XWayland arrow fallback; it
  requires `DISPLAY` and libX11, overrides context cursors with the stock arrow,
  and exits with the app prefix. Native Wayland is unsupported.
- `verify` requires a pre-launch background capture. A failed/missing/corrupt
  background or a window rect that cannot be cropped from it fails with
  `reason="background"` rather than passing. The crop assumes 1x output scale
  and insets 2 px; HiDPI, negative/multi-monitor geometry, larger
  borders/shadows, and animated backgrounds can still affect the diff. Inspect
  the evidence or use a static background when in doubt.
- The default runner (`soda@11.0-10`) can render WebView2 apps with an invisible
  cursor. This is a known Wine limitation, not a compositor bug: WebView2 child
  processes set cursors the window's process cannot resolve, Wine never calls
  `XDefineCursor`, and upstream `cursoricon.c` is unchanged through Wine 11.15
  ([Wine bug 58922](https://bugs.winehq.org/show_bug.cgi?id=58922)). No runner or
  compositor workaround is promised; `run --cursor=force` is an experimental
  X11/XWayland opt-in that applies the stock arrow to the app's windows, not a
  fix for the bug and not a substitute for a verified runner. Keep
  `soda@11.0-10` unless another runner is validated for paint. `verify` checks
  render, not cursor, and prints "cursor not checked"; cursor behavior is never a
  pass/fail signal. Details:
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-cursor-is-invisible).
- Login-callback forwarding is experimental. Device-code logins need no callback.
- Proton/GE-Proton runners are not supported in v0.1; only wine-kind runners.
- `unbridge` removes only what WinBridge created. It will not clean a hand-made
  prefix.
- The pin targets Omarchy's Quickshell bar. Other desktops get a launcher, not a
  pin.
