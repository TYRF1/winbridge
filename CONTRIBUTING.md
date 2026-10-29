# Contributing to WinBridge

Thanks for helping. This project is app-agnostic by rule and open source from day
one. Read the [design spec](docs/design.md) before changing behavior; the
[implementation plan](docs/plan-v0.1.md) is the build order.

## The one hard rule

**No vendor material.** No third-party app names, binaries, installers,
screenshots, logs, profiles, or "just for app X" code. WinBridge must work for
any Tauri 2.x / WebView2 Windows build. If a fix only works for one app, it
belongs in that app's profile, not in the repo. This is an NDA requirement and
a design invariant.

When reporting a bug, describe the behavior, not the vendor. Redact paths,
titles, and URLs. Use `winbridge doctor --bundle`; it redacts URLs and tokens.
Before any public release, history must be sanitized per
[docs/PUBLISHING.md](docs/PUBLISHING.md).

## Development setup

```bash
git clone https://github.com/TYRF1/winbridge.git
cd winbridge
uv sync --dev
uv run pytest -q
uv run ruff check .
uv run mypy src
```

Python 3.11+. Runtime dependencies are intentionally small: Typer, Rich,
`tomli-w`, Pillow. Stdlib handles HTTP, hashing, subprocesses, TOML reading, and
registry text.

Integration tests need Wine and a desktop session; unit tests must pass without
either. The synthetic Tauri fixture is built on Windows CI and never committed.

### Sandboxed development

Agents and reviewers run unit tests, lint, types, and packaging in a headless
container so the host desktop is never disturbed and the main checkout is never
touched:

```bash
bash scripts/sandbox-build      # once, and after sandbox/Containerfile changes
scripts/sandbox uv sync --dev
scripts/sandbox uv run pytest -q
scripts/sandbox uv run ruff check .
scripts/sandbox uv run mypy src
```

`scripts/sandbox` mounts only the current worktree at `/work`, keeps the host
home, `DISPLAY`/`WAYLAND_DISPLAY`, GPU, and audio out of reach, and refuses the
main checkout. There is no Wine inside; integration tests and GUI checks stay on
a host session. The guarantees, limits, and host-side exceptions are in
[docs/SANDBOX.md](docs/SANDBOX.md). The host-native commands above are fine for
humans who prefer them; agents must use the sandbox for these checks.

Git metadata for a linked worktree lives in the main checkout, outside the
mount, so `git status`, `git diff`, and `git commit` run on the host in the
worktree. The sandbox is for executing code, not version control.

## Architecture in one screen

```
cli.py
  └── core services
        ├── profiles / state / locks
        ├── runner store (catalog, checksums)
        ├── backend protocol ── Wine v0.1 / Bottles v0.2
        ├── installer + detection + WebView2
        ├── launcher + omarchy-shell pin
        ├── run / expose / unbridge / update
        ├── verify ── desktop protocol ── Hyprland / X11
        └── doctor / logging
```

Two internal seams, both dependency-injected. **No dynamic plugin system** in
v0.1: one interface, two implementations at most. Do not add an extension API
without a design discussion.

## Adding a desktop adapter

1. Implement `Desktop` in `src/winbridge/desktops/` (`windows`,
   `wait_for_window`, `screenshot`, `pin`, `unpin`) and declare `capabilities`.
2. Detect the session in `desktops/__init__.py` in the established order.
3. Degrade, do not guess: missing tools remove capabilities; `verify` exits 3
   with a clear message; `run` still works.
4. Add unit tests with recorded command output. No live compositor in CI.
5. Add the platform and its status to `docs/SUPPORT.md` and the README matrix.

## Adding a backend

1. Implement the `Backend` protocol in `src/winbridge/backends/`. Backends are
   runner-bound: construct with a resolved runner root.
2. Never mutate a shared Bottles/Steam installation or a pre-existing prefix
   without confirmation.
3. Keep runner execution behind the backend; no `wine` calls in core services.
4. Add tests with the shared `fake_backend` fixture in `tests/conftest.py`. A
   backend without a fake test will not merge.
5. Use `core/context.py` as the composition root; do not construct paths,
   backends, or desktops inside service functions.
6. Document the capability and its support status.

## Adding a runner or WebView2 payload to the catalog

- Link the upstream release; never mirror binaries.
- Record the exact version, URL, and SHA-256.
- Prefer a wine-kind runner for v0.1. Proton needs the Steam runtime/umu layer
  and is out of scope until that is proven.
- Update `src/winbridge/data/catalog.toml`; tests use the fixture catalog.

## Profile and state rules

- `schema = 1`; additive changes only. Unknown keys must survive a rewrite.
- Write a backup before migrating or rebuilding anything.
- Everything under XDG paths; no writes to the repo or CWD.
- Never run as root. Never use `shell=True`.
- `run` never kills a running app; only `run --restart` does. Setup/verify/update
  kill because they own the prefix.
- Strip Wine's `Z:` host-root link at prefix creation; `expose` is the only
  supported host link.
- Print every auto-detected value. Silent magic is a bug.

## Tests and review

- Write the failing test first. One behavior per test.
- Unit tests must not require Wine, Hyprland, or network.
- Integration tests are marked and skipped unless `WINBRIDGE_INTEGRATION=1`.
- Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src` before a PR.
- Keep PRs focused. A behavior change needs a test and a docs update.
- Screenshots in a PR must be generic (the fixture app), never a real vendor app.

## Commit messages

Short imperative summaries: `feat: add scheme registration`, `fix: escape
backslashes in Exec`, `docs: ...`, `test: ...`. Reference the spec section when a
decision is not obvious.

## Reporting bugs

Use the issue template. Include:

- `winbridge --version`, distro, desktop, runner, WebView2 version.
- The command and the exact error.
- The path to the `doctor --bundle` tarball (attach only after checking it).
- Whether the app renders, shows a cursor, sees exposed files, and completes login.

Never paste proprietary app names, screenshots, installers, or logs.

## License

By contributing, you agree your work is licensed under the MIT license.
