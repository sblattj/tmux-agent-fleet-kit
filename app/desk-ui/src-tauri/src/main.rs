// Fleet Desk: the fleet desk pages (DECISIONS, LEARN with its Lesson tab, WATCH) in their own Mac window.
//
// The app forks none of their HTML. It asks `lib/fleet_app.py` (the same verbs the pane has: `fleet app urls`,
// `fleet app serve`, `fleet app notify`) which desk servers are up, starts the missing ones, and points its one
// window at the live 127.0.0.1 page the menu picks. Notifications come from `fleet app notify --json`, which
// dedupes in the fleet state dir, so a restart or a second front end never fires the same call or lesson twice.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Manager, Url};
use tauri_plugin_notification::NotificationExt;

const POLL_SECS: u64 = 20;
const PATH: &str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Desk {
    name: String,
    title: String,
    url: Option<String>,
    alive: bool,
    pane: String,
    key: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Note {
    id: String,
    title: String,
    body: String,
}

fn kit_root() -> PathBuf {
    std::env::var("FLEET_KIT_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("FLEET_KIT_ROOT_BUILD")))
}

/// Run one `fleet app` verb (lib/fleet_app.py) and return its stdout. FLEET_ROLE is dropped: the app is the human.
fn fleet_app(args: &[&str]) -> Result<String, String> {
    let script = kit_root().join("lib").join("fleet_app.py");
    let path = match std::env::var("PATH") {
        Ok(p) if !p.is_empty() => format!("{}:{}", PATH, p),
        _ => PATH.to_string(),
    };
    let out = Command::new("python3")
        .arg(&script)
        .args(args)
        .env("PATH", path)
        .env_remove("FLEET_ROLE")
        .output()
        .map_err(|e| format!("can't run {}: {}", script.display(), e))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    } else {
        Err(format!(
            "fleet app {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ))
    }
}

fn desk_list(start: bool) -> Result<Vec<Desk>, String> {
    let raw = fleet_app(&[if start { "serve" } else { "urls" }, "--json"])?;
    serde_json::from_str(&raw).map_err(|e| format!("bad fleet app output: {}", e))
}

#[tauri::command]
fn desks(start: Option<bool>) -> Result<Vec<Desk>, String> {
    desk_list(start.unwrap_or(false))
}

#[tauri::command]
fn tunnel() -> Result<String, String> {
    fleet_app(&["tunnel"])
}

#[tauri::command]
fn open_desk(app: AppHandle, name: String) -> Result<(), String> {
    go(&app, &name)
}

/// Point the main window at a desk page (or "home", the bundled launcher). The URL is read fresh each time:
/// a restarted server has a new token.
fn go(app: &AppHandle, name: &str) -> Result<(), String> {
    let w = app.get_webview_window("main").ok_or("no main window")?;
    let target = if name == "home" {
        home_url()
    } else {
        let d = desk_list(true)?
            .into_iter()
            .find(|d| d.name == name)
            .ok_or(format!("no desk {}", name))?;
        match (d.alive, d.url) {
            (true, Some(u)) => u,
            _ => return Err(format!("{} is not running; in the pane: {}", d.title, d.pane)),
        }
    };
    let url = Url::parse(&target).map_err(|e| e.to_string())?;
    w.navigate(url).map_err(|e| e.to_string())?;
    w.show().ok();
    Ok(())
}

fn home_url() -> String {
    if cfg!(windows) { "http://tauri.localhost/index.html".into() } else { "tauri://localhost/index.html".into() }
}

fn notify_loop(app: AppHandle) {
    std::thread::spawn(move || loop {
        match fleet_app(&["notify", "--json"]) {
            Ok(raw) => {
                let notes: Vec<Note> = serde_json::from_str(&raw).unwrap_or_default();
                for n in notes {
                    let _ = app.notification().builder().title(&n.title).body(&n.body).show();
                }
            }
            Err(e) => eprintln!("{}", e),
        }
        std::thread::sleep(Duration::from_secs(POLL_SECS));
    });
}

fn menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let about = PredefinedMenuItem::about(app, Some("About Fleet Desk"), None)?;
    let app_menu = Submenu::with_items(
        app,
        "Fleet Desk",
        true,
        &[
            &about,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::show_all(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, None)?,
        ],
    )?;
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    let home = MenuItem::with_id(app, "desk:home", "Home", true, Some("CmdOrCtrl+0"))?;
    let desks = Submenu::with_id(app, "desks", "Desks", true)?;
    desks.append(&home)?;
    desks.append(&PredefinedMenuItem::separator(app)?)?;
    // The menu is the desk list fleet_app.py prints, so a new desk there shows up here with no Rust change.
    for d in desk_list(false).unwrap_or_default() {
        let accel = format!("CmdOrCtrl+{}", d.key);
        desks.append(&MenuItem::with_id(app, format!("desk:{}", d.name), &d.title, true, Some(accel.as_str()))?)?;
    }
    desks.append(&PredefinedMenuItem::separator(app)?)?;
    desks.append(&MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?)?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[&PredefinedMenuItem::minimize(app, None)?, &PredefinedMenuItem::fullscreen(app, None)?],
    )?;
    Menu::with_items(app, &[&app_menu, &edit, &desks, &window])
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![desks, open_desk, tunnel])
        .setup(|app| {
            let h = app.handle().clone();
            app.set_menu(menu(&h)?)?;
            app.on_menu_event(move |app, ev| {
                let id = ev.id().as_ref().to_string();
                if id == "reload" {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.eval("location.reload()");
                    }
                } else if let Some(name) = id.strip_prefix("desk:") {
                    if let Err(e) = go(app, name) {
                        let _ = app.notification().builder().title("Fleet Desk").body(&e).show();
                    }
                }
            });
            notify_loop(h);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Fleet Desk");
}
