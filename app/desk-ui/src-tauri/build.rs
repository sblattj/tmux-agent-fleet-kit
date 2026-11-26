fn main() {
    // The kit checkout this app was built from: the default place to find lib/fleet_app.py at run time
    // (FLEET_KIT_ROOT overrides it). It is baked into the local binary only, never into the repo.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let root = root.canonicalize().unwrap_or(root);
    println!("cargo:rustc-env=FLEET_KIT_ROOT_BUILD={}", root.display());
    tauri_build::build()
}
