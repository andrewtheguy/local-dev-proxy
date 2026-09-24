//! The UI-independent manager core.
//!
//! [`Manager`] owns the proxy and the managed service processes for one
//! profile and exposes every operation a frontend needs: lifecycle, per-service
//! control, status, logs, routes, and configuration editing. Frontends (the
//! headless runner and the desktop UI) only call into this type.

use std::path::Path;

use crate::config::{ConfigError, Manifest, load_manifest, parse_manifest};
use crate::log_rotation::{LogLimits, tail_file};
use crate::paths::ProjectPaths;
use crate::process::{ServiceError, ServiceManager, ServiceSnapshot};
use crate::proxy::{ProxyError, ProxyServer};
use crate::routes::{RouteGroup, RouteTable, route_listing};

#[derive(Debug, thiserror::Error)]
pub enum ManagerError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Proxy(#[from] ProxyError),
    #[error(transparent)]
    Service(#[from] ServiceError),
    #[error("services are not running")]
    NotRunning,
    #[error("could not write {path}: {source}")]
    Write {
        path: String,
        source: std::io::Error,
    },
}

#[derive(Debug)]
pub struct Manager {
    paths: ProjectPaths,
    runtime: tokio::runtime::Runtime,
    log_limits: LogLimits,
    /// Kept after a stop so the last known service states stay visible.
    services: Option<ServiceManager>,
    proxy: Option<ProxyServer>,
    /// The configuration the proxy and services were started from; `Some`
    /// exactly while running.
    running: Option<Manifest>,
}

/// What [`Manager::apply`] did with the configuration on disk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Applied {
    /// Nothing was running, so it was started.
    Started,
    /// It differed from the running configuration, so everything restarted.
    Restarted,
    /// It matches the running configuration; nothing was touched.
    Unchanged,
}

impl Manager {
    pub fn new(paths: ProjectPaths) -> std::io::Result<Self> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .thread_name("proxy")
            .build()?;
        Ok(Self {
            paths,
            runtime,
            log_limits: LogLimits::default(),
            services: None,
            proxy: None,
            running: None,
        })
    }

    pub fn paths(&self) -> &ProjectPaths {
        &self.paths
    }

    /// The runtime that drives the proxy; frontends may spawn tasks on it.
    pub fn runtime(&self) -> &tokio::runtime::Handle {
        self.runtime.handle()
    }

    pub fn is_running(&self) -> bool {
        self.proxy.is_some()
    }

    pub fn has_config(&self) -> bool {
        self.paths.services_file().exists()
    }

    /// Load the configuration, start the auto-start services, and bind the
    /// proxy. On any failure everything started so far is stopped again.
    ///
    /// Returns the manifest that is now running.
    pub fn start(&mut self) -> Result<Manifest, ManagerError> {
        if self.is_running() {
            self.stop();
        }
        let manifest = load_manifest(&self.paths.services_file())?;
        // Resolve routes before launching anything so a bad route never
        // leaves orphaned processes behind.
        let routes = RouteTable::from_manifest(
            &manifest,
            &|key| std::env::var(key).ok(),
            self.paths.root(),
        )?;
        let services = ServiceManager::new(
            &manifest,
            self.paths.logs_dir(),
            self.paths.root(),
            self.log_limits,
        )?;
        services.start_all();
        self.services = Some(services);

        match self.runtime.block_on(ProxyServer::bind(
            routes,
            manifest.http_port,
            &manifest.bind,
        )) {
            Ok(proxy) => {
                self.proxy = Some(proxy);
                self.running = Some(manifest.clone());
                Ok(manifest)
            }
            Err(err) => {
                self.stop();
                Err(err.into())
            }
        }
    }

    /// Start the configuration on disk, restarting everything only if it
    /// differs from the running one. Comment and formatting changes do not
    /// count as differences.
    pub fn apply(&mut self) -> Result<Applied, ManagerError> {
        let Some(running) = &self.running else {
            self.start()?;
            return Ok(Applied::Started);
        };
        if *running == load_manifest(&self.paths.services_file())? {
            return Ok(Applied::Unchanged);
        }
        self.start()?;
        Ok(Applied::Restarted)
    }

    /// Stop the proxy and every managed service.
    pub fn stop(&mut self) {
        self.running = None;
        if let Some(proxy) = self.proxy.take() {
            self.runtime.block_on(proxy.shutdown());
        }
        if let Some(services) = &self.services {
            services.stop_all();
        }
    }

    pub fn start_service(&self, name: &str) -> Result<(), ManagerError> {
        Ok(self.running_services()?.start_service(name)?)
    }

    pub fn stop_service(&self, name: &str) -> Result<(), ManagerError> {
        Ok(self.running_services()?.stop_service(name)?)
    }

    pub fn restart_service(&self, name: &str) -> Result<(), ManagerError> {
        Ok(self.running_services()?.restart_service(name)?)
    }

    fn running_services(&self) -> Result<&ServiceManager, ManagerError> {
        match &self.services {
            Some(services) if self.is_running() => Ok(services),
            _ => Err(ManagerError::NotRunning),
        }
    }

    /// Current state of every service; empty before the first start.
    pub fn services(&self) -> Vec<ServiceSnapshot> {
        self.services
            .as_ref()
            .map(ServiceManager::status)
            .unwrap_or_default()
    }

    /// Services that have a log view.
    pub fn log_names(&self) -> Vec<String> {
        self.services
            .as_ref()
            .map(ServiceManager::service_names)
            .unwrap_or_default()
    }

    /// The last `lines` lines of a service's log.
    pub fn tail_log(&self, name: &str, lines: usize) -> Result<String, ManagerError> {
        let services = self.services.as_ref().ok_or(ManagerError::NotRunning)?;
        Ok(tail_file(&services.log_path(name)?, lines))
    }

    /// Route listing for the running configuration, or the one on disk
    /// while stopped.
    pub fn routes(&self) -> Result<Vec<RouteGroup>, ConfigError> {
        match &self.running {
            Some(manifest) => Ok(route_listing(manifest)),
            None => Ok(route_listing(&load_manifest(&self.paths.services_file())?)),
        }
    }

    /// The configuration text on disk, or `None` if none exists yet.
    pub fn read_config(&self) -> std::io::Result<Option<String>> {
        match std::fs::read_to_string(self.paths.services_file()) {
            Ok(text) => Ok(Some(text)),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(err),
        }
    }

    /// Check configuration text, including that every route resolves,
    /// without touching the filesystem.
    pub fn validate_config(&self, text: &str) -> Result<Manifest, ConfigError> {
        let manifest = parse_manifest(text)?;
        RouteTable::from_manifest(&manifest, &|key| std::env::var(key).ok(), self.paths.root())?;
        Ok(manifest)
    }

    /// Validate and atomically replace the configuration. The running state
    /// is untouched until [`Manager::apply`].
    pub fn save_config(&self, text: &str) -> Result<(), ManagerError> {
        self.validate_config(text)?;
        write_atomically(&self.paths.services_file(), text)
    }
}

impl Drop for Manager {
    fn drop(&mut self) {
        self.stop();
    }
}

fn write_atomically(destination: &Path, text: &str) -> Result<(), ManagerError> {
    let mut temporary = destination.as_os_str().to_owned();
    temporary.push(".tmp");
    let temporary = std::path::PathBuf::from(temporary);
    std::fs::write(&temporary, text)
        .and_then(|()| std::fs::rename(&temporary, destination))
        .map_err(|source| ManagerError::Write {
            path: destination.display().to_string(),
            source,
        })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::process::ServiceStatus;

    fn free_port() -> u16 {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    fn config(port: u16) -> String {
        format!(
            "http_port = {port}\nbind = [\"127.0.0.1\"]\n\n\
             [services.app]\ncommand = [\"sh\", \"-c\", \"exec sleep 60\"]\n\n\
             [[services.app.routes]]\nid = \"app\"\nhosts = [\"app.localhost\"]\ntarget_port = 1\n\n\
             [services.external]\n"
        )
    }

    fn manager() -> (tempfile::TempDir, Manager) {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProjectPaths::new(dir.path()).unwrap().ensure().unwrap();
        (dir, Manager::new(paths).unwrap())
    }

    #[test]
    fn config_edits_while_running_apply_only_when_they_change_something() {
        let (_dir, mut manager) = manager();
        assert_eq!(manager.read_config().unwrap(), None);
        assert!(matches!(
            manager.save_config("http_port = 1"),
            Err(ManagerError::Config(_))
        ));
        assert!(!manager.has_config());

        let text = config(free_port());
        manager.save_config(&text).unwrap();
        assert_eq!(
            manager.read_config().unwrap().as_deref(),
            Some(text.as_str())
        );
        assert_eq!(manager.apply().unwrap(), Applied::Started);
        let pid = manager.services()[0].pid;

        // Saving while running leaves the running services alone.
        let commented = format!("# a comment\n{text}");
        manager.save_config(&commented).unwrap();
        assert!(manager.is_running());
        assert_eq!(manager.apply().unwrap(), Applied::Unchanged);
        assert_eq!(manager.services()[0].pid, pid);

        let port = free_port();
        manager.save_config(&config(port)).unwrap();
        assert_eq!(manager.services()[0].pid, pid);
        assert_eq!(manager.apply().unwrap(), Applied::Restarted);
        assert!(manager.is_running());
        assert_ne!(manager.services()[0].pid, pid);
        std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    }

    #[test]
    fn start_runs_services_and_proxy_then_stop_releases_them() {
        let (_dir, mut manager) = manager();
        let port = free_port();
        manager.save_config(&config(port)).unwrap();

        manager.start().unwrap();
        assert!(manager.is_running());
        let statuses: Vec<_> = manager.services().iter().map(|s| s.status).collect();
        assert_eq!(statuses, [ServiceStatus::Running, ServiceStatus::Unmanaged]);
        std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();

        manager.restart_service("app").unwrap();
        assert_eq!(manager.services()[0].restart_count, 1);
        assert!(matches!(
            manager.start_service("external"),
            Err(ManagerError::Service(ServiceError::Unmanaged(_)))
        ));
        assert!(
            manager
                .tail_log("app", 10)
                .unwrap()
                .contains("--- app started at")
        );
        assert_eq!(
            manager.routes().unwrap()[0].entries[0].target,
            "localhost:1"
        );

        manager.stop();
        assert!(!manager.is_running());
        assert_eq!(manager.services()[0].status, ServiceStatus::Stopped);
        assert!(matches!(
            manager.start_service("app"),
            Err(ManagerError::NotRunning)
        ));
        // The port is free again, so a restart can rebind it.
        manager.start().unwrap();
    }

    #[test]
    fn failed_proxy_bind_stops_started_services() {
        let (_dir, mut manager) = manager();
        let occupied = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = occupied.local_addr().unwrap().port();
        manager.save_config(&config(port)).unwrap();

        assert!(matches!(manager.start(), Err(ManagerError::Proxy(_))));
        assert!(!manager.is_running());
        assert_eq!(manager.services()[0].status, ServiceStatus::Stopped);
    }

    #[test]
    fn route_errors_prevent_any_launch() {
        let (_dir, mut manager) = manager();
        let text =
            config(free_port()).replace("target_port = 1", "target_port_env = \"LDP_UNSET_PORT\"");
        assert!(matches!(
            manager.save_config(&text),
            Err(ManagerError::Config(_))
        ));
        // A hand-edited file with the same problem fails before any launch.
        std::fs::write(manager.paths().services_file(), &text).unwrap();
        assert!(matches!(manager.start(), Err(ManagerError::Config(_))));
        assert!(manager.services().is_empty());
    }
}
