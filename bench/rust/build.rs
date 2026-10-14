//! Records the toolchain and the locked version of every library, so a result names
//! exactly what was measured instead of what the source says it should be.

use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=Cargo.lock");
    println!("cargo:rerun-if-changed=build.rs");
    let rustc = std::env::var("RUSTC").unwrap_or_else(|_| "rustc".into());
    let out = Command::new(rustc).arg("--version").output().ok();
    let v = out
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|| "unknown".into());
    println!("cargo:rustc-env=BENCH_RUSTC_VERSION={v}");
    println!(
        "cargo:rustc-env=BENCH_TARGET={}",
        std::env::var("TARGET").unwrap_or_default()
    );
    println!(
        "cargo:rustc-env=BENCH_PROFILE={}",
        std::env::var("PROFILE").unwrap_or_default()
    );
    println!(
        "cargo:rustc-env=BENCH_OPT_LEVEL={}",
        std::env::var("OPT_LEVEL").unwrap_or_default()
    );

    let lock = std::fs::read_to_string("Cargo.lock").unwrap_or_default();
    let mut name = String::new();
    for line in lock.lines() {
        if let Some(n) = line.strip_prefix("name = \"") {
            name = n.trim_end_matches('"').to_string();
        } else if let Some(v) = line.strip_prefix("version = \"") {
            let key: String = name
                .chars()
                .map(|c| {
                    if c.is_ascii_alphanumeric() {
                        c.to_ascii_uppercase()
                    } else {
                        '_'
                    }
                })
                .collect();
            println!("cargo:rustc-env=LOCKED_{key}={}", v.trim_end_matches('"'));
        }
    }
}
