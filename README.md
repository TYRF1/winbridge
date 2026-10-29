# WinBridge

One command to run a Windows Tauri/WebView2 desktop build on Linux — without the
Bottles yak-shave.

> **Status: v0.1.0 (release pending).** Host-native Wine only, for
> Tauri/WebView2 builds. The support matrix below is a release-checklist target;
> the manual checklist has not been executed yet, so no platform is labeled
> verified. Check the [support matrix](docs/SUPPORT.md) and
> [known limitations](docs/SUPPORT.md#known-limitations) before filing an issue.
> Background: [Design](docs/design.md) and [Plan](docs/plan-v0.1.md).

## The problem

Testing pre-release Windows builds of Tauri 2.x apps on Linux today means a day
of yak-shaving before the app even opens: bottle setup, WebView2 runtime hunting,
GPU flag folklore, login-callback breakage, file pickers that cannot see your
repos, pinned launchers that silently fail, and a reinstall ritual on every
release. Proton solves this for games. Bottles solves it generically. Nobody
solves it for "I test Windows desktop builds on my Linux machine."

## What it does (v0.1)

- `winbridge setup <installer.exe>` — create a pinned Wine prefix, install the
  checksum-pinned WebView2 runtime (latest Evergreen is opt-in and
  experimental), silently install the app, auto-detect the exe + registered URL
  scheme, write a launcher, pin it to the Omarchy taskbar, and prove the app
  renders. No clicks, no required config.
- `winbridge run <app>` — launch an installed app without disturbing a running
  instance. `--restart` is the only kill path; `--wait` blocks until two visible
  frames prove paint; `--json` streams NDJSON events.
- `winbridge update <app> [--force]` — find the newest installer in the
  recorded folder, silent-reinstall in place, verify. The default requires a
  changed executable; `--force` accepts a same-build reinstall only when the
  recorded exe still exists.
- `winbridge expose <app> <folder>` — make a host folder visible inside the
  Windows app; `unexpose` reverses it.
- `winbridge verify <app>` — launch, wait for first paint, screenshot, PASS/FAIL,
  keep evidence. Kills the "is it blank or loading?" guessing.
- `winbridge unbridge <app>` — remove the prefix, profile (including its
  `--force` backup), launcher, extracted icon, pin, handler, and scheme
  registration. Shared state (runners, WebView2 cache, logs, evidence,
  diagnostic bundles, locks) and the omarchy-shell `shell.json.bak` are kept;
  there is no runtime/desktop residue.
- `winbridge doctor` — environment checks and a redacted bundle for bug reports.
- Login-callback bridging — experimental. The scheme handler is real; forwarding
  into a running Wine instance is a spike, not a promise.

Runners (Wine builds) are downloaded from upstream releases, checksum-pinned, and
pinned per app. The default is `soda@11.0-10`; changing it is explicit.

## Quickstart

v0.1.0 is not on PyPI yet; from a clone:

```bash
git clone https://github.com/TYRF1/winbridge.git
cd winbridge
uv sync
uv run winbridge setup ./app-setup.exe --yes
uv run winbridge run <app-id> --restart
uv run winbridge verify <app-id>
```

`setup` prints the app id and every detected value. Use the generated launcher
for normal use; `run` is the same safe launch path from the CLI.

## Running and waiting

```bash
winbridge run <app>                 # launch; never kills a live instance
winbridge run <app> --restart       # the only kill path
winbridge run <app> --wait          # exit 0 only after two visible frames
winbridge run <app> --wait --json   # NDJSON stage/result events
winbridge run <app> --cursor=force  # experimental X11/XWayland arrow fallback
```

`--wait` uses the same transparency/paint detection as `verify` and defaults to
300 seconds (`--timeout`). It exits 4 with `transparent`, `background`,
`timeout`, or `already-running`, and 3 when the desktop cannot be observed.
`--cursor=force` is documented below and in
[Troubleshooting](docs/TROUBLESHOOTING.md#the-cursor-is-invisible).

## Design principles

- **Zero-touch.** Setup asks nothing. Everything it detects is printed, recorded
  in a versioned profile, and overridable.
- **App-agnostic.** Any vendor's Tauri 2.x / WebView2 build, no per-app code.
- **Verified.** A smoke harness beats "looks fine to me"; screenshots are kept.
- **Open source from day one.** MIT, CI without any NDA app, no proprietary
  artifacts in the repo.
- **Honest support.** "Verified" means the release checklist was executed on the
  target; CI covers the generic Wine pipeline. Everything else is labeled
  experimental.

## Support matrix

| Platform | Status |
|---|---|
| Arch / Omarchy + Hyprland | **release-checklist target — pending verification** |
| Arch + X11 | experimental |
| Other Arch desktops (GNOME/KDE) | experimental; launcher/run work, verify degrades |
| Debian/Fedora/Ubuntu via `uv tool` | experimental |
| Other Wayland compositors | unsupported for `verify` |
| Flatpak-packaged WinBridge | unsupported by design |

The manual release checklist has **not** been executed for v0.1.0 yet, so the
Hyprland row is a target, not evidence. "Verified" is earned only when that run
is recorded on Omarchy/Hyprland. The unit CI is green; the headless Wine
integration workflow is manual while the fixture's WebView2 first-paint issue is
being fixed, and it is not a PR gate yet. Details:
[docs/SUPPORT.md](docs/SUPPORT.md).

## Known limitations (v0.1)

- The default runner (`soda@11.0-10`) can render WebView2 apps with an invisible
  cursor. This is a known Wine limitation ([bug
  58922](https://bugs.winehq.org/show_bug.cgi?id=58922)): child-process cursors
  never reach `XDefineCursor`, and no runner or compositor workaround is
  promised. `run --cursor=force` is an experimental X11/XWayland opt-in that
  applies the stock arrow to the app's windows; it is not a fix and not a
  substitute for a verified runner. Keep the default runner unless another
  runner is validated for paint. `verify` checks render, not cursor, and says
  so. Details:
  [Troubleshooting](docs/TROUBLESHOOTING.md#the-cursor-is-invisible).
- Login-callback forwarding into a running Wine app is experimental. Device-code
  sign-in needs no callback.
- Proton/GE-Proton runners are not supported in v0.1; only wine-kind runners.
- `verify` needs Hyprland or X11 tooling; other desktops get launch/setup only.
- `verify` ignores windows smaller than 200x150 (or 30,000 px²) and prefers the
  largest main window. An app whose only real surface is smaller than that will
  fail verification.
- A runner-generic `WM_CLASS` (for example `steam_proton`) is not recorded as
  `StartupWMClass`; the launcher and pin use the exe stem instead.
- Exe auto-detection uses the app's exact installer shortcut, PE subsystem, and
  a known sidecar/helper list. That list is not exhaustive. If setup picks an
  unlisted helper or cannot disambiguate several GUI candidates, rerun with
  `winbridge setup <installer.exe> --exe <guest-path>`; the app's own shortcut
  is the strongest automatic signal.
- An app whose main executable is named like a known sidecar (for example
  `node.exe` or `browser.exe`) needs `--exe` at setup. `update` has no `--exe`,
  so such an app cannot be auto-updated.
- `unbridge` removes only what WinBridge created; hand-made prefixes are yours.

## Non-goals

- Not a faster Wine. CPU translation is near-native; GPU presentation is upstream
  Wine/DXVK territory. This project wins on setup friction and workflow.
- Not a store, compatibility database, or ProtonDB competitor.
- Not a VM/FreeRDP bridge.
- Not a general Windows-app manager. Tauri/WebView2 is the supported class.
- No telemetry, accounts, or bundled proprietary binaries.

## Install

v0.1.0 is not published yet. From a clone:

```bash
uv sync
uv run winbridge --help
```

- **Arch/Omarchy:** `winbridge` from the AUR after the v0.1.0 AUR submission.
- **Any distro:** `uv tool install winbridge` once v0.1.0 is published to PyPI.
- The orchestrator is host-native. It is deliberately not a Flatpak: it needs to
  see Wine prefixes, installers, the desktop session, and the `flatpak` CLI.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/TYRF1/winbridge.git
cd winbridge
uv sync --dev
uv run pytest -q
uv run ruff check .
uv run mypy src
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the architecture, the backend/desktop
adapter seams, and the NDA rule.

## Documentation

- [Design spec](docs/design.md) — the source of truth for behavior.
- [Implementation plan](docs/plan-v0.1.md) — task-by-task build order.
- [Glossary](docs/GLOSSARY.md) — prefix, runner, scheme, pin, verify.
- [Support matrix](docs/SUPPORT.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Runners](docs/RUNNERS.md) — paint results and runner policy
- [Sandbox](docs/SANDBOX.md) — isolated command execution
- [Publishing](docs/PUBLISHING.md) — history sanitization and release setup
- [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Agent rules](AGENTS.md)

## NDA / legal hygiene

This repo is app-agnostic by rule: no third-party binaries, installers, names,
screenshots, or internals. Everything here must work for ANY Tauri/WebView2
Windows build. Do not attach vendor material to issues, PRs, or CI artifacts.

## License

MIT. See [LICENSE](LICENSE). `winbridge` is the package and state-path name for
v0.1.
