# Run wait/progress design

Status: draft for approval
Date: 2026-10-28

## Intent

`winbridge run` is a launcher: it may kill on `--restart`, spawn the app, and
return immediately. That is right for a desktop click or a script that wants to
open-and-continue, but it is a black box when an agent or human needs a
return code for "is the app actually on screen?".

Add an opt-in waiting mode that reuses the existing transparency/paint harness.
Default behavior does not change.

## Behavior

```
winbridge run <app> [--restart] [--wait] [--timeout SECS] [--json] [-- APP_ARGS...]
```

- **No `--wait`**: stop if requested, launch, exit 0. Stages are printed for
  stop/launch/result, but the command does not wait for a window.
- **`--wait`**: after the same launch, observe the app and exit 0 only when two
  consecutive visible frames pass. On failure, exit 4 with the state
  (`transparent`, `background`, or `timeout`). An unsupported desktop exits 3.
- `--wait` defaults to 300 seconds. `--timeout` overrides it and is invalid
  without `--wait`.
- If an instance is already running, plain `run` keeps its current no-op
  behavior: print "already running" and exit 0. `run --wait` does **not** claim
  that instance is painted: it exits 4 with `reason="already-running"` and
  tells the caller to use `--restart --wait`. This keeps the promise that
  `--wait` exits 0 only after observing a launch it owns.
- `--json` emits one JSON object per line (NDJSON) on stdout. Human stage lines
  are suppressed. Logs stay in the app log file.

Typer 0.27's vendored option parser cannot express an optional-value flag
(`--wait[=secs]`) in one declaration. The supported spelling is `--wait` plus
`--timeout SECS`; `--wait` alone uses 300.

## Human output

```
[run +0.00s] stop: stopping old instance
[run +1.20s] stop: stopped
[run +1.21s] launch: started
[run +3.10s] window: found
[run +4.00s] paint: visible
[run +5.20s] paint: stable
[run +5.20s] result: PASS (5.20s)
```

The default (no wait) ends after `launch: started`:

```
[run +0.12s] result: launched (no wait)
```

Failures end with `result: FAIL (reason)` and the evidence directory when one
exists.

## JSON events

One object per line, flushed immediately:

```json
{"event":"stage","stage":"stop","status":"stopping","elapsed_s":0.0}
{"event":"stage","stage":"stop","status":"stopped","elapsed_s":1.2}
{"event":"stage","stage":"launch","status":"started","elapsed_s":1.21}
{"event":"stage","stage":"window","status":"found","elapsed_s":3.1}
{"event":"stage","stage":"paint","status":"stable","elapsed_s":5.2}
{"event":"result","status":"pass","reason":"passed","first_paint_s":4.0,"elapsed_s":5.2,"evidence":"/.../evidence"}
```

On failure, the final event carries `"status":"fail"` and the same `reason` the
verify harness reports. `--json` never mixes human lines into stdout.

## Approaches

- **Shared watcher (chosen).** Extract verify's observe-only loop and call it
  from both `verify` and `run --wait`. One paint definition, one background
  diff, one set of evidence.
- **Separate `winbridge watch <app>` command.** Easy to add, but the caller has
  to launch then watch, and the wait can race the launch. It does not give one
  command with one exit code.
- **Call `verify` after `run`.** Wrong: `verify` kills and relaunches, so the
  observed app is not the one `run` started, and `--restart` would kill twice.

## Architecture

- Extract the observe-only loop from `verify._verify_app_locked` into a public
  `watch_launch(...)` in `core/verify.py`. It still matches windows by prefix
  process membership, captures window frames, diffs against the pre-launch
  background, and requires two consecutive visible frames.
- `watch_launch` takes the launch handle, prefix, desktop, run directory,
  background path, timeout, and optional stage callback. It does not kill or
  launch.
- `verify_app` keeps its current contract: check desktop, kill prefix, capture
  background, launch, then call `watch_launch`. This makes verify and
  `run --wait` one detection path, not two.
- `run_app` gains two optional callbacks: `before_start` (inside the app lock,
  after any kill and after the running-instance probe, before spawn) and
  `on_stage` (for stop/launch transitions). Existing callers are unchanged.
- The CLI owns the wait flow: validate the desktop before killing, create the
  evidence directory, capture the background in `before_start`, launch, then
  call `watch_launch` under the per-app lock.
- `run --wait` writes frames/background under the normal evidence directory
  but does not update `last_verified`; it is a launch check, not `verify`.

## Non-goals

- No cursor checking.
- No change to default `run` blocking/exit semantics.
- No `--wait=SECS` pre-parse shim.
- No retry loop; `setup` keeps its one transparency retry. `run --wait` reports
  the first failure and returns.

## Tests

- `run_app` callback order: stop start/done, `before_start`, launch start/done;
  `before_start` is not called when already running or when restart is rejected.
- `verify_app` still passes after the watcher extraction (existing suite).
- CLI no-wait: exits 0, does not require a desktop, prints launch stages.
- CLI wait success: background captured before launch, window found, paint
  stable, exit 0; JSON lines parse independently.
- CLI wait failures: transparent, background, timeout, already-running,
  unsupported desktop; exit codes 4, 4, 4, 4, 3 with the state in human and JSON
  output.
- `--timeout` without `--wait` is a usage error (exit 2).
- Already-running `--wait` launches nothing and exits 4 with
  `reason="already-running"`.

## Risks

- `--wait` holds the per-app lock across observation, up to the timeout. A
  concurrent launcher click queues behind it; that is the same guarantee
  `verify` makes.
- The background diff keeps the existing fixed-inset, 1x-scale limitations.
- `--wait` can be added to the launcher later; it is not part of the generated
  desktop entry.
