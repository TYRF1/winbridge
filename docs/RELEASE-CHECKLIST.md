# Release Checklist — v0.1.0

> **Status: procedure only — not executed.** This file is the release gate, not
> evidence that the gate was passed. A human must run it on the primary target
> (Arch / Omarchy + Hyprland, with the release GPU) and record every result.
> CI cannot earn the "verified" label, and no item here may be checked from CI
> output alone.
>
> Do not tag `v0.1.0` until every **pre-tag** section passes — including the
> README status/CHANGELOG promotion (10.1), the support-matrix/known-limitations
> gate (10.2), the sanitized-history scan (10.4), and the final merged-main scan
> before the tag (11.1) — or the item is explicitly
> listed as a known limitation in the README. Do not announce until the
> **post-publish** section passes too. A failure after PyPI has accepted files
> is fixed with a new patch version, never by moving a published tag.

This procedure validates the behavior described in the [design spec](design.md)
and the [support matrix](SUPPORT.md). It covers the manual Omarchy work that CI
cannot reproduce: real Hyprland/NVIDIA first paint, cursor visibility, taskbar
pins, file visibility, login callbacks, and desktop cleanup. Repository checks
still follow [SANDBOX.md](SANDBOX.md); manual GUI checks follow its host-side
exception.

## 0. How to record results

Use a disposable Omarchy account or a machine snapshot. This checklist creates
and removes a real Wine prefix, writes desktop entries and a taskbar pin, and
changes the default handler for the fixture's URL scheme.

Keep the filled-in record outside this repository. Evidence frames and logs can
contain app content, paths, and callback tokens; redact them before attaching
anything to an issue. Record at least:

| Field | Value |
|---|---|
| Date / release manager | |
| Machine / Omarchy version | |
| Kernel / GPU driver | |
| Hyprland version (`hyprctl version`) | |
| Candidate commit (`git rev-parse HEAD`) | |
| Fixture app id | |
| App version (from `winbridge inspect`) | |
| Runner (`winbridge inspect`) | |
| WebView2 mode / version (pinned default) | |
| WebView2 pinned (catalog) | |
| Cursor: visible / invisible | |
| `verify` first paint | |
| Overall pre-tag gate | PASS / FAIL |

For every item, record **PASS**, **FAIL**, or **NOT RUN (reason)**. A blank
checkbox is not a pass. When an item is a known limitation, still record what
happened and quote the README/SUPPORT wording that makes it acceptable.

Set this at the top of the shell used for the run:

```bash
set -o pipefail
INSTALLER="$HOME/Downloads/winbridge-fixture-setup.exe"
HOST_FOLDER="$HOME/winbridge-release-folder"
```

## 1. Preconditions

- [ ] Fresh Omarchy (Arch + Hyprland), non-root user, NVIDIA/XWayland as
      supported. No prior WinBridge app, profile, runner, pin, or scheme
      registration from another test.
- [ ] Network access for the first runner and WebView2 download.
- [ ] `uv`, `python` (for the checklist snippets), rootless `podman` (for
      `scripts/sandbox`), `git`, `gh` (GitHub CLI), `curl`, `gitleaks`
      (mandatory history scan), `base-devel`, `hyprctl`, `grim`,
      `desktop-file-utils` (`desktop-file-validate`,
      `update-desktop-database`), `xdg-utils` (`xdg-mime`, `xdg-open`),
      `gtk-launch` (launcher test), `pacman-contrib` (`updpkgsums`), and
      `devtools` (`extra-x86_64-build`) are installed.
- [ ] The generic fixture installer is available. Do **not** use a vendor
      installer, vendor screenshots, or vendor names in this record. The fixture
      is described in [fixtures/synthetic-tauri-app/README.md](../fixtures/synthetic-tauri-app/README.md);
      its NSIS installer uses `installMode: currentUser` and registers the
      `winbridge-fixture://` scheme, which is what exercises the per-user HKCU
      detection path.
- [ ] A disposable host folder exists with one marker file:

      ```bash
      mkdir -p "$HOST_FOLDER"
      printf 'release-check marker\n' > "$HOST_FOLDER/marker.txt"
      test -f "$HOST_FOLDER/marker.txt"
      ```

- [ ] Capture the starting state:

      ```bash
      git status --short
      git rev-parse HEAD
      uv --version
      python --version
      hyprctl version
      nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || true
      test ! -e "$HOME/.local/share/winbridge" && echo "clean state"
      test ! -e "$HOME/.config/winbridge" && echo "clean config"
      find "$HOME/.local/share/applications" -maxdepth 1 -name 'winbridge-*' -print
      python - <<'PY'
      import json, pathlib
      path = pathlib.Path.home() / ".config/omarchy/shell.json"
      if path.is_file():
          json.loads(path.read_text())
          print(f"shell.json valid: {path}")
      else:
          print("shell.json absent: no taskbar to test")
      PY
      ```

      Expected: clean state, no `winbridge-*` desktop entries, and a valid
      `shell.json` if Omarchy's taskbar exists. If `shell.json` is absent, the
      pin items cannot pass; record the environment as unsupported for pin
      validation instead of pretending they passed.

- [ ] If this is not a fresh account, remove only WinBridge's own state before
      starting. Prefer a snapshot/fresh account; do not run this on a daily
      driver. `winbridge unbridge <old-id> --yes` for every old id, then verify
      the paths above are empty. Do not delete unrelated desktop entries,
      `mimeapps.list` lines, or user files.

- [ ] Repository checks run through the sandbox. A plain clone is refused by
      `scripts/sandbox` by design; create a linked worktree from the candidate
      commit and work there:

      ```bash
      git worktree add --detach ../winbridge-release <candidate-sha>
      cd ../winbridge-release
      bash scripts/sandbox-build          # first run, or after Containerfile changes
      ```

      Expected: `scripts/sandbox` runs and the worktree is writable. See
      [SANDBOX.md](SANDBOX.md) for the isolation guarantees.

## 2. Source install and build gate (pre-tag)

Run the repository gates in the sandbox. The release workflow repeats the unit
suite and build on the tag, but a local failure must be found before the tag.

```bash
scripts/sandbox uv sync --dev
scripts/sandbox uv run pytest -q
scripts/sandbox uv run ruff check .
scripts/sandbox uv run mypy src
scripts/sandbox uv build
scripts/sandbox uv run --with twine twine check dist/*
sha256sum dist/winbridge-0.1.0.tar.gz
```

Expected:

- `pytest`: all tests pass; integration tests are skipped unless
  `WINBRIDGE_INTEGRATION=1` and Wine are available.
- `ruff`: `All checks passed!`
- `mypy`: `Success: no issues found`.
- `uv build`: `dist/winbridge-0.1.0-py3-none-any.whl` and
  `dist/winbridge-0.1.0.tar.gz` are created.
- `twine check`: both artifacts `Passed`.
- Record the sdist SHA-256 now; it is the value the AUR package must match after
  the tag, but the authoritative value is the one printed by the release
  workflow / downloaded from PyPI, not a local rebuild.

Install from the source tree and from the built artifacts on the host:

```bash
uv tool install --force .
winbridge --version
winbridge --help >/dev/null
command -v winbridge

uv tool install --force ./dist/winbridge-0.1.0-py3-none-any.whl
winbridge --version

uv tool uninstall winbridge
uv tool install --force ./dist/winbridge-0.1.0.tar.gz
winbridge --version
```

Expected: every `--version` prints `winbridge 0.1.0`; `--help` exits 0; the
installed command resolves to `~/.local/bin/winbridge`. Keep the sdist install
for the host-side behavior checks. This proves the artifact path that PyPI and
the AUR will use, before either exists.

- [ ] Source install (`uv sync` and `uv tool install .`) works.
- [ ] Wheel and sdist installs work and report `0.1.0`.
- [ ] `uv build` and `twine check` pass.

## 3. Setup — zero-touch, pinned WebView2, detection, force, manual, Z-drive

### 3.1 Zero-touch install with the pinned WebView2

Run without `--yes` and without any detection override. This is the zero-touch
claim: no required input, every detected value printed.

```bash
winbridge setup "$INSTALLER" 2>&1 | tee "$HOME/wb-release-setup-pinned.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected (all values printed; exact order may vary, but WebView2 must be
installed before the app):

```text
[setup] installer: ...\winbridge-fixture-setup.exe (nsis)
[setup] app id: <slug>
[setup] runner: soda@11.0-10
[setup] backend: wine
[setup] prefix: ~/.local/share/winbridge/prefixes/<slug>
[setup] z drive: stripped
[setup] webview2: <detected version> (pinned)
[setup] install: [...] exit 0 ...
[setup] exe: C:\...\WinBridge Fixture.exe
[setup] schemes: winbridge-fixture
[setup] app version: 0.1.0
[setup] display name: WinBridge Fixture
[setup] profile: ~/.local/share/winbridge/profiles/<slug>.toml
[setup] launcher: ~/.local/share/applications/winbridge-<slug>.desktop
[setup] verify: PASS (first paint ...; cursor not checked)
[setup] pin: winbridge-<slug> (<match>) updated
[setup] result: pass
```

- [ ] Setup exits 0 with no prompt.
- [ ] WebView2 is detected and installed before the app; the version is printed.
- [ ] Exe, scheme, app version, display name, launcher, and profile are printed.
- [ ] `profile:` is under `~/.local/share/winbridge/profiles/`.
- [ ] `z drive: stripped` is printed.

Set the variables used below from the printed id (do not guess the slug):

```bash
APP='<app id printed by setup>'
PREFIX="$HOME/.local/share/winbridge/prefixes/$APP"
PROFILE="$HOME/.local/share/winbridge/profiles/$APP.toml"
LAUNCHER="$HOME/.local/share/applications/winbridge-$APP.desktop"
HANDLER="$HOME/.local/share/applications/winbridge-$APP-handler.desktop"
```

- [ ] Check every detected value is present in the log:

      ```bash
      for key in "app id" runner prefix "z drive" webview2 exe schemes \
                 "app version" "display name" profile launcher verify pin result; do
        grep -q "^\[setup\] $key:" "$HOME/wb-release-setup-pinned.log" ||
          echo "MISSING: $key"
      done
      ```

      Expected: no `MISSING` lines.

- [ ] Profile schema and identity:

      ```bash
      test -f "$PROFILE"
      winbridge inspect "$APP" | tee "$HOME/wb-release-profile.toml"
      grep -E '^(schema|id|runner|exe|schemes)' "$HOME/wb-release-profile.toml"
      grep -A3 '^\[pin\]' "$HOME/wb-release-profile.toml"
      ```

      Expected: `schema = 1`, the printed id, `runner = "soda@11.0-10"`,
      the guest exe path, `schemes = [...]` containing `winbridge-fixture`, and
      a `[pin]` table with a non-empty `match`.

- [ ] The scheme came from HKCU, not only a machine hive. The fixture uses a
      current-user NSIS install:

      ```bash
      grep -n 'winbridge-fixture' "$PREFIX/user.reg" | head
      test ! -e "$PREFIX/dosdevices/z:" && test ! -L "$PREFIX/dosdevices/z:"
      ```

      Expected: the scheme appears in `user.reg` under
      `Software\\Classes\\winbridge-fixture` (or the WOW6432Node equivalent),
      proving the per-user detection path. `dosdevices/z:` is absent.

- [ ] Independent doctor view of the new app:

      ```bash
      winbridge doctor "$APP" | tee "$HOME/wb-release-doctor-setup.log"
      ```

      Expected: `[doctor] ok webview2: WebView2 runtime ...`, `[doctor] ok
      prefix: ... has system.reg and drive_c`, and `[doctor] ok
      launcher.entry: ...`. `launcher.scheme` is expected to be a **warn** here
      because the fixture records a scheme but `login-bridge install` has not
      run yet; it should say the handler entry is missing and point to
      `winbridge login-bridge install`. After section 6.2 it must be `ok`. No
      unexplained `fail`.

### 3.2 Pinned WebView2 and the profile conflict guard

Use a second app id so the main app's state stays intact. The pinned path uses
the catalog's checksummed standalone installer:

```bash
winbridge setup "$INSTALLER" --id fixture-pinned --webview2=pinned \
  2>&1 | tee "$HOME/wb-release-setup-pinned.log"
test "${PIPESTATUS[0]}" -eq 0
grep '^\[setup\] webview2:' "$HOME/wb-release-setup-pinned.log"
```

Expected: `[setup] webview2: 156.0.4314.42 (pinned)` (or the catalog version at
release time), and setup succeeds and verifies. The version must match
`winbridge runners list`'s pinned WebView2 line.

- [ ] Pinned WebView2 installs from the catalog payload and prints the pinned
      version.
- [ ] The pinned setup verifies (`PASS ... cursor not checked`).

Re-run without `--force`; no files may change. There is no same-build
`setup --force` check in this checklist: setup requires a changed executable,
so an identical build is rejected even with `--force`. The profile-backup
branch is covered by unit tests, and same-build reinstall is `update --force`.

```bash
winbridge setup "$INSTALLER" --id fixture-pinned --webview2=pinned
status=$?
test "$status" -eq 2
test ! -e "$HOME/.local/share/winbridge/profiles/fixture-pinned.toml.bak"
```

Expected: the re-run exits `2` with a message like `a profile already exists at
...; re-run with --force to replace it (the old profile is backed up)`, and
creates no backup. Identical-build setup is intentionally rejected; use
`update --force` for a same-build reinstall.

- [ ] Re-running setup without `--force` is refused cleanly (exit 2, no change).
- [ ] Identical-build setup is rejected even with `--force`; same-build
  reinstall belongs to `update --force`.

Confirm the pinned app is healthy, then remove it so the rest of the run only
has the main app. `unbridge` removes the profile and extracted icon together:

```bash
winbridge doctor fixture-pinned
winbridge unbridge fixture-pinned --yes
test ! -e "$HOME/.local/share/winbridge/prefixes/fixture-pinned"
test ! -e "$HOME/.local/share/winbridge/profiles/fixture-pinned.toml"
test ! -e "$HOME/.local/share/applications/winbridge-fixture-pinned.png"
```

- [ ] The pinned app verifies and unbridges cleanly.
- [ ] `unbridge` removes the profile and extracted icon.

### 3.3 Manual fallback and the Z: opt-out

The fixture's NSIS installer supports silent flags, so force a failed silent
probe with a drive that does not exist in the prefix, then let `--manual` start
the GUI installer. `--keep-z-drive` is added here only to exercise the
documented opt-out; the main app already proved the default strip. Do not use
`Z:` for the probe: `--keep-z-drive` makes it a valid drive, so the silent
attempt could succeed and skip the manual branch. The doubled backslash inside
the single-quoted argument is deliberate: `shlex.split` collapses `\\` to the
single Windows separator the installer receives.

```bash
winbridge setup "$INSTALLER" --id fixture-manual --manual --keep-z-drive \
  --silent-args '/S /D=Q:\\winbridge-manual-probe' \
  --no-verify 2>&1 | tee "$HOME/wb-release-setup-manual.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected:

- The silent attempt fails (`install attempt: ... exited ...` or exits 0 with no
  new executable).
- `[setup] install: silent attempts failed; starting the GUI installer` is
  printed.
- The NSIS GUI appears. Complete it as a human (default install path), then
  setup continues.
- `[setup] z drive: kept (--keep-z-drive)` is printed and
  `test -L "$HOME/.local/share/winbridge/prefixes/fixture-manual/dosdevices/z:"`
  succeeds.
- Setup exits 0 and reports `[setup] result: not verified`, with
  `[setup] verify: skipped (--no-verify)`. That is the expected `--no-verify`
  result; do not expect `result: pass` here.

- [ ] `--manual` falls back to the GUI installer after silent probes fail.
- [ ] `--keep-z-drive` is the only path that keeps `Z:`; the default app has no
      `Z:` link.

Clean up the manual app now; it has no schemes registered beyond the profile:

```bash
winbridge unbridge fixture-manual --yes
test ! -e "$HOME/.local/share/winbridge/prefixes/fixture-manual"
```

## 4. Render and verify

The setup's first verification is the cold-start measurement. If the machine
was already warm, reboot once and repeat this section; record which run was
cold.

```bash
time winbridge verify "$APP" --timeout 300 2>&1 | tee "$HOME/wb-release-verify.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected:

```text
[verify] app: <id>
[verify] app version: 0.1.0
[verify] runner: soda@11.0-10
[verify] desktop: hyprland
[verify] window class: <class>
[verify] first paint: 4.21s
[verify] evidence: ~/.local/share/winbridge/evidence/<id>/<version>/<timestamp>
[verify] frames: 3
[verify] result: PASS (...)
[verify] cursor not checked
```

- [ ] The app paints within 300 s on a cold start; record the first-paint time.
- [ ] `verify` exits 0 and prints `PASS`.
- [ ] Evidence frames are retained under `~/.local/share/winbridge/evidence/`.
- [ ] The output contains the literal `cursor not checked`.
- [ ] A human opens every retained frame and confirms it is not blank and shows
      the expected fixture UI. Record the app version from `[verify] app
      version` or `winbridge inspect`, not from the frame; file size alone is
      not proof.

```bash
EVIDENCE=$(sed -n 's/^\[verify\] evidence: //p' "$HOME/wb-release-verify.log" | tail -1)
CLASS=$(sed -n 's/^\[verify\] window class: //p' "$HOME/wb-release-verify.log" | tail -1)
test -d "$EVIDENCE"
test -n "$CLASS"
ls -l "$EVIDENCE"
for frame in "$EVIDENCE"/*.png; do xdg-open "$frame"; done
```

- [ ] Cursor behavior is recorded. Launch with `winbridge run "$APP"`, move the
      cursor over the window and into the fixture content, and record visible
      or invisible with the runner name. If invisible, confirm the README,
      [SUPPORT.md](SUPPORT.md), and CHANGELOG list it as a known limitation and
      that the app remains usable. Do not mark the release failed for the
      documented cursor behavior, but do record it.

### 4.1 Cursor force fallback (opt-in, X11/XWayland)

Only for an X11 session or an XWayland display (`test -n "$DISPLAY"`):

```bash
winbridge run "$APP" --cursor=force
tail -n 20 "$HOME/.local/share/winbridge/logs/run-$APP.log"
```

- [ ] `run --cursor=force` exits 0 and the app still paints.
- [ ] The pointer is a visible arrow over the app window, including over text
      fields and links where the app requested an I-beam or link cursor; record
      that the fallback replaces those context cursors with the arrow.
- [ ] The run log contains `[cursor] force helper started (pid N)` and
      `[cursor] force active`.
- [ ] While the first helper is active, start a second `run "$APP"
      --cursor=force`; its log contains `[cursor] already active` and
      `pgrep -af 'winbridge.cursor_helper'` still shows one helper.
- [ ] If the prefix has processes before any matching X11 window appears, the
      log contains `[cursor] force waiting: no matching X11 window yet` once,
      not a false `force active`.
- [ ] Close the app. Once the prefix has no Wine processes left (allow a few
      seconds for `wineserver`), the helper exits:
      `pgrep -af 'winbridge.cursor_helper'` returns nothing and the run log
      contains `[cursor] force stopped`.
- [ ] A normal `winbridge run "$APP"` still has no helper and no cursor change,
      and `winbridge verify "$APP"` still prints `cursor not checked`.
- [ ] The check was performed without `--cursor=force` being needed as a
      substitute for a verified runner; record any cursor gaps that remain.

### 4.2 `run` must not kill, `--restart` is the only kill

```bash
winbridge run "$APP"
test $? -eq 0
# Expected stdout: "already running (pids ...); use --restart"
```

Ensure the app is still running from the cursor check, then capture its PIDs
before and after a plain `run`. The fixture process name is
`WinBridge Fixture.exe`; adjust if the build uses a different product name:

```bash
pids() { ps -eo pid=,args= | grep '[W]inBridge Fixture' | awk '{print $1}' | sort; }
before=$(pids)
winbridge run "$APP"
after=$(pids)
test "$before" = "$after"
```

- [ ] `run` on a running app prints `already running (pids ...); use --restart`,
      exits 0, and leaves every PID unchanged. It must not stack a second
      instance.

```bash
before=$(pids)
winbridge run "$APP" --restart
for attempt in $(seq 1 30); do
  after=$(pids)
  if [ -n "$after" ] && [ "$after" != "$before" ]; then break; fi
  sleep 1
done
test -n "$after"
test "$before" != "$after"

hyprctl clients -j > /tmp/wb-release-clients.json
python - "$CLASS" <<'PY'
import json, sys
clients = json.load(open("/tmp/wb-release-clients.json"))
matches = [window for window in clients if window.get("class") == sys.argv[1]]
print("matching windows:", len(matches))
raise SystemExit(0 if len(matches) == 1 else 1)
PY
```

- [ ] `run --restart` is the only path that kills; the PID set changes and the
      window count for `$CLASS` is exactly one (the loop waits up to 30 s and
      the JSON assertion proves there is no duplicate window). Note that
      `verify`, `update`, and `setup` also kill because they own the prefix;
      that is documented and not a violation of the click-safety rule.

### 4.3 `run --wait` proves paint

```bash
winbridge run "$APP" --restart --wait --timeout 300
test $? -eq 0
# Expected: stop/launch/window/paint stages and "result: PASS"
```

- [ ] `run --restart --wait` exits 0 only after a visible, painted window; the
      stage lines show stop, launch, window found, and paint stable.
- [ ] On a live instance, plain `run` reports the blocking PIDs; `run --wait`
      does not claim that instance painted (exit 4, `already-running`).

```bash
winbridge run "$APP" --restart --wait --timeout 300 --json \
  | tee /tmp/wb-run-events.ndjson \
  | python -c 'import json,sys; [json.loads(line) for line in sys.stdin]'
```

- [ ] `--json` emits only parseable NDJSON; the final event is `result` with
      `status: pass` and an evidence path.
- [ ] Plain `run` under a pipe (`| tail -n 5`) returns without waiting for the
      app to exit; app output is in the per-app run log, not the caller's pipe.

## 5. Desktop integration

### 5.1 Launcher

```bash
desktop-file-validate "$LAUNCHER"
echo "desktop-file-validate exit=$?"
grep -E '^(Exec|Icon|StartupWMClass|MimeType)=' "$LAUNCHER"
```

Expected: `desktop-file-validate` exits 0 (warnings, if any, are advisory);
`Exec=` is an absolute command (the installed `winbridge` path, or the
interpreter running `-m winbridge`) followed by `run $APP`; a `StartupWMClass=`
is present after verification; there is no `MimeType=` in the main entry.

- [ ] Close the app, then confirm the launcher entry starts it (use `gtk-launch
      "winbridge-$APP"` or click it in the app menu). Expected: one window
      appears. A launcher click while the app is already running refuses to
      stack (section 4.2); focus-on-re-click belongs to the taskbar pin below.
- [ ] The launcher does not rely on the caller's `PATH`; `Exec` is absolute.

### 5.2 Pin appears, launches, focuses on re-click, survives shell restart

Inspect the live taskbar config. This reads every bar section and prints the
matching entry:

```bash
python - "$APP" <<'PY'
import json, pathlib, sys
app = sys.argv[1]
path = pathlib.Path.home() / ".config/omarchy/shell.json"
doc = json.loads(path.read_text())
for section, widgets in doc.get("bar", {}).items():
    if not isinstance(widgets, list):
        continue
    for widget in widgets:
        if isinstance(widget, dict) and widget.get("id") == "io.github.joeyvigil.taskbar":
            for entry in widget.get("apps", []):
                if isinstance(entry, dict) and entry.get("desktopId") == f"winbridge-{app}":
                    print(section, entry)
PY
```

Expected: one line, preferably in `left`, with a **bare** `desktopId`
(`winbridge-<id>`, no `.desktop`) and a non-empty `match` regex derived from the
verified window class.

- [ ] The pin is visible in the omarchy-shell taskbar.
- [ ] Clicking the pin launches the app when it is not running.
- [ ] With the app running, clicking the pin focuses the existing window and
      does not launch a second instance. Check with
      `hyprctl activewindow -j` and the PID set; the active class must match the
      verified class.
- [ ] `omarchy restart shell`, wait for the bar to return, and confirm the pin
      is still present and still focuses/launches. The pin lives in
      `~/.config/omarchy/shell.json`; a shell restart must not lose it.

```bash
omarchy restart shell
sleep 5
# Repeat the JSON check above; the entry must still be present.
```

## 6. Files and login bridge

### 6.1 Expose

```bash
winbridge expose "$APP" "$HOST_FOLDER" 2>&1 | tee "$HOME/wb-release-expose.log"
test "${PIPESTATUS[0]}" -eq 0
find "$PREFIX/drive_c/users" -maxdepth 2 -type l -name 'winbridge-release-folder' -ls
```

Expected: `exposed /home/.../winbridge-release-folder -> C:\users\<guest-user>\
winbridge-release-folder`; exactly one symlink under the guest user directory
points at the host folder.

Prove the guest can see the host folder. Use the app's file picker if the build
has one; the synthetic fixture has no picker, so use the same runner's
`cmd.exe` inside the prefix:

```bash
RUNNER_WINE=$(find "$HOME/.local/share/winbridge/runners" -type f -path '*/bin/wine' | head -n1)
test -x "$RUNNER_WINE"
WINEPREFIX="$PREFIX" WINEDEBUG=-all "$RUNNER_WINE" cmd /c dir \
  'C:\users\<guest-user>\winbridge-release-folder'
```

Expected: the listing contains `marker.txt`. This is the same guest filesystem
the app uses; a real app with a file dialog must also see the folder.

- [ ] The host folder is visible inside the guest (file picker or guest `cmd`).

Do **not** unexpose yet. The update and runner-rebuild checks in section 7 must
prove the exposure survives; `unexpose` is validated at the end of that section.

### 6.2 Login handler and callback

```bash
winbridge login-bridge install "$APP"
winbridge login-bridge status "$APP"
winbridge login-bridge status "$APP" | awk -F '\t' '{print $1 " | " $2 " | " $3}'
xdg-mime query default x-scheme-handler/winbridge-fixture
grep -E '^(Exec|MimeType|NoDisplay)=' "$HANDLER"
```

Expected:

- `x-scheme-handler/winbridge-fixture -> winbridge-<id>-handler.desktop`;
- `experimental: forwarding under Wine is unproven`;
- `status` is tab-separated (`SCHEME<TAB>DEFAULT HANDLER<TAB>STATUS`), shown
  here as `winbridge-fixture | winbridge-<id>-handler.desktop | ok`;
- `xdg-mime query` returns `winbridge-<id>-handler.desktop`;
- the handler entry has `NoDisplay=true`,
  `MimeType=x-scheme-handler/winbridge-fixture;`, and an absolute
  `winbridge`/interpreter command followed by `open-url <id> %u`.

- [ ] `login-bridge install` writes the handler entry and registers the scheme.
- [ ] `login-bridge status` reports our handler as the default.
- [ ] A callback reaches the running app, or the failure is recorded as the
      documented experimental limitation. Use a fake token; never a real one:

      ```bash
      winbridge run "$APP"
      timeout 20 xdg-open 'winbridge-fixture://release-check?code=release-test-token'
      echo "xdg-open exit=$?"
      winbridge open-url "$APP" 'winbridge-fixture://release-check?code=release-test-token'
      ```

      Expected: `xdg-open` exits 0 and invokes the handler; `open-url` prints
      `forwarded winbridge-fixture://release-check/[redacted] to <id>` and
      exits 0. The app must not restart; compare PIDs. If the fixture has no
      deep-link UI, record "forwarded; app has no callback UI; experimental as
      documented" rather than inventing a pass. A missing handler or a crash is
      a FAIL.

- [ ] Callback tokens are not logged:

      ```bash
      if grep -R -F 'release-test-token' \
           "$HOME/.local/share/winbridge/logs" \
           "$HOME/.local/share/winbridge/evidence" 2>/dev/null; then
        echo "TOKEN LEAKED"
      else
        echo "token not present in logs"
      fi
      ```

      Expected: `token not present in logs`. The test token is fake, but this
      check catches accidental URL logging.

## 7. Update and runner pinning

### 7.1 Update in place from the newest installer

Record the profile before updating:

```bash
winbridge inspect "$APP" > "$HOME/wb-release-before-update.toml"
cp "$INSTALLER" "$HOME/Downloads/winbridge-fixture-setup-0.2.0.exe"
```

Use a genuine newer fixture build when one exists. If only one build exists,
the renamed copy proves installer selection and in-place reinstall but **not**
an application version upgrade; record that limitation explicitly. A
same-build installer rewrites identical executable bytes, so the default
update rejects it by design. For that case use `winbridge update "$APP"
--force`; it must warn that no executable change was detected and still fail
if the recorded exe is missing.

With a genuine newer build:

```bash
winbridge update "$APP" 2>&1 | tee "$HOME/wb-release-update.log"
test "${PIPESTATUS[0]}" -eq 0
winbridge inspect "$APP" > "$HOME/wb-release-after-update.toml"
```

For the same-build path, run the explicit force variant and expect exit 0 plus
`no executable change detected; --force accepted the recorded executable`:

```bash
winbridge update "$APP" --force 2>&1 | tee "$HOME/wb-release-update-force.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected:

- `[update] installer: .../winbridge-fixture-setup-0.2.0.exe (nsis)` (the newest
  match by version, then mtime);
- `[update] prefix:` is the same prefix; the app updates in place, not a new
  prefix;
- `[update] app version:` and `[update] exe:` are printed;
- `[update] verify: PASS (... cursor not checked)` or `skipped` only when the
  desktop cannot verify;
- `update: <id> (<name>) is ready on <runner>`.

- [ ] The newest matching installer is selected.
- [ ] The app updates in place; the prefix path is unchanged.
- [ ] A same-build reinstall fails without `--force`; with `--force` it warns
  that no executable change was detected and still fails if the recorded exe
  is missing.
- [ ] The profile is preserved (`created_at`, schemes, exposed folder, pin).
- [ ] The launcher and pin still work after the update.
- [ ] The exposed folder is still visible after the update.

Compare the before/after profiles and confirm only installer/build/verification
fields changed:

```bash
diff -u "$HOME/wb-release-before-update.toml" "$HOME/wb-release-after-update.toml"
```

### 7.2 Runner pin: record-only vs `--rebuild`

Create a second runner reference by importing the installed store by reference
(nothing is copied):

```bash
RUNNER_ROOT=$(find "$HOME/.local/share/winbridge/runners" -type f -path '*/bin/wine' \
  -printf '%h\n' | sed 's#/bin$##' | head -n1)
test -n "$RUNNER_ROOT"
winbridge runners import "$RUNNER_ROOT"
stat -c '%Y %n' "$PREFIX/system.reg" > "$HOME/wb-release-prefix-mtime.txt"
```

Record-only must not touch the prefix:

```bash
winbridge runners pin "$APP" "path:$RUNNER_ROOT" 2>&1 | tee "$HOME/wb-release-pin-record.log"
test "${PIPESTATUS[0]}" -eq 0
stat -c '%Y %n' "$PREFIX/system.reg" > "$HOME/wb-release-prefix-mtime-after.txt"
diff -u "$HOME/wb-release-prefix-mtime.txt" "$HOME/wb-release-prefix-mtime-after.txt"
```

Expected: `recorded <runner> for <id> (record-only; the prefix did not change)`
and a warning that the initialized prefix stays tied to the old runner; the
`system.reg` mtime is unchanged; the launcher, pin, handler, and exposed folder
are untouched.

- [ ] Record-only changes the profile reference only; the prefix is untouched.
- [ ] Record-only preserves launcher, pin, schemes, and exposed folders.

Rebuild under the default runner. This is the only automatic path that discards
a prefix; use `--yes` only in this disposable test:

```bash
winbridge runners pin "$APP" soda@11.0-10 --rebuild --yes \
  2>&1 | tee "$HOME/wb-release-pin-rebuild.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected:

- `[pin] runner: ... -> soda@11.0-10 (rebuild)`;
- prefix is removed and recreated, `Z:` stripped, WebView2 installed, the
  recorded installer reinstalled;
- `[pin] last verified: cleared (the prefix was recreated)`;
- `[pin] expose: winbridge-release-folder -> ...` recreates the exposed link;
- `rebuilt <id> under soda@11.0-10`.

- [ ] `--rebuild` recreates the prefix under the requested runner.
- [ ] It preserves the launcher, pin, schemes, and exposed host folder.
- [ ] It clears `last_verified`; run `winbridge verify "$APP"` again and require
      PASS before continuing.
- [ ] The rebuilt prefix still has no `Z:` link
      (`test ! -e "$PREFIX/dosdevices/z:" && test ! -L "$PREFIX/dosdevices/z:"`);
      the main app did not use `--keep-z-drive`.

After the rebuild, confirm the handler and exposure survived, then unexpose:

```bash
winbridge login-bridge status "$APP"   # still ok
find "$PREFIX/drive_c/users" -maxdepth 2 -type l -name 'winbridge-release-folder' -ls
winbridge unexpose "$APP" "$HOST_FOLDER"
test ! -e "$PREFIX/drive_c/users/<guest-user>/winbridge-release-folder"
test -f "$HOST_FOLDER/marker.txt"
```

- [ ] Schemes and the exposed folder survive a rebuild.
- [ ] `unexpose` removes only WinBridge's link; the host folder is intact.

## 8. Doctor and redacted bundle

```bash
winbridge doctor 2>&1 | tee "$HOME/wb-release-doctor-env.log"
winbridge doctor "$APP" --bundle 2>&1 | tee "$HOME/wb-release-doctor-app.log"
```

Expected environment checks (all `ok`, or a documented `warn` with an
explanation): `python`, `python.deps`, `config`, `paths.xdg`,
`session.adapter`, `desktop.screenshot`, `runners.store`, `prefix`, `webview2`,
`webview2.pin`, `launcher.entry`, `launcher.scheme`, `disk`, `locks`. The app
run additionally reports the prefix, WebView2 version, launcher, and scheme
registration as `ok`.

- [ ] `winbridge doctor` reports no unexplained failures.
- [ ] `winbridge doctor "$APP"` reports no unexplained failures and prints the
      WebView2 version and runner store status.
- [ ] `--bundle` writes a `*.tar.gz` under
      `~/.local/share/winbridge/bundles/`, never the current directory.

Verify the bundle is owner-only and redacted:

```bash
BUNDLE=$(sed -n 's/^\[doctor\] bundle: //p' "$HOME/wb-release-doctor-app.log" | tail -1)
test -f "$BUNDLE"
stat -c '%a %n' "$BUNDLE"          # expected 600
tar -tzf "$BUNDLE"                 # expected manifest.json plus sanitized logs
tar -xOzf "$BUNDLE" manifest.json | python -m json.tool >/dev/null
if tar -xOzf "$BUNDLE" | grep -F "$HOME"; then echo "HOME PATH LEAKED"; fi
if tar -xOzf "$BUNDLE" | grep -F 'release-test-token'; then echo "TOKEN LEAKED"; fi
```

Expected: mode `600`, valid `manifest.json`, no home path, and no callback
token. If the bundle contains vendor content, redact it and record a FAIL; never
attach it to a public issue.

## 9. Unbridge and residue

First confirm the refusal path, then remove the main app:

```bash
printf 'n\n' | winbridge unbridge "$APP"
test $? -eq 1
# Expected: confirmation prompt, aborted, no files removed.

winbridge unbridge "$APP" --yes 2>&1 | tee "$HOME/wb-release-unbridge.log"
test "${PIPESTATUS[0]}" -eq 0
```

Expected: `unbridged <id>`; no traceback. The profile (and its `--force`
backup), prefix, launcher, extracted icon, scheme handler, taskbar pin, and
scheme default are removed. The exposed host folder is not touched.

- [ ] Without `--yes`, the command prompts and aborts with no side effects.
- [ ] `--yes` removes the prefix and profile, including `<id>.toml.bak`.
- [ ] It removes the launcher, extracted icon, and handler entries.
- [ ] It removes the bare `winbridge-<id>` pin from the taskbar.
- [ ] It removes the scheme default; `xdg-mime query` no longer returns our
      handler.
- [ ] It removes our lines from `~/.config/mimeapps.list`.
- [ ] The host folder and marker file still exist.
- [ ] No app process remains.

Verification commands:

```bash
test ! -e "$PREFIX"
test ! -e "$PROFILE"
test ! -e "$PROFILE.bak"
test ! -e "$LAUNCHER"
test ! -e "$HANDLER"
test ! -e "$HOME/.local/share/applications/winbridge-$APP.png"
find "$HOME/.local/share/applications" -maxdepth 1 -name "winbridge-$APP*" -print
grep -n "winbridge-$APP" "$HOME/.config/mimeapps.list" 2>/dev/null || true
xdg-mime query default x-scheme-handler/winbridge-fixture
test -f "$HOST_FOLDER/marker.txt"
```

Expected: no `winbridge-$APP*` files in the applications directory, no
`winbridge-$APP` lines in `mimeapps.list`, and `xdg-mime` no longer returns our
handler. `unbridge` removes the exact app-owned profile backup
(`<id>.toml.bak`) and extracted icon (`winbridge-<id>.png`) as well; both
`test ! -e` lines must pass.

Idempotence:

```bash
winbridge unbridge "$APP" --yes
test $? -eq 0
```

Expected: exit 0; warnings such as `no prefix`, `no profile`, `no launcher`,
`no scheme handler`, or `no pin` are acceptable; no traceback and no new files.

- [ ] A second `unbridge` is a no-op and exits 0.
- [ ] No host files were deleted.

### 9.1 Residue audit

Unbridge is allowed to keep shared state (runner store, WebView2 cache, global
logs, evidence, diagnostic bundles, and locks). It is not allowed to keep
per-app integration artifacts. Inspect:

```bash
find "$HOME/.local/share/winbridge/profiles" -maxdepth 1 -name '*.toml*' -print
find "$HOME/.local/share/winbridge/prefixes" -maxdepth 1 -mindepth 1 -print
find "$HOME/.local/share/applications" -maxdepth 1 -name 'winbridge-*' -print
find "$HOME/.local/share/winbridge/evidence" -maxdepth 1 -mindepth 1 -type d -print
find "$HOME/.local/share/winbridge/bundles" -maxdepth 1 -type f -print
find "$HOME/.local/share/winbridge/locks" -maxdepth 1 -name 'app-*.lock' -print
```

Expected after unbridging every test app:

- no `profiles/<id>.toml` or `profiles/<id>.toml.bak`;
- no `prefixes/<id>`;
- no `winbridge-<id>.desktop`, `winbridge-<id>-handler.desktop`, or
  `winbridge-<id>.png`;
- evidence directories, diagnostic bundles, and lock files may remain, but
  record them. Evidence and bundles are retained for diagnostics per
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md); lock files are retained so `doctor`
  can report stale locks. They are not runtime integration residue.

`shell.json.bak`, evidence directories, and diagnostic bundles may remain by
design; record their paths but do not delete the user's shell config backup. The
exact app-owned profile backup (`<id>.toml.bak`) and extracted icon
(`winbridge-<id>.png`) are not retained; af3f31f made `unbridge` remove both,
and the assertions above must pass.

## 10. Pre-tag docs promotion and history sanitization

Read [PUBLISHING.md](PUBLISHING.md) in full; it is the authority. The current
git history is not public-safe, and the tag must be created on the sanitized
history, not on the old one.

### 10.1 Promote README status and CHANGELOG (pre-tag)

This is part of the tag gate. Promote the README **status** and the CHANGELOG to
`0.1.0`, but leave the README Install section saying the release is not
published: AUR/PyPI availability is only true after 11.2/11.3 and is flipped in
11.4. The tag commit must already contain the status/CHANGELOG promotion.

Edit, do not blindly overwrite:

- `README.md` status: `v0.1.0 (release pending)` becomes `v0.1.0`.
- `README.md` Install: leave the “not published yet” wording and the clone
  instructions untouched until 11.4. Do not claim AUR/PyPI availability here.
- `CHANGELOG.md`: replace the `[Unreleased]` preamble with
  `## [0.1.0] - <actual date>`, keep the Added/Notes content, and add a fresh
  `## [Unreleased]` section at the top. If the “nothing is available yet”
  sentence would be false only after PyPI/AUR publication, keep it until 11.4 or
  phrase it as publication following the tag. Update the comparison links for
  the new tag.
- Keep the known limitations (cursor, login experimental, Proton, X11,
  non-Hyprland verify degradation) accurate.

```bash
$EDITOR README.md CHANGELOG.md
git diff --check
git diff README.md CHANGELOG.md
git add README.md CHANGELOG.md
git commit -m "docs: add v0.1.0 release notes"
```

Expected: the diff contains the README status/date promotion and the CHANGELOG
`## [0.1.0]` section; README Install still says “not published yet”; no
product-behavior claims change.

- [ ] README status is `v0.1.0`; Install still says “not published yet”.
- [ ] CHANGELOG has `## [0.1.0] - <date>` and accurate publication wording.
- [ ] Known limitations are still accurate.

### 10.2 Support matrix and known limitations (pre-scan)

This is a **pre-tag gate** and must finish before 10.3/10.4: any doc edit here
changes the commit that 10.4 scans. Verify the docs match the behavior observed
above. The README, SUPPORT, and CHANGELOG must agree; "verified" means this
checklist was actually executed on Omarchy/Hyprland. The checked-in docs say the
Hyprland row is a **release-checklist target — pending verification**. Until
this run is recorded, do not publish or tag on the strength of a "verified"
label. If this run completes successfully, change the row (and any matching
CHANGELOG wording) to "verified", record the evidence in the release notes, and
commit that edit as part of 10.2 before the sanitized scan.

- [ ] `README.md` and [SUPPORT.md](SUPPORT.md) agree: Hyprland is initially the
      release-checklist target (pending verification), X11 is experimental,
      other Arch desktops are experimental, other Wayland compositors are
      unsupported for `verify`, and Flatpak is unsupported. Flip Hyprland to
      "verified" only after every item in this checklist passes.
- [ ] The known limitations list cursor invisibility, experimental login
      forwarding, Proton/GE-Proton unsupported, X11 experimental, and
      non-Hyprland `verify` degradation.
- [ ] On a non-Hyprland session, record the actual degradation. `setup` and
      `update` should record `skipped` and continue; a direct `verify` on a
      desktop with no adapter exits `3` with `desktop not supported` rather than
      pretending to pass. Do not claim a pass on a session that was not tested.
- [ ] `winbridge runners list` contains no Proton/GE-Proton entries; only
      wine-kind runners are supported:

      ```bash
      winbridge runners list | grep -i proton \
        && echo "PROTON ENTRY FOUND" || echo "no proton runners"
      ```

- [ ] Inspect the exact labels rather than trusting the summary:

      ```bash
      grep -nE 'verified|experimental|unsupported' README.md docs/SUPPORT.md
      grep -nE 'cursor|Proton|login|X11|Wayland' README.md docs/SUPPORT.md CHANGELOG.md
      ```

      Expected: Hyprland/Omarchy is the only target that may be called verified
      once this run is recorded; X11, other Arch desktops, and
      Debian/Fedora/Ubuntu are experimental; other Wayland compositors and
      Flatpak are unsupported for the stated scope. Until then the row must say
      pending verification.

- [ ] If any behavioral claim changed during this run, update the docs **now**
      and commit the edit (amend the 10.1 commit or add a follow-up) before
      continuing to 10.3, so the sanitized scan covers it.
- [ ] The worktree is clean before 10.3:
      `test -z "$(git status --short)"`.
- [ ] Every support-matrix and known-limitation item above is PASS or explicitly
      documented; this gate is part of the tag gate.

Known limitations are not release failures only when they are documented and the
app remains usable. Record each one with the runner, WebView2 version, and
observed behavior.

### 10.3 Sanitize the tree and rewrite history

- [ ] The whole tree contains no vendor names, app versions, screenshots,
      installers, tokens, hardware IDs, or sign-in details.
- [ ] No vendor artifacts are tracked:

      ```bash
      git ls-files | grep -Ei '\.(exe|msi|deb|AppImage|dmg|log|bak)$' \
        && echo "VENDOR ARTIFACT FOUND" || echo "no installer/log artifacts"
      git ls-files | grep -Ei '(^|/)(dist|build|target)/' \
        && echo "BUILD OUTPUT TRACKED" || echo "no build output tracked"
      git ls-files '*.png' '*.jpg' '*.jpeg' '*.webp'
      ```

      Expected: no installer/log artifacts and no build output. The only images
      may be the fixture's own generic icons under
      `fixtures/synthetic-tauri-app/src-tauri/icons/`; anything else needs
      review.

- [ ] No stored PyPI credentials:

      ```bash
      git grep -nE 'PYPI_API_TOKEN|TWINE_PASSWORD|password:' -- .github \
        && echo "STORED SECRET" || echo "no stored PyPI secret"
      ```

      Expected: no stored secret; the release workflow uses OIDC trusted
      publishing and the `pypi` environment.

- [ ] Rewrite history or start a fresh repository with one sanitized base
      commit, per PUBLISHING.md. This comes **after** 10.1 and 10.2 so the docs
      promotion/support edits are part of the sanitized history. Preserve the
      old history only in a private
      archive. If the public remote already contains unsanitized history,
      delete/recreate the repository or force-push the sanitized history and
      verify old objects are unreachable; do not leave the old history
      accessible.

- [ ] External settings are ready (check the web UI where there is no CLI):
      - PyPI pending trusted publisher: owner `TYRF1`, repository
        `winbridge`, workflow `release.yml`, environment `pypi`. No API token.
      - GitHub: repo description/topics match the support matrix; private
        vulnerability reporting enabled; issues enabled; `main` protected with:
        PR required, `lint, types, unit tests` required, force pushes and
        deletions blocked, push/merge restricted to `TYRF1` (fork PRs welcome).
      - AUR: package name `winbridge` is available or owned by this account;
        SSH key registered (`ssh aur@aur.archlinux.org help`).
      - `pyproject.toml` URLs and `LICENSE` are correct.

      ```bash
      gh repo view TYRF1/winbridge --json description,repositoryTopics,visibility
      gh api repos/TYRF1/winbridge/private-vulnerability-reporting
      gh api repos/TYRF1/winbridge/branches/main/protection
      ssh aur@aur.archlinux.org help
      ```

      Expected: public repository, matching description/topics, vulnerability
      reporting enabled, branch protection present, and AUR SSH access.

### 10.4 Pre-push scan of the sanitized clone

This is the **pre-push** scan. It runs after 10.3's rewrite/export, against a
clone of the exact sanitized history. It is mandatory even when the history is a
fresh single commit; the tree scan alone is not enough. After the protected
merge in 11.1, the final merged `main` is scanned again and becomes the only
`SCANNED_HEAD` allowed to receive the tag.

```bash
set -euo pipefail

normalize_github_url() {
  local url="${1%.git}"
  case "$url" in
    git@github.com:TYRF1/winbridge) printf '%s\n' github.com/TYRF1/winbridge ;;
    ssh://git@github.com/TYRF1/winbridge) printf '%s\n' github.com/TYRF1/winbridge ;;
    https://github.com/TYRF1/winbridge) printf '%s\n' github.com/TYRF1/winbridge ;;
    *) return 1 ;;
  esac
}

check_origin() {
  local fetch_url push_url
  fetch_url=$(git remote get-url origin) || exit 2
  push_url=$(git remote get-url --push origin) || exit 2
  test "$(normalize_github_url "$fetch_url")" = "github.com/TYRF1/winbridge" \
    || { echo "unexpected fetch origin: $fetch_url" >&2; exit 2; }
  test "$(normalize_github_url "$push_url")" = "github.com/TYRF1/winbridge" \
    || { echo "unexpected push origin: $push_url" >&2; exit 2; }
}

RELEASE_WORKTREE=$(git rev-parse --show-toplevel)
cd "$RELEASE_WORKTREE"
git bundle create /tmp/winbridge-sanitized.bundle HEAD
rm -rf /tmp/winbridge-sanitized
git clone /tmp/winbridge-sanitized.bundle /tmp/winbridge-sanitized

SCANNED_HEAD=$(git -C /tmp/winbridge-sanitized rev-parse HEAD)
git -C /tmp/winbridge-sanitized log --oneline --decorate
git -C /tmp/winbridge-sanitized rev-list --all --count

# Run every scan inside the clone, then leave it exactly once.
(
  cd /tmp/winbridge-sanitized
  uvx detect-secrets scan --all-files > /tmp/winbridge-secrets.json
  python - <<'PY'
import json
data = json.load(open("/tmp/winbridge-secrets.json"))
print("results:", len(data.get("results", {})))
PY
  gitleaks detect --source . --redact --no-banner
)

# All release mutations happen back in the worktree, never the scan clone.
cd "$RELEASE_WORKTREE"
test "$(git rev-parse HEAD)" = "$SCANNED_HEAD" || exit 2
check_origin
```

Expected: only the sanitized release commit(s), no pre-sanitization SHAs, a
clean tree scan, and `gitleaks` reports no leaks. The final `test` proves the
worktree HEAD is the pre-push scanned HEAD; `check_origin` normalizes both fetch
and push URLs (optional `.git`, `https://` or `ssh://git@`) to
`github.com/TYRF1/winbridge` and requires an exact match, so forks and
spoofs fail closed. Record the commit count. If an old commit or a leak appears,
stop and fix 10.1–10.3; do not tag. Keep this shell open: 11.1 uses
`$RELEASE_WORKTREE`, `$SCANNED_HEAD`, and `check_origin`; 11.3 and 11.4 use
`$RELEASE_WORKTREE`.

- [ ] The sanitized clone contains only sanitized commits.
- [ ] `detect-secrets` and mandatory `gitleaks` both report clean.
- [ ] The worktree HEAD equals the pre-push scanned clone HEAD.
- [ ] Fetch and push URLs are exactly the WinBridge GitHub remote.

## 11. Tag, PyPI, AUR, and post-publish installs

Entry gate: sections 2–10 must pass, including the support-matrix and
known-limitations checks in 10.2 and the pre-push scanned-head equality in 10.4.
11.1 then repeats the scan on the final merged `main` commit; the tag is allowed
only after that final scan. Record the release date; do not copy the old target
date blindly.

### 11.1 Final merged-head scan and tag

```bash
set -euo pipefail

cd "$RELEASE_WORKTREE"
grep -n '^version = "0.1.0"' pyproject.toml
grep -n '__version__ = "0.1.0"' src/winbridge/__init__.py
test -z "$(git status --short)" || exit 2
test -z "$(git tag --list 'v0.1.0')" || exit 2
```

Expected: versions agree, the worktree is clean, and no `v0.1.0` tag exists.
Confirm every pre-push gate before continuing:

- [ ] 10.1's README status/CHANGELOG promotion is in the sanitized history;
      README Install still says “not published yet”.
- [ ] 10.2's support-matrix and known-limitations gate is PASS.
- [ ] 10.3's history is sanitized and 10.4's pre-push scan is clean.
- [ ] `git rev-parse HEAD` equals `$SCANNED_HEAD` and `check_origin` passes
      before the push.

A FAIL in any of these blocks the tag even if every command in sections 2–10
passed.

Push or merge the sanitized history to `main`:

```bash
# Fast-forward path (only when main is not protected):
git push origin HEAD:main
```

If `main` is protected, open a pull request from the sanitized history, merge it
through the protected path, and do **not** run the direct push above or tag yet.
A protected merge/squash creates a new commit that was not in the 10.4 scan.

Whether the push was fast-forward or a protected merge, fetch the final
`main` and scan that exact merged commit before tagging:

```bash
set -euo pipefail

cd "$RELEASE_WORKTREE"
git fetch origin main
git switch --detach origin/main

SCANNED_HEAD=$(git rev-parse HEAD)
test -n "$SCANNED_HEAD" || exit 2
test "$(git rev-parse HEAD)" = "$SCANNED_HEAD" || exit 2
check_origin

uvx detect-secrets scan --all-files > /tmp/winbridge-secrets.json
python - <<'PY'
import json
data = json.load(open("/tmp/winbridge-secrets.json"))
print("results:", len(data.get("results", {})))
PY
gitleaks detect --source . --redact --no-banner

test -z "$(git status --short)" || exit 2
test "$(git rev-parse HEAD)" = "$SCANNED_HEAD" || exit 2
```

Expected: `origin/main` is the exact final commit, `SCANNED_HEAD` is reset to
that merged commit, `detect-secrets` and `gitleaks` are clean on it, and the
worktree is clean. A protected merge therefore cannot be tagged until this
second scan passes. If the final `main` differs from the pre-push scan, only this
final scan authorizes the tag.

Tag exactly the final scanned commit and push the tag:

```bash
set -euo pipefail

cd "$RELEASE_WORKTREE"
test "$(git rev-parse HEAD)" = "$SCANNED_HEAD" || exit 2
check_origin
git tag -a -m "WinBridge v0.1.0" v0.1.0 "$SCANNED_HEAD"
git push origin v0.1.0
run_id=$(gh run list --workflow release.yml --limit 1 \
  --json databaseId --jq '.[0].databaseId')
test "$(gh run view "$run_id" --json headSha --jq '.headSha')" = "$SCANNED_HEAD" || exit 2
gh run watch "$run_id" --exit-status
```

Expected: the tag points at `$SCANNED_HEAD` on the final merged `main`; the
`release.yml` run passes `verify`, `build`, and `publish`; PyPI accepts the wheel
and sdist. If the run fails **before** PyPI accepts any file, fix the commit,
delete the tag (local and remote), and repeat the final-main scan and tag; never
move a tag after PyPI has accepted files. After acceptance, a fix is a new patch
version.

- [ ] Tag `v0.1.0` points at the final merged, scanned `main` commit.
- [ ] Release workflow is green and PyPI published.

Leave the release worktree detached at `$SCANNED_HEAD`; do not check out `main`
here. The post-publish AUR and README commits use short-lived release branches
in 11.3/11.4.

### 11.2 Confirm PyPI

```bash
url='https://files.pythonhosted.org/packages/source/w/winbridge/winbridge-0.1.0.tar.gz'
curl -fsSLI "$url" | tail -n 1
test "$(curl -fsSL -o /dev/null -w '%{http_code}' "$url")" = 200
curl -fsSL https://pypi.org/pypi/winbridge/json -o /tmp/winbridge-pypi.json
python - <<'PY'
import json
data = json.load(open("/tmp/winbridge-pypi.json"))
info = data["info"]
print(info["name"], info["version"])
for item in data["urls"]:
    print(item["packagetype"], item["filename"], item["digests"]["sha256"])
PY
```

Expected: final HTTP 200, version `0.1.0`, and both `bdist_wheel` and `sdist`
present. Record the PyPI sdist SHA-256; it must match the AUR checksum.

```bash
uv tool uninstall winbridge 2>/dev/null || true
uv tool install --force 'winbridge==0.1.0'
~/.local/bin/winbridge --version
uvx --from 'winbridge==0.1.0' winbridge --version
```

Expected: `winbridge 0.1.0` from both the installed tool and the disposable
`uvx` environment.

- [ ] PyPI URL returns 200 and exposes the wheel and sdist.
- [ ] `uv tool install winbridge==0.1.0` works.
- [ ] `uvx --from winbridge==0.1.0 winbridge --version` works.

### 11.3 AUR checksum, clean build, and submission

The checksum cannot be committed before the tag because the sdist does not exist
until the release workflow publishes it. Follow
[packaging/aur/README.md](../packaging/aur/README.md).

```bash
cd packaging/aur
updpkgsums
grep '^sha256sums' PKGBUILD
makepkg --printsrcinfo > .SRCINFO
diff -u <(makepkg --printsrcinfo) .SRCINFO
makepkg --cleanbuild --syncdeps --rmdeps
```

`makepkg --syncdeps`, `extra-x86_64-build`, and `pacman -U` may ask for sudo to
install packaging dependencies or the built package. That is the Arch package
manager, not WinBridge; WinBridge itself never elevates. Do not run `winbridge`
itself with sudo.

Expected:

- `updpkgsums` replaces the placeholder with the real 64-hex digest of the PyPI
  sdist; the digest equals the one recorded in 11.2.
- `makepkg --printsrcinfo` output matches `.SRCINFO` exactly (the `diff` is
  empty).
- `makepkg --cleanbuild --syncdeps --rmdeps` builds
  `winbridge-0.1.0-1-any.pkg.tar.zst` without errors.
- No `0000000000000000000000000000000000000000000000000000000000000000`
  placeholder remains.

Clean chroot (devtools):

```bash
extra-x86_64-build
```

Expected: the package builds in the clean x86_64 chroot. If `devtools` is not
installed, install it and rerun; a host-only `makepkg` is not the chroot check.

- [ ] `updpkgsums` writes the real PyPI sdist digest.
- [ ] `.SRCINFO` is regenerated and matches `makepkg --printsrcinfo`.
- [ ] Clean `makepkg` build passes.
- [ ] Clean chroot build passes.

Install the built package and test it (this is the package manager, not
WinBridge; WinBridge itself never elevates):

```bash
uv tool uninstall winbridge 2>/dev/null || true
sudo pacman -U --noconfirm winbridge-0.1.0-1-any.pkg.tar.zst
winbridge --version
pacman -Q winbridge
```

Expected: `winbridge 0.1.0`, `winbridge 0.1.0-1`, and `/usr/bin/winbridge`.
Then submit:

```bash
git clone ssh://aur@aur.archlinux.org/winbridge.git /tmp/aur-winbridge
cp PKGBUILD .SRCINFO /tmp/aur-winbridge/
cd /tmp/aur-winbridge
git add PKGBUILD .SRCINFO
git commit -m "winbridge 0.1.0-1"
git push
```

Expected: the push is accepted and the AUR package page appears. If the AUR
repository does not exist yet for a first submission, create the package in the
AUR web interface first, then clone and push. Never push the placeholder
checksum.

- [ ] AUR package builds from the PyPI sdist, not a git checkout.
- [ ] `PKGBUILD` and `.SRCINFO` are submitted without the placeholder.
- [ ] AUR package installs and `winbridge --version` prints `0.1.0`.

After submission, verify the published AUR source on a clean path:

```bash
rm -rf /tmp/aur-winbridge-check
git clone https://aur.archlinux.org/winbridge.git /tmp/aur-winbridge-check
cd /tmp/aur-winbridge-check
makepkg --printsrcinfo | diff -u - .SRCINFO
makepkg -si
winbridge --version
```

Expected: the submitted metadata matches, the package builds, and the installed
command reports `0.1.0`. This is the post-submission AUR check.

- [ ] AUR clone after submission matches the submitted metadata.
- [ ] AUR install from the published repository works.

Commit the release-time checksum and metadata to this repository on `main`
(after the tag; do not move the tag to include them):

```bash
cd "$RELEASE_WORKTREE"
git fetch origin main
git switch -c release/aur-checksum origin/main
git add packaging/aur/PKGBUILD packaging/aur/.SRCINFO
git commit -m "build: pin aur checksum for v0.1.0"
git push origin HEAD:main
```

This uses a release branch, not a local `main` checkout, because the linked
worktree does not own `main`. If `main` is protected, push
`git push -u origin release/aur-checksum`, open a PR from that branch, and merge
it through the protected path instead of the direct push. Do not move `v0.1.0`
to include this commit.

### 11.4 Mark Install availability (post-publish)

Only after 11.2 (PyPI) and 11.3 (AUR) pass. The README status and CHANGELOG were
already promoted in 10.1; this step changes only the Install availability claim.

- `README.md` Install: AUR and PyPI become the primary install paths; remove the
  "not published yet" framing.
- If `CHANGELOG.md` still says publication follows the tag, change it to the
  published state.
- Keep the support matrix, status, and known limitations accurate.

```bash
cd "$RELEASE_WORKTREE"
git fetch origin main
git switch -c release/readme-available origin/main
$EDITOR README.md CHANGELOG.md
git diff --check
git diff README.md CHANGELOG.md
git add README.md CHANGELOG.md
git commit -m "docs: mark v0.1.0 available"
git push origin HEAD:main
```

Expected: the README Install section matches the live PyPI/AUR artifacts; the
status was already `v0.1.0` since 10.1. This commit is after the tag; it does not
change `v0.1.0`. Run this only after the 11.3 AUR checksum commit has been
pushed/merged so `origin/main` includes it. If `main` is protected, push
`git push -u origin release/readme-available`, open a PR from that branch, and
merge it through the protected path instead of the direct push.

- [ ] README Install availability is promoted only after both artifacts exist.
- [ ] README/SUPPORT/CHANGELOG claims match the published support matrix.

## 12. Sign-off

| Gate | Result | Evidence / notes |
|---|---|---|
| Source install, build, wheel/sdist | | |
| Setup: zero-touch, pinned default, explicit pinned, force, manual, Z: | | |
| Render, verify, evidence, cursor record | | |
| `run` no-kill / `run --restart` | | |
| Launcher, pin, focus, shell restart | | |
| Expose/unexpose, login handler/callback | | |
| Update, runner record-only/rebuild | | |
| Doctor and redacted bundle | | |
| Unbridge, idempotence, no residue | | |
| README status + CHANGELOG promoted (pre-tag 10.1) | | |
| Support matrix and known limitations (pre-tag 10.2) | | |
| History sanitized, mandatory scan, external settings (10.3–10.4) | | |
| Tag HEAD equals scanned HEAD; origin is GitHub | | |
| Tag, PyPI, AUR, post-publish installs | | |

- [ ] Every pre-tag item is PASS or a documented known limitation.
- [ ] History is sanitized and the public clone contains no old commits.
- [ ] `v0.1.0` tag, PyPI artifacts, and AUR package are published and verified.
- [ ] README/SUPPORT/CHANGELOG claims match the observed support matrix.

Release manager: ____________________  Date: __________  Tag commit: __________

**Do not tag on a FAIL. Do not announce on a post-publish FAIL.**
