# Troubleshooting

Start with `winbridge doctor <app> --bundle`. It checks the session, tools,
runner hashes, prefix, WebView2, launcher, scheme registration, disk space, and
stale locks, and writes a redacted archive for an issue.

## WebView2 is missing after setup

Setup installs the runtime before the app and stages the installer inside the
prefix (`C:\windows\temp\winbridge-webview2-<unique>.exe`), because the host
`Z:` mapping is removed. The staged copy is removed after a successful or
failed install. A timeout keeps it only while the runtime is still missing; if
the runtime is already registered, the timeout is treated as success and the
copy is removed.

- The default is the checksum-pinned catalog payload. `--webview2=latest` is an
  experimental Evergreen-bootstrapper path and was only proven under system Wine
  in the current QA environment.
- Run `winbridge doctor <app> --bundle`; it reads the actual `pv` from both
  registry views.
- A timeout is not proof of a failed install: installer child processes can keep
  running after the launcher exits. Check `doctor` and the evidence logs before
  retrying.

## WebView2 flags have no effect

On WebView2 runtime 150 and newer under Wine,
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` is ignored: the runtime treats the host
as elevated, so the environment variable is dead. Set the policy instead, in
**both** registry views inside the prefix:

```
HKLM\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
```

Use `reg add ... /reg:64` and `reg add ... /reg:32`. For example,
`--enable-logging` in that policy produces `chrome_debug.log` and does not
prevent pages from rendering. WinBridge sets no browser flags by default;
`winbridge doctor <app>` prints the detected runtime version so you can confirm
which channel is in use.

On WebView2 runtime 152 and newer, the runtime can hide the cursor while the
user types. Adding `--disable-features=HideCursorWhileTyping` to that HKLM
policy may not change the behavior; it is a diagnostic experiment, not a fix,
and WinBridge does not set it as a default flag.

## `run` looks hung and prints nothing

App and bridge children no longer inherit the caller's stdout/stderr. App output
appends (mode `0600`) to
`~/.local/share/winbridge/logs/run-<app-id>.log`, so a child that outlives the
launcher cannot keep the caller's pipe open and make `run --restart` look stuck
after the command has returned. `run` is non-blocking by default and ends with a
`launched (no wait)` result.

- Read the per-app run log for the app's own output.
- If the log cannot be opened, WinBridge logs a warning and discards app output;
  the launch still proceeds.

## The app installer fails

WinBridge copies every app installer (EXE or MSI) into
`C:\windows\temp\winbridge-<unique>.<ext>` inside the prefix and runs it by its
guest path; setup does not depend on Wine's host `Z:` mapping. The copy is
removed after a successful or failed install and kept when the installer times
out, so a timeout leaves the exact payload for diagnosis.

Check the installer log path printed by setup and run
`winbridge doctor <app> --bundle`. With `--manual`, the GUI installer runs inside
the prefix and can show the vendor's own error.

## Setup launches a bundled helper instead of the app

Setup and update rank new executables by name, size, install location, PE
subsystem, and the exact primary target of installer shortcuts. The app's own
shortcut is the strongest automatic signal; targets must match the candidate
executable path exactly (case-insensitive), and a link whose target is missing,
outside the prefix, or a known helper is ignored. New or changed shortcuts get
a strong bonus; pre-existing shortcuts are weaker fallback evidence. GUI
binaries outrank console sidecars. Known bundled sidecars and browser helpers
(`node.exe`, `python.exe`, `ffmpeg.exe`, `renderer.exe`, `browser.exe`,
`msedgewebview2.exe`, crashpad/elevation helpers, VC redistributables) are
never auto-selected as the app, even as a lone candidate; pass `--exe` to
choose one deliberately. The known list is **not exhaustive**: an unlisted
helper can still be chosen when a new installer shortcut points at it, even
when another GUI candidate is the app. Update and rebuild trust the recorded
profile executable when it still exists; if it is missing or unusable, they
stop with an `--exe` hint. An app whose main executable is named like a known
sidecar needs `--exe` at setup and cannot be auto-updated, because `update` has
no `--exe`. If several GUI candidates lack shortcut evidence and their name
scores are close, setup refuses to guess. Pass the executable explicitly:

```bash
winbridge setup <installer.exe> --exe 'C:\Program Files\Example\Example.exe'
```

Verify waits for a window, so a console server/sidecar without one can never
pass. Run `winbridge doctor <app> --bundle` to capture the recorded exe and the
verification evidence.

## The window is white or black

Expected on a cold start: 1–4 minutes while DXVK builds its state cache and
WebView2 loads. Rules:

- Do not kill it before ~2 minutes.
- Do not start a second instance while one is running; stacked launches exit
  immediately. `winbridge run` detects a running instance and refuses to stack;
  use `winbridge run --restart` only when you mean to kill it.
- Use `winbridge verify` and view the evidence frames; file size is not proof.
  Setup's automatic verify retries a timeout or `transparent` result once with
  the same timeout after a fresh relaunch; a second failure still exits 4. An
  explicit `--verify` and the standalone `winbridge verify` command do not
  retry. Both attempts keep their
  evidence.
- `verify` ignores windows smaller than 200x150 (or 30,000 px²) and prefers the
  largest main window; a tiny helper window is never used as evidence. If a real
  app surface is smaller than that, verification cannot pass.
- A runner-generic `WM_CLASS` (for example `steam_proton`) is replaced by the
  exe stem for `StartupWMClass` and the pin match.

If it never paints, try `winbridge verify` again, then re-pin the runner:

```bash
winbridge runners pin <app> soda@11.0-10 --rebuild
```

## The window is fully transparent

`verify` reports `transparent` instead of `timeout` when the window is mapped
but every captured frame is invisible. A frame is invisible when it is blank,
has a fully transparent alpha channel, reports window opacity `0`, or matches
the static desktop background captured before launch. A small (2 px) inset
avoids comparing a thin border/shadow ring. Exact static-background matches
are rejected, but a transparent window can still be affected by the limitations
below. A pass needs two consecutive frames that are both visible: not blank
**and** not identical to the pre-launch background.

- Wait: first paint can take 1–4 minutes; do not kill the app early. Setup
  retries a `transparent` automatic verify once with a fresh relaunch, exactly
  like a timeout, then fails.
- Remove the prefix's `EBWebView` directory and relaunch. Poisoned GPU/shader
  caches can leave the runtime permanently blank (see
  [Permanent blank after a runner change](#permanent-blank-after-a-runner-change)).
- Capture evidence and inspect the frames:

  ```bash
  winbridge verify <app> --keep-all
  ```

  The evidence directory keeps `background.png` plus the window frames. The log
  records each window's `opacity`, an `alpha=min..max` range when the capture
  has an alpha channel, and a per-frame `diff=<mean abs diff>` against the
  background.
- If content appears later in the same run, the normal two-consecutive-visible
  frame rule still passes. A transparent first frame is not a failure by itself.

### Why verify reports `background`

The background is mandatory for a supported desktop. Verify kills the old
prefix instance first, captures the bare desktop, then launches, so a previous
window cannot become part of the background. If the capture fails, the file is
missing or corrupt, or the window rectangle cannot be cropped from it, the run
fails with `reason="background"` (exit 4) rather than passing on blank/alpha
signals. A `background` failure is not retried by setup; fix the capture first.

The comparison rejects exact static-background matches. Borders, shadows,
animated backgrounds, output scale, and multi-monitor layout can still affect
the diff; inspect the evidence frames or use a static background when the
result looks wrong. Missing/corrupt backgrounds still fail closed with
`reason="background"`.

Known limitations:

- 1x output scale is assumed; HiDPI sizes can mismatch and report `background`.
- Negative or out-of-bounds window rectangles and multi-monitor layouts can
  fail the crop.
- A 2 px inset ignores small borders/shadows; larger decorations or focus
  effects can still shift the compared region.
- An animated background can differ between the capture and the frame.
- A stale or overlapping instance in the background is avoided by the kill-first
  order; an external window that moves during the run can still change pixels.

## Verify times out because another copy is running

A Tauri app already running in another prefix can consume the launch: the new
process hands off to the existing instance and exits, so no window appears for
this prefix. `verify` then reports `timeout` (or a different window class) even
though the app is running elsewhere.

- Close the copy running in the other prefix, then retry.
- Or give this install its own prefix by using a different app id, for example
  `winbridge setup <installer.exe> --id example-app-2`. WinBridge creates one
  prefix per app id.
- `winbridge run` never kills an existing instance; use `--restart` only when
  you mean to end unsaved work.

## `run` says already running and no window appears

A lingering Wine service or a hand-off helper can keep the prefix busy after the
visible window is gone. Plain `run` names the blocking process IDs:

```text
already running (pids 123, 456); use --restart
```

Inspect them before acting; window titles are not needed and may be stale:

```bash
ps -o pid=,stat=,args= -p 123,456
```

Then choose:

- `winbridge run <app> --restart` stops the prefix and starts one fresh
  instance. This discards unsaved work in the old instance.
- Stop only the listed PIDs (`kill 123 456`) when you know they are leftovers.
  Wine can respawn a service until its prefix is stopped cleanly.

Plain `run` never kills them automatically: the process probe cannot tell a
lingering backend from an app with unsaved work.

## The cursor is invisible

Known Wine/WebView2 limitation, not a WinBridge regression: WebView2 child
processes set cursors on a window owned by a different process, and the window's
own process cannot resolve them. Wine never calls `XDefineCursor`, so no cursor
is drawn. [Wine bug 58922](https://bugs.winehq.org/show_bug.cgi?id=58922) tracks
this; upstream `cursoricon.c` is unchanged through Wine 11.15. Check that the
session is XWayland and that `WAYLAND_DISPLAY` is unset in the app environment
(WinBridge does this); changing the desktop or the app does not work around the
bug.

Trace it explicitly, without changing the default quiet Wine environment:

```bash
WINBRIDGE_WINEDEBUG=+cursor winbridge run <app>
```

`WINBRIDGE_WINEDEBUG` is used verbatim for `WINEDEBUG`; an inherited `WINEDEBUG`
is still ignored. The trace and the app's own output append to
`~/.local/share/winbridge/logs/run-<app-id>.log`. Include that log, the runner,
the session type, and the GPU driver with the bundle.

`+all` or cursor tracing grows until the app exits; stop the app and truncate or
delete the run log to reclaim the space. The run log contains the app launched
by `run`, plus `[cursor]` marker lines when `--cursor=force` is active; setup and
`wineboot` output, and URL-forwarding `wine start` traces, are captured by their
own commands and do not appear there.

If the app runs under X11 or XWayland and you need a usable pointer now, opt in:

```bash
winbridge run <app> --cursor=force
```

A detached helper polls the prefix every two seconds, reads X11's root children
and their `_NET_WM_PID` values directly, and applies the standard X11 arrow to
windows owned by the prefix's processes and their descendants. It appends
`[cursor] force helper started (pid N)`, `[cursor] force active`, and
`[cursor] force stopped` to the same `logs/run-<app-id>.log`, so `doctor` and
`verify` evidence can show whether it ran. When the prefix has processes but no
matching X11 window yet, it logs `[cursor] force waiting: no matching X11 window
yet` once instead of claiming success. A second helper for the same app logs
`[cursor] already active` and exits. The flag requires `DISPLAY`, libX11, and a
reachable X display; it does not use the compositor's window list. Otherwise
`run` exits 3 before killing or launching anything. `--cursor=off` is the
default and changes nothing.

Limitations: native Wayland sessions are unsupported because there is no
`DISPLAY`; every context cursor becomes the arrow, including I-beam and link
pointers; the arrow is re-applied on a two-second poll, so a change can lag; and
a window created by a process outside the prefix is never targeted. There is no
upstream fix through Wine 11.15. This is an experimental convenience for a
known bug, not a substitute for a verified runner, and it does not make `verify`
check the cursor.

WebView2 runtime 152+ can also hide the cursor while typing; see
[WebView2 flags have no effect](#webview2-flags-have-no-effect). The HKLM
`--disable-features=HideCursorWhileTyping` policy may not change it.

No runner or compositor workaround is promised. Keep `soda@11.0-10` unless
another runner is validated for paint; cursor behavior is never a pass/fail
signal. Report the runner, session type, and GPU driver with the bundle.

## The app cannot see my folder

```bash
winbridge expose <app> ~/repos
```

The host folder appears as a symlink under the prefix's user profile. If the app
still refuses, confirm it validates paths inside its Windows profile; that is
why `expose` puts the link there. `unexpose` removes only links WinBridge owns.
WinBridge removes Wine's `Z:` host-root mapping, so `expose` is the supported
path; `--keep-z-drive` opts out at setup time.

## Login callback does nothing

The callback bridge is experimental. Check:

```bash
winbridge login-bridge status <app>
```

- The scheme must be in the app's profile, and the separate handler desktop
  entry must carry its `MimeType`.
- The default handler must be `winbridge-<app>-handler.desktop`.
- Forwarding into a running Wine app is a spike; if it fails, use a device-code
  or copy/paste flow.
- Never paste callback URLs with tokens into an issue.

## The taskbar pin is missing

On Omarchy:

- The pin lives in `~/.config/omarchy/shell.json`, plugin
  `io.github.joeyvigil.taskbar`, with a **bare** desktop id (no `.desktop`).
  Current Omarchy 4 stores bar sections under `bar.layout.<section>` (for
  example `bar.layout.left`); older configs use `bar.<section>`. WinBridge
  reads both and writes into the section that contains the taskbar widget.
- The bar reloads on save; if the entry does not appear, run
  `omarchy restart shell`.
- Re-click focuses the window only when the pin has a `match` regex. WinBridge
  records one from the verified window class.

## Permanent blank after a runner change

WebView2's `EBWebView` profile can be poisoned by GPU/shader cache. Remove the
`EBWebView` directory inside the prefix and relaunch. This is a troubleshooting
step, not a feature. A profile reset command is planned for v0.2.

## Fonts look thin or wrong

Wine ships no Segoe UI. Install Microsoft corefonts (Tahoma carries the UI) and
enable ClearType RGB smoothing in the prefix. A default font stage is planned for
v0.2.

## Collecting logs

```bash
winbridge doctor <app> --bundle
winbridge --verbose <command>
```

Logs live under `~/.local/share/winbridge/`; app launch output appends to
`logs/run-<app-id>.log`. Evidence frames are kept per app and version. Review
them before attaching; they may show app content.
