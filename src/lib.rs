//! Local development process orchestration with a built-in reverse proxy.

pub mod config;
pub mod frontend;
pub mod instance;
pub mod log_rotation;
pub mod logging;
pub mod manager;
pub mod paths;
pub mod process;
pub mod proxy;
pub mod routes;
pub mod shell_env;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Reference configuration demonstrating every supported service and target
/// form. It is never written into a profile.
pub const SAMPLE_CONFIG: &str = include_str!("../assets/services.toml.sample");
