strict no backward compatibility
after Rust changes, run `cargo fmt`, `cargo clippy --all-targets -- -D warnings`, and `cargo test`
to run the CI workflow's fmt + clippy + test steps against the working tree, use `ci/unix/ci.sh` (natively on this Linux host, or on the macOS VM with `ci/unix/remote.sh -H macvm`) and `pwsh -File ci/windows/remote.ps1` (on the Windows CI VM) — see the Development section of README.md. Worth doing before a release, or after touching platform-gated code (`cfg(windows)` / `cfg(target_os = "macos")`), which Linux-only checks never compile.
