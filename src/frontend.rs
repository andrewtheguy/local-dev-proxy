//! Frontends drive a [`Manager`] and react to application events.
//!
//! [`crate::desktop::Desktop`] is the default: a manager window plus a
//! system-tray icon. [`Headless`] runs the same backend with no UI.

use std::process::ExitCode;
use std::sync::mpsc::Receiver;

use crate::manager::Manager;

/// Events delivered to a frontend from outside its own control flow.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppEvent {
    /// A later launch for the same profile asked this instance to show itself.
    Activate,
    /// A termination signal (SIGTERM/SIGINT/SIGHUP, or Ctrl-C/Ctrl-Break/close
    /// on Windows) asked the application to quit.
    Shutdown,
}

pub trait Frontend {
    /// Run until the application should exit. The frontend decides whether to
    /// start services immediately; the manager stops everything when dropped.
    fn run(self: Box<Self>, manager: Manager, events: Receiver<AppEvent>) -> ExitCode;
}

/// Runs the proxy and services in the foreground with no UI.
///
/// There is no configuration editor, so a missing or invalid configuration
/// is reported and the process exits instead of waiting for an edit.
#[derive(Debug, Default)]
pub struct Headless;

impl Frontend for Headless {
    fn run(self: Box<Self>, mut manager: Manager, events: Receiver<AppEvent>) -> ExitCode {
        let services_file = manager.paths().services_file();
        if !manager.has_config() {
            tracing::error!(
                "No configuration at {}. Create it (`local-dev-proxy --sample-config` prints a \
                 reference), then start again.",
                services_file.display()
            );
            return ExitCode::FAILURE;
        }
        let manifest = match manager.start() {
            Ok(manifest) => manifest,
            Err(err) => {
                tracing::error!("Startup failed; services left stopped: {err}");
                return ExitCode::FAILURE;
            }
        };
        for service in manager.services() {
            tracing::info!("Service {}: {}", service.name, service.status);
        }
        tracing::info!("Portal: http://localhost:{}/", manifest.http_port);

        // A shutdown event, or every sender dropping, ends the loop.
        while let Ok(AppEvent::Activate) = events.recv() {
            tracing::info!("Another launch asked to show the manager; running headless");
        }
        tracing::info!("Shutting down");
        manager.stop();
        ExitCode::SUCCESS
    }
}
