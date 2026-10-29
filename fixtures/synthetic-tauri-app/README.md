# Synthetic Tauri 2 fixture

A minimal, app-agnostic Windows desktop app used only by the integration loop.
It is built on `windows-latest` and never committed: the workflow uploads the
NSIS installer as `winbridge-fixture-setup.exe`.

- Tauri crate: `=2.12.5` (CLI `2.12.4`, the matching npm release)
- Rust toolchain: `1.90.0`
- `Cargo.lock` is committed so the Windows build resolves the same transitive
  crates on every run
- URL scheme: `winbridge-fixture://`, registered by the NSIS installer through
  `plugins.deep-link.desktop.schemes`
- First paint delay: `WINBRIDGE_FIXTURE_DELAY_MS` (milliseconds, capped at 120 s;
  default 2 s). The Rust command returns the value and the frontend keeps the
  window blank until it elapses.

The window deliberately paints nothing until the delay ends so the smoke
harness has to wait for real content instead of passing on the first frame.

Local build (Windows, with the Tauri CLI installed):

```powershell
cd fixtures/synthetic-tauri-app
tauri build --bundles nsis
```

The installer lands in `src-tauri/target/release/bundle/nsis/`. The integration
test expects the path in `WINBRIDGE_FIXTURE_INSTALLER`; CI renames it to
`winbridge-fixture-setup.exe`.
