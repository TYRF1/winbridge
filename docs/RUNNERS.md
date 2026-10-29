# Runners and paint results

A runner can launch and still render nothing. **Launch success is not render
success.** WinBridge records paint through `winbridge verify`; the runner matrix
below is per app and per environment, not a universal ranking.

## Matrix template

Record one row per runner per app. Keep evidence frames and the exact runner
ref.

| Runner | Paint | First paint | Cursor | Login callback | Notes |
|---|---|---|---|---|---|
| `soda@11.0-10` | PASS | ~2 min | no | untested | catalog default |
| `caffe-10.0` | FAIL | — | unknown | untested | transparent window, no paint after 10+ min in one test app; imported by reference |
| `ge-proton11-11` | FAIL | — | unknown | untested | blank window in early field observations; not supported in v0.1 |
| system Wine 11.20 | PASS | fast | unknown | untested | headless QA only; experimental |

The caffe row is an observation for one app, not a claim that caffe is always
worse. Re-test per app; runner behavior is workload-specific.

## Rules

- After `setup`, `verify`, or `runners pin --rebuild`, run `winbridge verify`
  and view the evidence. PASS means visible content, not a process that started.
- A runner change invalidates the previous `last_verified`; re-verify before
  treating the app as testable.
- Cursor visibility is independent of paint. A runner that paints may still
  show no cursor.
- `setup` and `verify` print the runner ref; include it with first-paint time
  and cursor behavior in bug reports.
- Do not infer a runner's paint result from another app or another prefix.
- The release checklist must include a runner matrix for the test app, with
  paint, cursor, and login callback columns.

## Cursor

Cursor visibility is not a paint result. WebView2 apps on the default runner can
paint while showing no cursor: WebView2 child processes set cursors on a window
owned by another process, and the window's own process cannot resolve them.
Wine never calls `XDefineCursor`; upstream `cursoricon.c` is unchanged through
Wine 11.15 ([Wine bug
58922](https://bugs.winehq.org/show_bug.cgi?id=58922)). This is a known
limitation of the default runner for WebView2 apps, not a WinBridge regression.

Trace it with the explicit Wine escape and read the per-app run log:

```bash
WINBRIDGE_WINEDEBUG=+cursor winbridge run <app>
tail -n 200 ~/.local/share/winbridge/logs/run-<app-id>.log
```

`WINBRIDGE_WINEDEBUG` is used verbatim for `WINEDEBUG`; an inherited `WINEDEBUG`
is still ignored, and the default stays `-all`. App launch output never inherits
the caller's stdio; a bridge child that outlives `run` cannot hold the caller's
pipe open. The app started by `run`, plus the opt-in `--cursor=force` helper's
`[cursor]` marker lines, writes to this log; setup/`wineboot` output and
URL-forwarding traces stay with their own commands. A `+all` or cursor trace
grows until the app exits, so truncate or delete the run log afterward.

WebView2 runtime 152+ can additionally hide the cursor while typing. Setting
`--disable-features=HideCursorWhileTyping` through the HKLM
`AdditionalBrowserArguments` policy may not change the behavior; WinBridge does
not set it as a default flag.

## Why this exists

The default runner is pinned because it is known to render the target class of
apps. That does not make it universal. A runner that works for one app can
produce a transparent or blank window for another; only `verify` (or a human
looking at the frames) can tell the difference.
