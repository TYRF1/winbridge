#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Synthetic Tauri 2 fixture for WinBridge.
//!
//! The app deliberately delays its visible UI so `winbridge verify` has to wait
//! for a real first paint instead of passing on an immediately rendered window.
//! `WINBRIDGE_FIXTURE_DELAY_MS` overrides the delay; the integration test sets
//! it so the wait is deterministic on a loaded CI machine.

/// Two seconds is long enough for a blank frame to be captured first.
const DEFAULT_DELAY_MS: u64 = 2_000;

/// Never let a bad environment value hang the pipeline for a whole job.
const MAX_DELAY_MS: u64 = 120_000;

/// Return the configured first-paint delay to the frontend.
#[tauri::command]
fn fixture_delay_ms() -> u64 {
    std::env::var("WINBRIDGE_FIXTURE_DELAY_MS")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(DEFAULT_DELAY_MS)
        .min(MAX_DELAY_MS)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![fixture_delay_ms])
        .run(tauri::generate_context!())
        .expect("failed to run the WinBridge fixture");
}
