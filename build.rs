fn main() {
    // Element debug info backs the ElementHandle queries in the UI tests;
    // release binaries leave it out.
    let debug_build = std::env::var("PROFILE").as_deref() == Ok("debug");
    let config = slint_build::CompilerConfiguration::new().with_debug_info(debug_build);
    slint_build::compile_with_config("ui/app.slint", config).expect("compile ui/app.slint");
}
