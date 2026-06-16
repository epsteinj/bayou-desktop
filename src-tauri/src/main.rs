// bayou-desktop — Tauri shell.
//
// Loads the brain UI (../ui) and, on startup, best-effort spawns the Python
// telemetry backend (backend/server.py) as a child process so the UI's
// WebSocket connects to a live engine. If Python/uvicorn isn't available the
// UI falls back to its built-in mock engine, so the app still runs.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;

struct Backend(Mutex<Option<Child>>);

fn spawn_backend() -> Option<Child> {
    // Resolve backend/server.py relative to the project root in dev.
    let candidates = ["backend/server.py", "../backend/server.py"];
    for path in candidates {
        if std::path::Path::new(path).exists() {
            return Command::new("python3").arg(path).spawn().ok();
        }
    }
    None
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            use tauri::Manager;
            app.manage(Backend(Mutex::new(spawn_backend())));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                use tauri::Manager;
                if let Some(state) = window.try_state::<Backend>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running bayou desktop");
}
