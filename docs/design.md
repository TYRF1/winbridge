# WinBridge — Design

Status: approved 2026-10-27
Date: 2026-10-27
Supersedes: nothing (replaces the intent of `docs/PLAN.md`)

## 1. Intent

WinBridge is a host-native Linux CLI that turns a Windows Tauri 2.x / WebView2
installer into a working, pinnable, testable app on Linux with one command.

The pain it removes: getting a current Windows build running under Wine today
costs hours of prefix setup, WebView2 hunting, runner roulette, launcher
escaping, file-access plumbing, and "is it blank or still loading?" guessing.
The vendor's Linux build lags; the Windows build is the only current one.

WinBridge is not a faster Wine. It wins on setup friction, repeatability, and
verification.

## 2. Goals

- `winbridge setup <installer.exe>` works with **zero required input** and ends
  with a launcher that starts a rendering app.
- The engine is **app-agnostic**: any vendor's Tauri 2.x / WebView2 installer,
  no per-app code in the repository.
- **Deterministic**: every auto-detected value (exe, scheme, runner, prefix,
  WebView2 version) is recorded in a versioned profile, printed, and
  overridable. The checksum-pinned catalog payload is the default; the latest
  Evergreen bootstrapper is an opt-in experimental path.
- **One-command updates** per release.
- **Verified**: a smoke harness proves a visible render and keeps evidence.
- **Open source from day one**: MIT, runnable CI without any NDA app, contributor
  path that never needs Windows, no proprietary artifacts in the repo.

## 3. Non-goals

- Not a Wine fork, DXVK replacement, or performance layer.
- Not a store, compatibility database, or ProtonDB competitor.
- Not a VM/FreeRDP bridge (WinApps, WinBoat, Cassowary territory).
- Not a general Windows-app manager: Tauri/WebView2 is the supported class.
  Other Win32 apps may work, but are not promised or tested.
- Not a Flatpak app. The orchestrator must see the host, Wine prefixes,
  installers, and the `flatpak` CLI.
- No telemetry, no accounts, no bundled proprietary binaries.

Anti-goals: becoming a pile of per-app hacks; silently mutating shared Bottles
or Steam installations; claiming distro/desktop support that CI does not cover.

## 4. Constraints

- Target: Arch/Omarchy, Hyprland, NVIDIA, XWayland; Python 3.11+.
- First paint under Wine can take 1–4 minutes; never kill early. Never stack
  launches. `setup`, `verify`, and `update` kill existing prefix processes
  first; `run` reuses a running instance, and only `run --restart` kills it.
- WebView2 is not bundled by Tauri NSIS installers. Install runtime **before**
  the app, silently. Default to the checksum-pinned catalog payload for
  reproducible/offline runs; `--webview2=latest` opts into Microsoft's
  Evergreen bootstrapper and is experimental.
- Cursor visibility is runner/app-specific. `verify` checks render, not cursor;
  the default runner's cursor behavior is a documented known limitation.
  `run --cursor=force` is an opt-in X11/XWayland arrow fallback, never a default
  and never a verify signal.
- `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` is dead on WebView2 ≥150 under Wine.
  Use the HKLM policy in both 32/64-bit registry views when flags are needed.
- Runners decide success: known-good pin per app, no implicit upgrades. Runner
  references use `id@version` (`soda@11.0-10`).
- Wine's default `Z:` maps the host root. `setup` removes it by default; only
  `expose` links cross the host boundary. `--keep-z-drive` is an explicit
  compatibility escape.
- The main launcher runs `winbridge run <id>`; scheme handling uses a separate
  `NoDisplay` desktop entry. Pins use bare ids and doubled backslashes in
  `Exec`. On Omarchy the pin target is the Quickshell bar (`omarchy-shell`),
  plugin `io.github.joeyvigil.taskbar`, in `~/.config/omarchy/shell.json`; the
  click path runs `gtk-launch <id>.desktop`, so stored ids never carry
  `.desktop`.
- NDA: no vendor names, binaries, installers, screenshots, logs, or app
  profiles in the repository or CI artifacts.
- Linux only. macOS/Windows hosts are out of scope.

## 5. Approaches considered

| Approach | Verdict | Why |
|---|---|---|
| Bottles-first wrapper | rejected | Bottles' CLI has hung; GUI-centric; makes the tool fail when Bottles is absent; duplicates the very dependency we want to remove. Kept as an optional v0.2 backend. |
| Raw `flatpak run` orchestration | rejected | Same coupling, more fragile than host-native Wine. |
| Host-native engine + internal backend seam | **chosen** | Removes the hard dependency; Bottles can be added later behind a small interface; matches the "fresh machine, three commands" promise. |
| Python + uv | **chosen** | Orchestration-heavy, no perf requirement, largest contributor pool, fastest iteration. Static binary deferred. |
| Rust / Go | rejected for v0.1 | 2–3× code/time, smaller drive-by contributor pool; distribution advantage does not outweigh iteration speed. |
| Dynamic plugin system | rejected | No users to justify it; security/support liability on a tool that runs untrusted installers. Internal seam only. |
| Depend on `winetricks webview2` | rejected as default | Core promise must be owned and debuggable; winetricks is LGPL and drifts. Recipe reimplemented; opt-in fallback only. |
| Managed runner store | **chosen** | Runner pinning is a proven requirement; relies on upstream release URLs + checksums, never a binary mirror. |
| Zero-touch + generated profile | **chosen** | Config is an output, not an input. Required config would kill first-run experience. |
| Flatpak-packaged orchestrator | rejected | Sandbox would fight prefixes, installers, protocol handlers, and the `flatpak` CLI. |

## 6. Architecture

### 6.1 Layers

```
CLI (Typer)
  └── composition root (Context)
        └── core services
        ├── state / profiles / locking
        ├── runner store
        ├── prefix manager ── Backend interface ── Wine v0.1 / Bottles v0.2
        ├── installer engine
        ├── detection (exe, scheme, version)
        ├── WebView2 installer
        ├── launcher / desktop entry / pin
        ├── verify (smoke harness) ── Desktop adapter ── Hyprland v0.1 / X11 v0.1 / others later
        ├── url bridge (experimental)
        └── doctor / logging
```

Both seams are **internal interfaces with dependency injection**, documented in
`CONTRIBUTING.md`. No runtime plugin loading, no third-party extension API
before three external backends exist.

### 6.2 State layout (XDG)

```
~/.config/winbridge/config.toml          global defaults
~/.local/share/winbridge/
  profiles/<app-id>.toml                 source of truth per app
  prefixes/<app-id>/                     Wine prefix
  runners/<runner>/<version>/            managed runner store
  cache/downloads/                       verified downloads
  evidence/<app-id>/<version>/<ts>/      screenshots + logs
  bundles/                               redacted doctor archives
  locks/                                 per-app and per-runner locks
  logs/run-<app-id>.log                  app launch output + cursor markers (0600)
$XDG_DATA_HOME/applications/winbridge-<app-id>.desktop
```

Evidence and logs are `0700`. WinBridge's command logs redact tokens, query
strings, and full URLs (exceptions: the scheme and host). The per-app run log is
raw app output and is not redacted; `doctor --bundle` redacts it on export.

### 6.3 Profile format (TOML, versioned)

```toml
schema = 1
id = "example-app"
name = "Example App"
backend = "wine"                # wine | bottles (v0.2)
runner = "soda@11.0-10"         # id@version, or "path:/abs/runner"
installer = { path = "...", dir = "...", glob = "*setup*.exe", type = "nsis",
              silent_args = ["/S"] }
exe = "C:\\users\\<host-user>\\AppData\\Local\\Programs\\Example\\Example.exe"
schemes = ["example-app"]
exposed = [{ host = "/home/me/repos", guest = "C:\\users\\<host-user>\\repos" }]
pin = { match = "^Example$" }    # optional; omarchy-shell focus regex
created_at = "2026-10-27T10:00:00Z"
last_verified = { app_version = "1.2.3", result = "pass",
                  first_paint_s = 95, at = "2026-10-27T10:04:00Z" }
```

Rules: additive changes only; unknown keys preserved on rewrite; `schema`
bumped only for breaking changes; a backup is written before any migration.
CLI flags override profile values; profile overrides global config.

### 6.4 Backend interface (internal)

```python
class Backend(Protocol):
    name: str
    def create_prefix(self, app_id: str, *, keep_z: bool = False) -> Path
    def wine(self, prefix: Path, *args: str) -> CompletedProcess
    def install(self, prefix: Path, installer: Path, args: list[str], *, log_path: Path | None = None) -> int
    def run(self, prefix: Path, exe: str, argv: list[str]) -> Popen
    def expose(self, prefix: Path, host: Path, guest: str) -> None
    def remove(self, prefix: Path) -> None
```

`WineBackend` is v0.1 and is **runner-bound**: it is constructed with a resolved
runner root and executes wine-kind runners directly (Kron4ek builds, Bottles'
soda) via `WINEPREFIX` + the runner's `bin/wine`. The prefix user is the host
user, so guest paths are `C:\users\<host-user>`. Proton-kind runners (GE-Proton)
are deferred to v0.2 because their execution semantics need the Steam runtime/umu
layer; catalog entries for them only appear once that is proven.
`BottlesBackend` (v0.2) wraps `flatpak` and `bottles-cli`; it must ask before
mutating a pre-existing bottle and must never modify shared runners.

Launch never inherits the caller's stdio: stdin is `/dev/null`, and stdout and
stderr append to `logs/run-<app-id>.log` (mode `0600`) with
`stderr=STDOUT`. A bridge or WebView2 child that outlives the launcher must not
be able to hold a caller's pipe open and make a finished `run` look hung. The
run log is best-effort: if it cannot be opened, the launch warns and discards
output instead of failing. The app started by `run`, and the opt-in
`--cursor=force` helper's `[cursor]` marker lines, write to this log;
setup/`wineboot` and URL-forwarding `wine start` output stay with their own
captured command output. A `+all` or cursor trace grows until the app exits, so
truncate or delete the run log afterward.

### 6.5 Desktop adapter interface (internal)

```python
class Desktop(Protocol):
    name: str
    capabilities: set[str]           # windows | screenshot | pin
    def windows(self) -> list[Window]
    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None
    def screenshot(self, window: Window, dest: Path) -> None
    def screenshot_screen(self, dest: Path) -> None   # mandatory background
    def pin(self, desktop_id: str, match: str) -> bool
    def unpin(self, desktop_id: str) -> bool
```

v0.1: `HyprlandDesktop` (`hyprctl clients -j`, `grim`) and `X11Desktop`
(`xdotool`/`wmctrl`/`xprop`, ImageMagick `import`). `screenshot_screen` captures
the whole output (`grim <dest>`, `import -window root <dest>`) and is required
for a supported verify: a failed, missing, corrupt, or geometrically
incompatible background fails with `reason="background"` rather than passing on
blank/alpha.
Detection order: Hyprland signature,
then session type. Missing tools degrade the adapter's capabilities; `verify`
refuses with a clear message, `run` still works. `HyprlandDesktop` reports `pin`
only when `~/.config/omarchy/shell.json` exists **and** a widget with id
`io.github.joeyvigil.taskbar` is present; `X11Desktop` never reports `pin`. No
dock detection: Omarchy pinning is omarchy-shell-only, as verified live.

Unsupported desktop behavior: setup/run/update/expose/unbridge work. Standalone
`verify` prints "desktop not supported" and exits 3. During `setup` the automatic
verify is skipped with a warning and setup exits 0; `setup --verify` exits 3.

### 6.6 Runner store

- Catalog ships inside the package (`winbridge/data/catalog.toml`), replacing
  live catalog fetching in v0.1. Updating the catalog means updating WinBridge;
  no second trust channel, and therefore no catalog signing (the package
  signature is the trust channel).
- Entry: id, version, kind (`wine` | `soda` in v0.1; `proton` in v0.2), URL,
  SHA-256, archive root, relative wine path, `default` flag, notes.
- The catalog marks one **default runner** (`soda@11.0-10` initially). Zero-input
  `setup` uses it; `--runner` and per-app profiles override. A failing default
  is not silently retried with another runner (each attempt costs minutes and
  hides the real failure). The default's invisible-cursor behavior is documented
  in the README/SUPPORT known limitations; `run --cursor=force` is the opt-in
  X11/XWayland arrow fallback, and `verify` does not check cursor.
- WebView2 defaults to the **checksum-pinned catalog payload**, which is
  reproducible and proven on the default runner. The catalog pins the standalone
  installer + SHA-256 for offline installs and troubleshooting;
  `--webview2=latest` is an opt-in experimental path to Microsoft's Evergreen
  bootstrapper (no digest, and only proven under system Wine in the current QA
  environment). `doctor` warns when a pinned payload is older than 90 days. The
  recipe is still ours; only the payload is external.
- Download → verify SHA-256 → extract to temp → atomic rename. Locks prevent
  concurrent installs.
- Never mirror runner binaries; link upstream releases only.
- Reuse detection scans Bottles' runner dir and Steam `compatibilitytools.d`,
  offering import-by-reference (record `path:`), never copy or mutate.
- Per-app pin is explicit. `winbridge runners install` never changes a profile.

### 6.7 Setup pipeline

1. **Preflight** — Python, disk space, session, runner availability, installer
   exists, no concurrent operation on the same app id.
2. **Resolve identity** — provisional id from `--id` or `slugify(installer
   stem)`; collision refused. The display `name` comes from the installed
   Uninstall `DisplayName` after step 6, falling back to the installer stem.
   The id never changes after prefix creation.
3. **Create the prefix** — `backend.create_prefix(app_id, keep_z=options.
   keep_z_drive)` under the selected runner. Remove Wine's `dosdevices/z:`
   host-root link unless `--keep-z-drive`; only `expose` may create host links.
   The guest user is the host user (`C:\users\<host-user>`).
4. **Install WebView2 (before app)** — detect via
   `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
   `pv`. If absent: use the checksum-pinned catalog payload (default);
   `--webview2=latest` opts into the Evergreen bootstrapper. Stage the installer
   under `C:\windows\temp` first, because `Z:` is stripped; invoke the guest
   path and remove the copy after success or failure. A timeout keeps the copy
   only while the runtime is still missing, for diagnosis; when the runtime is
   already registered the timeout is a slow success and the copy is removed.
   Run `/silent /install`, kill stuck `MicrosoftEdgeUpdate.exe`,
   apply the `msedgewebview2.exe` win7 override, re-verify. An installer
   timeout with the runtime already registered is a slow success, not a
   failure; only a still-missing runtime fails. Never sudo. Record the runtime
   version.
5. **Snapshot** — paths + sizes + mtimes + ctimes, plus SHA-256 content
   hashes for `.exe` files, under `Program Files*` and each user's
   `AppData\Local` / `AppData\Roaming` (including `Local\Programs`). `.lnk`
   files under each user's Desktop and Start Menu plus the machine Start Menu
   `Programs` root are also captured (other files there are ignored); registry
   export; scheme list.
6. **Silent install** — NSIS `/S`; Inno `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`;
   MSI via `msiexec /i /qn /norestart`; Burn `/quiet /norestart`; unknown →
   try `/S`, then `/silent`, then fail with the installer's log path and a
   `--manual` mode that runs the GUI installer inside the prefix. Every
   installer (EXE and MSI) is copied into `C:\windows\temp` first and invoked by
   its guest path, because `Z:` is stripped; remove after quiescence, keep on
   timeout.
7. **Wait for quiescence** — installer process exit, then settle until no new
   installer children for 5 s; timeout 15 min.
8. **Verify install** — at least one genuinely new or changed executable and
   exit code 0; otherwise fail with evidence. Executable content is compared by
   SHA-256 when available, so metadata-only changes and config/log writes alone
   do not count.
9. **Detect exe** — rank new/changed `.exe` files by install location, size,
   filename similarity to the product, PE subsystem (GUI binaries outrank
   console sidecars), and exact primary targets of `.lnk` files. New/changed
   links get a strong bonus; pre-existing links are weak fallback evidence only
   when their exact target exists and is a candidate. Uninstallers, updaters,
   and helpers are excluded; known bundled sidecars and browser helpers
   (`node.exe`, `python.exe`, `ffmpeg.exe`, `renderer.exe`, `browser.exe`,
   `msedgewebview2.exe`, crashpad/elevation helpers, VC redistributables) are
   never auto-selected as the app, even as a lone candidate; a user can always
   pass `--exe`. Update/rebuild treat the recorded profile executable as
   authoritative when it still exists; a missing, excluded, or out-of-prefix
   recorded path fails instead of falling through to a changed helper.
   With several GUI candidates and no shortcut evidence, a name lead of at
   least 0.25 is required; otherwise detection refuses and `--exe` is required
   (prompted on a TTY when available).
10. **Detect metadata** — schemes from the registry diff (keys containing
    `URL Protocol` whose `shell\open\command` points into the app's install
    tree); app version from Uninstall `DisplayVersion`; display name from
    Uninstall `DisplayName`, using the detected exe as plausibility evidence.
11. **Write the provisional profile** — so a verification failure still leaves a
    usable, inspectable record with evidence.
12. **Launcher (initial)** — write
    `$XDG_DATA_HOME/applications/winbridge-<id>.desktop` with
    `Exec=<absolute winbridge> run <id>` (wrapper, never raw wine) and an icon
    (extracted if `wrestool` exists, generic otherwise). No `StartupWMClass` yet
    and no scheme `MimeType`; the scheme handler is a separate entry (§6.10).
    `update-desktop-database` best-effort.
13. **Verify (tri-state)** — `verify=None` is the default: launch, wait for the
    window, screenshot, background-diff visibility check, record first-paint
    time, PASS/FAIL, and the desktop window class. `--no-verify` skips. If the
    desktop cannot verify (unsupported adapter or missing tools), record
    `last_verified = skipped`, warn, and exit 0. An explicit `--verify` exits 3
    when verification is impossible. A timeout or `transparent` result is
    retried **once** with the same timeout after a fresh kill/relaunch (the
    first launch after install can miss its window);
    both attempts keep their evidence. Only the default automatic verify
    (`verify=None`) retries; an explicit `--verify`, unsupported desktops, tool
    failures, and `--no-verify` are single-attempt. A failed check after the
    retry on a supported desktop exits 4 but keeps the profile, launcher, and
    evidence so the runner can be re-pinned without reinstalling. Cursor
    visibility is not checked.
14. **Refresh launcher and pin** — rewrite the launcher with the verified
    `StartupWMClass`, then, when omarchy-shell is present, add
    `{"desktopId": "winbridge-<id>", "match": "<escaped class>"}` to the
    `io.github.joeyvigil.taskbar` widget's `apps` array in
    `~/.config/omarchy/shell.json` (left section preferred; current Omarchy 4
    nests sections under `bar.layout.<section>`, older configs put them
    directly on `bar.<section>`, and both are read). The stored id is
    bare: the click path runs `gtk-launch <id>.desktop`, so an id carrying
    `.desktop` double-appends and fails. `match` comes from the verified
    app-specific window class; a known runner-generic class
    (`steam_proton`, `wine`, `wine64`, `wine-preloader`, `explorer.exe`) falls
    back to the exe stem, as does a skipped verify, so re-clicks focus the
    running window instead of spawning duplicates. Atomic
    write with a backup; preserve unrelated keys; strict JSON. Run
    `omarchy restart shell` best-effort after writing (`--no-shell-restart` opts
    out), because desktop entries are not always indexed otherwise. Non-Omarchy
    desktops have no pin capability: launcher only. `Exec` escaping still
    doubles backslashes; `desktop-file-validate` is advisory only.
15. **Finalize the profile** — record the display name, launcher/pin paths,
    runtime version, and `last_verified` result.

Every step prints the detected/decided value. Failures keep evidence and point
to `winbridge doctor`.

### 6.8 Commands

```
winbridge setup <installer> [--id ID] [--force] [--runner RUN] [--exe PATH]
                           [--scheme S] [--silent-args "..."] [--backend wine]
                           [--webview2 pinned|latest (default pinned)]
                           [--keep-z-drive] [--manual]
                           [--verify|--no-verify] [--no-shell-restart] [--yes]
winbridge update <app> [--installer PATH] [--force] [--verify|--no-verify]
winbridge run <app> [--url URL] [--restart] [--wait] [--timeout 300] [--json]
                    [--cursor off|force] [--] [app args...]
winbridge verify <app> [--timeout 300] [--keep-all] [--json]
winbridge expose <app> <host-folder> [--as NAME]
winbridge unexpose <app> <host-folder|NAME>
winbridge list [--json]
winbridge inspect <app> [--json]
winbridge unbridge <app> [--yes]
winbridge runners list|install <id@version>|import <path>|pin <app> <id@version> [--rebuild] [--yes]
winbridge doctor [<app>] [--bundle]
winbridge open-url <app> <url>          # desktop handler target, experimental
winbridge login-bridge install|status <app>   # experimental spike
```

Exit codes: 0 success, 1 failure, 2 usage, 3 unsupported environment, 4 verify
failed. `--json` on `list`/`inspect`/`verify`/`run` for scripts. Global
`--verbose`.

Behavior rules:
- `run` never kills a running app. If the prefix is already running, plain
  `run` prints that and exits 0 (`--wait` fails with `already-running` instead
  of claiming an instance it did not launch); `--restart` is the only path that
  kills. `setup`, `verify`, and `update` kill explicitly because they own the
  prefix during their work. This is what makes a pin click safe while the app is
  open. A launch's stdin is `/dev/null` and its stdout/stderr go to the per-app
  run log, never the caller's stdio: a surviving app or bridge child cannot hold
  the caller's pipe open after `run` returns.
- `run --url` and `open-url` never kill; they forward if running, otherwise
  start then forward. The scheme handler is a separate desktop entry (§6.10).
- `run` is non-blocking by default: it prints `[run +Xs]` stage lines for
  stop/launch and ends with a `launched (no wait)` result, then exits 0.
  `--wait` runs the verify watcher and exits 0 only after two visible frames it
  observed itself; it defaults to 300 s, and `--timeout` overrides that and is
  invalid without `--wait` (negative values are a usage error). A failed wait
  exits 4 with `transparent`, `background`, `timeout`, or `already-running`; an
  unsupported desktop exits 3 before anything is killed or launched, and a
  launch or backend error exits 1 with reason `launch`. `--json` replaces the
  human lines with NDJSON on stdout, one `stage` or `result` object per line, so
  output can be parsed while streaming. Result reasons are `passed` (wait),
  `no-wait` (default launch), `already-running`, `unsupported` (exit 3),
  `transparent`, `background`, `timeout`, and `launch`. Every mode that reaches
  a launch ends with exactly one `result` event, including failures.
- `run --cursor=force` is an experimental X11/XWayland fallback for Wine bug
  58922, not a default. It spawns a detached helper that reads X11's own root
  children and their `_NET_WM_PID` values, keeps windows owned by the prefix's
  processes (selected by PID, never by title or class), and applies the standard
  X11 arrow to them and their descendants; the arrow also overrides an
  app-provided I-beam or link cursor. Compositor window lists are never used,
  because their ids may be 64-bit client addresses rather than XIDs. The helper
  is single-instance per app through an advisory lock, gets `/dev/null` stdio so
  it can never hold the caller's pipe, and exits when the prefix has no
  processes left. A session without `DISPLAY`, libX11, or a reachable X display
  is refused with exit 3 before any kill or launch. `--cursor=off` is the
  default and changes nothing. `--cursor=force` does not change `verify`: its
  result still says "cursor not checked".
- `update <app>` scans the directory recorded in `installer.dir` using
  `installer.glob`: newest by version parsed from the filename, then mtime. It
  kills, reinstalls in place, preserves the profile, optionally verifies, and
  records the new version. The default requires a genuinely new or changed
  executable (SHA-256); a zero-exit install with identical bytes fails.
  `--force` accepts that same-build case only when the recorded executable
  still exists, with a warning that no executable change was detected.
  `--installer` overrides the scan and updates the recorded path/dir. No
  batch/multi-app update in v0.1.
- `runners pin` is record-only by default and warns that an initialized prefix
  is tied to its runner. `--rebuild` kills the prefix, recreates it under the
  new runner, reinstalls WebView2 and the recorded installer, and re-detects the
  exe/version. It preserves the profile, launcher, pin, schemes, and exposed
  folders; it fails clearly if the installer is missing. Confirmation unless
  `--yes`. This is the only automatic path that discards a prefix.
- `expose` symlinks the host folder into `drive_c/users/<user>/<name>`
  (host-native backend needs no Flatpak override). Refuses to replace an
  existing path; `unexpose` removes only a symlink it owns.
- `unbridge` kills, removes the prefix, profile, both desktop entries (launcher
  and scheme handler), scheme defaults, and its exact entry from the
  omarchy-shell `apps` array (backup + atomic write, then best-effort shell
  restart). Its own entries only; never user content. Idempotent. Confirmation
  unless `--yes`.
- Setup is re-runnable: an existing profile requires `--force`, which backs up
  the old profile first.

### 6.9 Verify harness

- Launch through the same code path as `run`, but `run_app` returns a
  `Launch(popen, pid_tree)` handle. The whole verify sequence (kill, background,
  launch, captures, finish) holds the per-app lock, so a standalone verify
  queues behind setup/update/run/unbridge; setup and update already hold it and
  the lock is re-entrant in the same thread. Wine reparents the app to PID 1, so
  verify treats a window as the launch's when its pid is in that tree **or** in
  `processes_for_prefix(prefix)` (re-probed on every poll). A window outside
  that candidate set is ignored even when its title/class matches the profile;
  title/class is never evidence on its own.
- Window match is process membership first, never title alone. Tiny helper
  windows (width < 200, height < 150, or area < 30k) are ineligible; among
  matching windows the largest area wins. A window switch resets the
  consecutive-frame streak, and pass needs two visible frames from the same
  window id. Visible means not blank **and** not identical to the pre-launch
  desktop background; wallpaper showing through a transparent window is not app
  content.
- Capture a frame immediately, then poll every 2 s and capture every 10 s;
  default timeout 300 s. `first_paint_s` is the timestamp of the first visible
  frame, not the confirming frame.
- Verify owns the prefix: kill it first, then capture the full-screen
  background, then launch with `restart=False`. This ordering keeps a previous
  app instance out of the background.
- The background is mandatory for a supported adapter. Capture through
  `screenshot_screen` (Hyprland plain `grim`, X11 `import -window root`) into
  the evidence directory, and validate that it is a readable image. For each
  window frame, inset the compared region by 2 px (to skip a thin border/shadow
  ring), crop the background to the window geometry, and compare pixels. Exact
  static-background matches are rejected when the mean absolute difference is
  <= 0.5 **and** at least 99.9% of pixels are identical; the strict AND avoids
  mistaking dark content with small contrast for the desktop. A failed, missing,
  corrupt, or geometrically incompatible background fails closed with
  `reason="background"` and exit 4; it never falls back to a blank/alpha pass.
  The background stays beside the frames for a human to compare. Borders,
  shadows, animated backgrounds, output scale, and multi-monitor layouts can
  still affect the diff; inspect the evidence or use a static background when
  in doubt. Known limits: 1x scale is assumed (HiDPI sizes can mismatch),
  negative or out-of-bounds window rects and multi-monitor layouts fail the
  crop, and an animated background can make a transparent window look
  different.
- Setup retries a `timeout` **or** `transparent` automatic verify once with the
  same timeout and a fresh relaunch; a `background` failure is not retried. An
  explicit `--verify` and the standalone `winbridge verify` command do not
  retry. Both setup attempts keep their evidence, and only the final result is
  recorded.
- `is_blank(image: Path)` on the window region: grayscale standard deviation and
  dominant-color share; thresholds tuned against sampled frames. Additional
  signals are a fully transparent alpha channel, compositor/`_NET_WM_WINDOW_OPACITY`
  opacity `0`, and a frame identical to the background crop. A frame can pass
  only after the same window is mapped and stable for two consecutive visible
  captures.
- Cursor visibility is not checked. Print "cursor not checked" in the result so
  the limitation is visible, not implicit. WebView2 apps on the default runner
  can paint with no cursor: child processes set cursors on a window owned by a
  different process, Wine never calls `XDefineCursor`, and upstream
  `cursoricon.c` is unchanged through Wine 11.15 (Wine bug 58922). This is a
  known limitation, not a WinBridge regression; no runner or compositor
  workaround is promised, and `WINBRIDGE_WINEDEBUG=+cursor` plus the per-app run
  log is the trace recipe. `run --cursor=force` is the separate opt-in
  X11/XWayland fallback; it does not make verify inspect the cursor.
- Result: PASS/FAIL, first-paint time, runtime version, runner, app version.
  Evidence keeps the background, the first frame, the passing frame, and the
  last frame. `VerifyResult.evidence_dir` and `verify --json` report the run
  directory even when no frame exists (for example a background-only failure).
- App version comes from the prefix's Uninstall registry `DisplayVersion`;
  fallback to the installer filename, then `unknown`. Evidence paths use that
  value, never a guessed semver.
- Human rule from the field: screenshots are always viewable evidence; no
  automated check is trusted over a human looking at the frames.

### 6.10 Login-callback bridge (experimental)

- Schemes come from the profile; only those may be registered. `setup` records
  them but writes no handler.
- `login-bridge install <app>` writes a separate
  `winbridge-<id>-handler.desktop` (`NoDisplay=true`,
  `MimeType=x-scheme-handler/<scheme>;`,
  `Exec=<absolute winbridge> open-url <id> %u`) and registers it with
  `xdg-mime default`. Do not hand-edit `mimeapps.list` for registration.
  `unbridge` removes the handler entry and the app's lines from the default
  applications list.
- Forwarding under Wine is unproven: spike `wine start <url>`, `cmd /c start`,
  and relaunch-with-argv; record which works per app. The spike gates the
  experimental flag, not the v0.1 release.
- No token logging; URLs are redacted in logs.

### 6.11 Doctor

Checks: Python/deps, session + adapter capabilities, screenshot tool, runner
store integrity (hashes), prefix health, WebView2 version and pin staleness,
launcher/scheme registration, disk space, stale locks. `--bundle` writes a
redacted tarball (versions, commands, sanitized logs) under
`~/.local/share/winbridge/bundles/`, never the CWD, ready for an issue.

### 6.12 Logging

App-scoped commands log under `evidence/<app>/<version>/<ts>/winbridge.log`
(`unknown` before a version exists). Commands with no app (`doctor`, `runners
install`, `list`) log to `logs/<ts>-<command>.log`. A global rolling
`~/.local/share/winbridge/winbridge.log` receives every invocation.
`--verbose` raises subprocess output into the log. Default `WINEDEBUG=-all`; an
inherited `WINEDEBUG` is ignored, and `WINBRIDGE_WINEDEBUG` (when set and
non-empty) is the explicit trace escape, used verbatim. App launch output
appends to `logs/run-<app-id>.log`, never the caller's stdio. The opt-in cursor
helper appends its `[cursor]` activation/stop lines to the same file; otherwise
only the app launched by `run` writes there, and setup/`wineboot` and
URL-forwarding output stays with their own commands. The run log is raw app
output, so `doctor --bundle` redacts it on export, and a `+all` or cursor trace
grows until the app exits.
Colors only on TTY. Stable message prefixes (`[setup]`, `[detect]`, `[verify]`)
for greppability.

## 7. Support matrix

| Platform | Status | Notes |
|---|---|---|
| Arch / Omarchy + Hyprland | **release-checklist target (pending verification)** | Primary target; the label is earned only by the manual checklist |
| Arch + X11 | experimental | X11 adapter; CI covers the Wine pipeline under X11/headless |
| Other Arch desktops (GNOME/KDE) | experimental | launcher/run work; verify degrades |
| Debian/Fedora/Ubuntu via `uv tool` | experimental | host Wine runners are managed, not distro Wine |
| Other Wayland compositors | unsupported | run may work; verify does not |
| Flatpak-packaged WinBridge | unsupported | by design |

"Verified" means the release checklist has been executed on Omarchy/Hyprland.
CI covers the generic Wine pipeline under X11/headless; it cannot reproduce
Hyprland + NVIDIA, so CI alone never earns the "verified" label. Until the
manual checklist is recorded, the primary row remains a target; everything else
gets an explicit "experimental" label in the README; no silent claims.

## 8. Repository layout

```
pyproject.toml            uv, Typer, Rich, tomli-w; dev: pytest, ruff
src/winbridge/
  cli.py
  core/    errors.py state.py config.py profile.py locking.py runner.py
           prefix.py installer.py detect.py webview2.py launcher.py omarchy.py
           expose.py run.py setup.py update.py unbridge.py verify.py doctor.py
           urlbridge.py context.py
  backends/ base.py wine.py bottles.py
  desktops/ base.py __init__.py hyprland.py x11.py
  data/ catalog.toml
  util/    proc.py download.py reg.py slug.py log.py
tests/     conftest.py (shared paths, profiles, fake backend/desktop)
           unit/ (fake prefixes, fixture registries, mocked adapters)
           integration/ (Wine + synthetic fixture, CI)
fixtures/  synthetic-tauri-app/ (built on Windows CI, never committed)
docs/      design.md plan-v0.1.md PLAN.md SUPPORT.md
           TROUBLESHOOTING.md GLOSSARY.md PUBLISHING.md RELEASE-CHECKLIST.md
packaging/ aur/PKGBUILD
```

Dependencies stay small and pure-Python where possible: Typer, Rich, `tomli-w`,
Pillow (verify), pytest/ruff (dev). Stdlib for HTTP, hashing, subprocess, TOML
reading. No shell=True anywhere.

## 9. Testing and CI

- **Unit**: profile round-trip/migration, slug collisions, registry `.reg`
  parsing, scheme filtering, installer-type detection, escaping, expose
  ownership, runner hash verification (with fixtures), command exit codes.
- **Adapter/backend**: fake implementations injected; no desktop or Wine needed.
- **Integration (nightly + release)**: build the synthetic Tauri 2 fixture on
  Windows CI, then run `setup → verify → update → expose → unbridge` under Wine
  on an Arch container with X11/Xvfb (and Wine multilib for 32-bit installers).
  CI exercises the Wine pipeline and the X11 adapter only; Hyprland + NVIDIA is
  covered by the manual release checklist. The fixture has a custom scheme and a
  deliberately slow first paint.
- **No NDA content**: the fixture is authored in-repo; CI artifacts contain no
  third-party binaries.
- Release gates: unit + lint + type check; integration on release tags; manual
  Omarchy checklist (render, cursor, files, login, pin).

## 10. Open-source deliverables

Repository deliverables, all derived from this spec:

- `LICENSE` (MIT), `README.md` (pitch, install, 3-command demo, support matrix,
  known limitations), `CONTRIBUTING.md` (dev setup, backend/adapter guide,
  fixture app, NDA rule), `SECURITY.md` (untrusted installers, private reports),
  `CODE_OF_CONDUCT.md` (Contributor Covenant), `AGENTS.md`, issue/PR templates,
  `CHANGELOG.md`.
- `docs/design.md`, `docs/plan-v0.1.md`, `docs/SUPPORT.md`,
  `docs/TROUBLESHOOTING.md`, `docs/GLOSSARY.md`, `docs/PUBLISHING.md`,
  `docs/RELEASE-CHECKLIST.md`.
- AUR PKGBUILD, `uv tool`/PyPI release workflow, tagged releases, SHA-256
  entries for runners and the WebView2 payload in the embedded catalog (the
  package signature is the trust channel).
- No vendor names, screenshots, installers, or app-specific profiles.

## 11. Milestones

- **M0** — this spec; repo skeleton; CI (lint, types, unit); shared fixtures and
  fakes; composition root; LICENSE/README/CONTRIBUTING stubs.
- **M1** — state/profile, catalog + runner store, Wine backend, prefix (with
  `Z:` stripped), WebView2, silent install, exe/scheme detection.
- **M2** — launcher + safe `run` (never kills unless `--restart`),
  expose/unexpose, unbridge.
- **M3** — verify harness + adapters, doctor, update, logging/evidence.
- **M4** — login-bridge spike, docs polish, AUR, v0.1 release.
- **v0.2** — Bottles backend, GNOME/KDE adapters, fonts/corefonts stage, GPU
  policy flags, EBWebView profile reset command.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| WebView2 installer behavior changes under Wine | Recipe isolated behind one module; doctor reports version; integration fixture catches breakage |
| No generic single-instance URL forwarding under Wine | Spike first; ship as experimental; device-code logins need no callback |
| Runner provider removes/renames releases | Catalog pins exact URLs + hashes; cached downloads; `path:` escape hatch |
| Installer ignores silent flags | Ordered flag attempts; `--manual` GUI fallback inside the prefix; clear failure |
| Blank-window false results | Human-viewed evidence retained; stability window; conservative thresholds |
| Desktop diversity explodes | Adapter interface + explicit support matrix; degrade, don't guess |
| Distro dependency drift | Managed runners; pure-Python core; only Pillow is native |
| NDA leakage | App-agnostic rule enforced in CONTRIBUTING and review; synthetic fixture; sanitized field notes; public history rewrite per `docs/PUBLISHING.md` |
| Supply-chain risk | HTTPS + pinned SHA-256 for runners; no mirroring; never sudo; scheme allowlist |
| Wine `Z:` exposes the host root to installers | strip `dosdevices/z:` by default; `--keep-z-drive` explicit; only `expose` creates host links |
| Default runner has an invisible cursor | documented known limitation (Wine bug 58922; WebView2 child cursors never reach `XDefineCursor`); no runner/compositor workaround promised; keep the default unless a runner is validated for paint; `verify` prints "cursor not checked"; `run --cursor=force` is the opt-in X11/XWayland arrow fallback |
| WebView2 child holds the caller's pipe after `run` | launch stdio is `/dev/null` + a private per-app run log; the caller's pipes are never inherited |
| Latest WebView2 is unpinned | Pinned catalog payload is the default; `--webview2=latest` is opt-in experimental with an HTTPS/Microsoft-domain guard; the installed version is recorded; doctor warns on stale pins |
| A pin click kills unsaved work | `run` never kills; only `--restart` does; setup/verify/update own their prefix |
| omarchy-shell config write breaks the bar | one pin module, strict JSON, backup + atomic write, preserve unknown keys, doctor validates; restart opt-out |
| Shell restart disrupts the session | restart only after a pin change, best-effort, `--no-shell-restart`; unbridge confirms first |
| First-time maintainer scope creep | v0.1 cut line fixed in §11; anything not listed is v0.2+ |

## 13. Decisions made / left to the builder

Made here: host-native engine, Python 3.11+ with uv, Typer/Rich, MIT, XDG state,
TOML profiles, managed runner store, vendored WebView2 recipe, internal backend
and desktop seams, Wine-only v0.1, Hyprland + X11 adapters, zero-touch setup with
generated profile, support-matrix honesty, no Flatpak, no plugin API, login
bridge experimental, `run` never kills, pinned WebView2 by default with latest
opt-in experimental, host `Z:`
removed, and `winbridge` as the final package/name for v0.1.

Left to the builder (no product decision changes): detection ranking weights and
ambiguity thresholds, timeout defaults beyond the stated 300 s/15 min, exact
visibility thresholds, icon extraction fallback behavior, evidence retention
count, log formatting, fixture app implementation, catalog contents, and AUR/CI
details.

## 14. Builder review resolutions

Ambiguities found in the first draft, settled here so the plan is buildable:

- **Default runner:** catalog default (`soda@11.0-10`); no silent retry on a
  failed verification. The profile and evidence are kept, and re-pinning is an
  explicit command. Its invisible-cursor behavior is a documented limitation.
- **Runner execution:** wine-kind only in v0.1, invoked as the runner's
  `bin/wine`; the backend is runner-bound; prefix user is the host user; Proton
  deferred.
- **Update source:** `installer.dir` + `installer.glob` live in the profile; no
  batch update command.
- **Runner change:** record-only by default; `--rebuild` recreates the prefix
  and reinstalls the app but preserves the profile, launcher, pin, schemes, and
  exposed folders.
- **Verify unavailability:** `SetupOptions.verify` is tri-state (`None` default,
  `True` explicit, `False` disabled); automatic setup verify skips with exit 0
  and `last_verified = skipped`; an explicitly requested verify exits 3; a timed-out
  setup verify retries once after a fresh relaunch; a failed verify exits 4 and
  keeps the profile/evidence.
- **Identity:** app id from `--id` or `slugify(installer stem)` before prefix
  creation; display name from Uninstall `DisplayName`; id never changes.
- **App version:** Uninstall `DisplayVersion`, then installer filename, then
  `unknown`.
- **WebView2 payload:** checksum-pinned catalog payload by default; the latest
  Evergreen bootstrapper is opt-in experimental (`--webview2=latest`); the
  installed version is recorded and stale pins warn in doctor.
- **Scheme registration:** setup records schemes only; `login-bridge install`
  writes a separate `NoDisplay` handler desktop entry and owns `xdg-mime
  default`; `unbridge` removes both entries and our default lines.
- **Launch safety:** `run` never kills; `--restart` is the only kill path for a
  user launch; setup/verify/update kill explicitly because they own the prefix.
- **Host boundary:** `dosdevices/z:` is removed at prefix creation;
  `--keep-z-drive` is an explicit compatibility escape.
- **Cursor:** verify checks render only and prints "cursor not checked";
  `run --cursor=force` is an explicit, experimental X11/XWayland fallback and
  never changes verify's policy.
- **Catalog signing:** cut. The embedded catalog plus the package signature is
  the trust chain.
- **Pin (confirmed live on the target):** Omarchy's Quickshell bar
  (`omarchy-shell`), plugin `io.github.joeyvigil.taskbar`, config
  `~/.config/omarchy/shell.json`, bar left section `apps` (current Omarchy 4
  nests sections under `bar.layout`; legacy `bar.<section>` is also read).
  Entry is
  `{"desktopId": "winbridge-<id>", "match": "<regex>"}` with a bare id (the click
  path runs `gtk-launch <id>.desktop`, so an id with the suffix double-appends);
  `match` comes from the verified window class so re-clicks focus instead of
  spawning duplicates. Atomic write with backup, preserve unrelated keys, strict
  JSON; the bar hot-reloads on save, and `omarchy restart shell` is run
  best-effort when entries need indexing (`--no-shell-restart` opts out).
  `pin`/`unpin` are real v0.1 features; no dock detection; X11 has no pin.
