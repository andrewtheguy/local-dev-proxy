//! Managed service processes: launch, stop, restart, crash detection, and
//! stdout/stderr capture into rotating per-service logs.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fmt;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crate::config::{ConfigError, Manifest, resolve_command};
use crate::log_rotation::{LogLimits, RotatingLogWriter, pump_log_stream};

const MONITOR_INTERVAL: Duration = Duration::from_secs(2);
const GRACEFUL_STOP_TIMEOUT: Duration = Duration::from_secs(5);
const FORCED_STOP_TIMEOUT: Duration = Duration::from_secs(3);
const LOG_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const LOG_DISCARD_TIMEOUT: Duration = Duration::from_secs(1);
const WAIT_POLL: Duration = Duration::from_millis(25);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceStatus {
    Running,
    Stopped,
    /// The process exited on its own (with any exit code) or failed to launch.
    Crashed,
    /// No command: another tool owns the process.
    Unmanaged,
    Disabled,
}

impl ServiceStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Stopped => "stopped",
            Self::Crashed => "crashed",
            Self::Unmanaged => "unmanaged",
            Self::Disabled => "disabled",
        }
    }

    /// Whether start/stop/restart apply to a service in this state.
    pub fn is_controllable(self) -> bool {
        matches!(self, Self::Running | Self::Stopped | Self::Crashed)
    }
}

impl fmt::Display for ServiceStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A point-in-time view of one service, for display.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceSnapshot {
    pub name: String,
    pub status: ServiceStatus,
    pub managed: bool,
    pub pid: Option<u32>,
    pub exit_code: Option<i32>,
    pub restart_count: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ServiceError {
    #[error("Unknown service: {0}")]
    Unknown(String),
    #[error("Service '{0}' is unmanaged")]
    Unmanaged(String),
}

#[derive(Debug)]
struct Launch {
    command: Vec<String>,
    env: HashMap<OsString, OsString>,
    auto_start: bool,
}

/// An in-flight capture of a child's output into its log.
#[derive(Debug)]
struct LogCapture {
    /// Disconnects when the pump thread finishes.
    done: mpsc::Receiver<()>,
    discard: Arc<AtomicBool>,
}

impl LogCapture {
    /// Wait for the pipe to drain. If a lingering grandchild keeps it open,
    /// stop writing so the next launch owns the log file alone.
    fn finish(self, name: &str) {
        if self.done.recv_timeout(LOG_DRAIN_TIMEOUT) != Err(RecvTimeoutError::Timeout) {
            return;
        }
        self.discard.store(true, Ordering::Relaxed);
        if self.done.recv_timeout(LOG_DISCARD_TIMEOUT) == Err(RecvTimeoutError::Timeout) {
            tracing::warn!("Log capture for service {name} is still draining a held-open pipe");
        }
    }
}

#[derive(Debug)]
struct ServiceInfo {
    name: String,
    /// `None` for unmanaged and disabled services.
    launch: Option<Launch>,
    status: ServiceStatus,
    exit_code: Option<i32>,
    restart_count: u32,
    child: Option<Child>,
    capture: Option<LogCapture>,
}

impl ServiceInfo {
    fn snapshot(&self) -> ServiceSnapshot {
        ServiceSnapshot {
            name: self.name.clone(),
            status: self.status,
            managed: self.launch.is_some(),
            pid: self.child.as_ref().map(Child::id),
            exit_code: self.exit_code,
            restart_count: self.restart_count,
        }
    }

    fn finish_capture(&mut self) {
        if let Some(capture) = self.capture.take() {
            capture.finish(&self.name);
        }
    }

    /// Record that the child exited without being asked to.
    fn mark_exited(&mut self, status: ExitStatus) {
        self.child = None;
        self.finish_capture();
        self.exit_code = exit_code(status);
        self.status = ServiceStatus::Crashed;
        tracing::warn!(
            "Service {} crashed (exit code {})",
            self.name,
            display_code(self.exit_code)
        );
    }
}

#[derive(Debug)]
struct Shared {
    services: Mutex<Vec<ServiceInfo>>,
    log_dir: PathBuf,
    cwd: PathBuf,
    limits: LogLimits,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, Vec<ServiceInfo>> {
        self.services
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    fn log_path(&self, name: &str) -> PathBuf {
        self.log_dir.join(format!("{name}.log"))
    }

    fn poll_exits(&self) {
        for info in self.lock().iter_mut() {
            if info.status != ServiceStatus::Running {
                continue;
            }
            let exited = info
                .child
                .as_mut()
                .and_then(|child| child.try_wait().ok().flatten());
            if let Some(status) = exited {
                info.mark_exited(status);
            }
        }
    }

    fn start_locked(&self, info: &mut ServiceInfo) {
        if info.launch.is_none() {
            return;
        }
        if let Some(child) = &mut info.child {
            match child.try_wait() {
                Ok(None) => return,
                Ok(Some(status)) => info.mark_exited(status),
                Err(_) => {}
            }
        }
        info.finish_capture();
        let Some(launch) = &info.launch else {
            return;
        };

        let log_path = self.log_path(&info.name);
        let mut log = match RotatingLogWriter::open(&log_path, self.limits) {
            Ok(log) => log,
            Err(err) => {
                tracing::error!(
                    "Failed to start {}: cannot open {}: {err}",
                    info.name,
                    log_path.display()
                );
                info.status = ServiceStatus::Crashed;
                return;
            }
        };
        let timestamp = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S UTC");
        let rule = "=".repeat(60);
        let _ = write!(
            log,
            "\n{rule}\n--- {} started at {timestamp} ---\n{rule}\n",
            info.name
        );

        match spawn_with_output(&launch.command, &launch.env, &self.cwd) {
            Ok((child, output)) => {
                let (done_tx, done_rx) = mpsc::channel::<()>();
                let discard = Arc::new(AtomicBool::new(false));
                let pump_discard = Arc::clone(&discard);
                let spawned = std::thread::Builder::new()
                    .name(format!("{}-log-writer", info.name))
                    .spawn(move || {
                        pump_log_stream(output, log, &pump_discard);
                        drop(done_tx);
                    });
                if let Err(err) = spawned {
                    tracing::error!("Could not start log capture for {}: {err}", info.name);
                }
                tracing::info!("Started {} (PID {})", info.name, child.id());
                info.capture = Some(LogCapture {
                    done: done_rx,
                    discard,
                });
                info.child = Some(child);
                info.status = ServiceStatus::Running;
                info.exit_code = None;
            }
            Err(err) => {
                let program = &launch.command[0];
                let message = if err.kind() == std::io::ErrorKind::NotFound {
                    format!("Command not found: {program}")
                } else {
                    format!("Could not start {program}: {err}")
                };
                let _ = writeln!(log, "ERROR: {message}");
                tracing::error!("Failed to start {}: {message}", info.name);
                info.status = ServiceStatus::Crashed;
            }
        }
    }

    fn stop_locked(info: &mut ServiceInfo) {
        let Some(mut child) = info.child.take() else {
            info.finish_capture();
            info.status = ServiceStatus::Stopped;
            return;
        };
        let status = match child.try_wait() {
            Ok(Some(status)) => status,
            _ => terminate_tree(&mut child, &info.name),
        };
        info.finish_capture();
        info.exit_code = exit_code(status);
        info.status = ServiceStatus::Stopped;
        tracing::info!(
            "Stopped {} (exit code {})",
            info.name,
            display_code(info.exit_code)
        );
    }
}

#[derive(Debug)]
struct Monitor {
    stop: mpsc::Sender<()>,
    thread: JoinHandle<()>,
}

/// Owns every service declared in one manifest.
#[derive(Debug)]
pub struct ServiceManager {
    shared: Arc<Shared>,
    monitor: Mutex<Option<Monitor>>,
}

impl ServiceManager {
    /// Prepare services from `manifest`. Command placeholders are resolved
    /// here, with the process environment taking precedence over each
    /// service's `env` table; the child environment is the process
    /// environment overlaid with the service's `env`.
    pub fn new(
        manifest: &Manifest,
        log_dir: impl Into<PathBuf>,
        cwd: impl Into<PathBuf>,
        limits: LogLimits,
    ) -> Result<Self, ConfigError> {
        let log_dir = log_dir.into();
        std::fs::create_dir_all(&log_dir)
            .map_err(|err| ConfigError(format!("Could not create {}: {err}", log_dir.display())))?;

        let mut services = Vec::with_capacity(manifest.services.len());
        for service in &manifest.services {
            let (launch, status) = match (&service.command, service.disabled) {
                (_, true) => (None, ServiceStatus::Disabled),
                (None, false) => (None, ServiceStatus::Unmanaged),
                (Some(command), false) => {
                    let command = resolve_command(command, |key| {
                        std::env::var(key)
                            .ok()
                            .or_else(|| service.env.get(key).cloned())
                    })?;
                    let mut env: HashMap<OsString, OsString> = std::env::vars_os().collect();
                    env.extend(
                        service
                            .env
                            .iter()
                            .map(|(key, value)| (key.into(), value.into())),
                    );
                    let launch = Launch {
                        command,
                        env,
                        auto_start: service.auto_start,
                    };
                    (Some(launch), ServiceStatus::Stopped)
                }
            };
            services.push(ServiceInfo {
                name: service.name.clone(),
                launch,
                status,
                exit_code: None,
                restart_count: 0,
                child: None,
                capture: None,
            });
        }

        Ok(Self {
            shared: Arc::new(Shared {
                services: Mutex::new(services),
                log_dir,
                cwd: cwd.into(),
                limits,
            }),
            monitor: Mutex::new(None),
        })
    }

    /// Start every managed service with `auto_start`, then watch for crashes.
    pub fn start_all(&self) {
        {
            let mut services = self.shared.lock();
            for info in services.iter_mut() {
                if info.launch.as_ref().is_some_and(|launch| launch.auto_start) {
                    self.shared.start_locked(info);
                }
            }
        }
        self.start_monitor();
    }

    /// Stop every managed service and the crash monitor.
    pub fn stop_all(&self) {
        self.stop_monitor();
        for info in self.shared.lock().iter_mut() {
            if info.launch.is_some() {
                Shared::stop_locked(info);
            }
        }
    }

    pub fn start_service(&self, name: &str) -> Result<(), ServiceError> {
        self.with_managed(name, |shared, info| shared.start_locked(info))
    }

    pub fn stop_service(&self, name: &str) -> Result<(), ServiceError> {
        self.with_managed(name, |_, info| Shared::stop_locked(info))
    }

    pub fn restart_service(&self, name: &str) -> Result<(), ServiceError> {
        self.with_managed(name, |shared, info| {
            Shared::stop_locked(info);
            shared.start_locked(info);
            info.restart_count += 1;
        })
    }

    pub fn status(&self) -> Vec<ServiceSnapshot> {
        self.shared
            .lock()
            .iter()
            .map(ServiceInfo::snapshot)
            .collect()
    }

    pub fn service_names(&self) -> Vec<String> {
        self.shared
            .lock()
            .iter()
            .map(|info| info.name.clone())
            .collect()
    }

    pub fn log_path(&self, name: &str) -> Result<PathBuf, ServiceError> {
        if self.shared.lock().iter().any(|info| info.name == name) {
            Ok(self.shared.log_path(name))
        } else {
            Err(ServiceError::Unknown(name.to_owned()))
        }
    }

    /// Detect exited children now instead of waiting for the monitor tick.
    pub fn poll_exits(&self) {
        self.shared.poll_exits();
    }

    fn with_managed(
        &self,
        name: &str,
        action: impl FnOnce(&Shared, &mut ServiceInfo),
    ) -> Result<(), ServiceError> {
        let mut services = self.shared.lock();
        let info = services
            .iter_mut()
            .find(|info| info.name == name)
            .ok_or_else(|| ServiceError::Unknown(name.to_owned()))?;
        if info.launch.is_none() {
            return Err(ServiceError::Unmanaged(name.to_owned()));
        }
        action(&self.shared, info);
        Ok(())
    }

    fn monitor_slot(&self) -> MutexGuard<'_, Option<Monitor>> {
        self.monitor
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    fn start_monitor(&self) {
        let mut slot = self.monitor_slot();
        if slot.is_some() {
            return;
        }
        let (stop, stop_rx) = mpsc::channel::<()>();
        let shared = Arc::clone(&self.shared);
        let spawned = std::thread::Builder::new()
            .name("service-monitor".into())
            .spawn(move || {
                while stop_rx.recv_timeout(MONITOR_INTERVAL) == Err(RecvTimeoutError::Timeout) {
                    shared.poll_exits();
                }
            });
        match spawned {
            Ok(thread) => *slot = Some(Monitor { stop, thread }),
            Err(err) => tracing::error!("Could not start the service monitor: {err}"),
        }
    }

    fn stop_monitor(&self) {
        if let Some(monitor) = self.monitor_slot().take() {
            drop(monitor.stop);
            let _ = monitor.thread.join();
        }
    }
}

impl Drop for ServiceManager {
    fn drop(&mut self) {
        self.stop_all();
    }
}

/// Spawn `command` with stdout and stderr merged into one pipe.
fn spawn_with_output(
    command: &[String],
    env: &HashMap<OsString, OsString>,
    cwd: &Path,
) -> std::io::Result<(Child, std::io::PipeReader)> {
    let (reader, writer) = std::io::pipe()?;
    let mut builder = Command::new(&command[0]);
    builder
        .args(&command[1..])
        .env_clear()
        .envs(env)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer);
    platform::isolate_process_group(&mut builder);
    let child = builder.spawn()?;
    // `builder` holds the parent's copies of the pipe's write end; drop them
    // so the reader sees end-of-stream once the child tree exits.
    drop(builder);
    Ok((child, reader))
}

/// Ask the child's whole process tree to exit, escalating to a forced kill.
fn terminate_tree(child: &mut Child, name: &str) -> ExitStatus {
    platform::request_tree_stop(child);
    if let Some(status) = wait_timeout(child, GRACEFUL_STOP_TIMEOUT) {
        return status;
    }
    tracing::warn!("Service {name} did not stop within {GRACEFUL_STOP_TIMEOUT:?}; killing it");
    platform::force_tree_stop(child);
    if let Some(status) = wait_timeout(child, FORCED_STOP_TIMEOUT) {
        return status;
    }
    let _ = child.kill();
    child.wait().unwrap_or_else(|err| {
        tracing::error!("Could not reap service {name}: {err}");
        platform::unknown_exit()
    })
}

fn wait_timeout(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(WAIT_POLL),
            _ => return None,
        }
    }
}

/// Exit code, or the negated signal number for a signal-terminated process.
fn exit_code(status: ExitStatus) -> Option<i32> {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .or_else(|| status.signal().map(|signal| -signal))
    }
    #[cfg(not(unix))]
    {
        status.code()
    }
}

fn display_code(code: Option<i32>) -> String {
    code.map_or_else(|| "unknown".to_owned(), |code| code.to_string())
}

#[cfg(unix)]
mod platform {
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    use std::process::{Child, Command, ExitStatus};

    pub fn isolate_process_group(command: &mut Command) {
        command.process_group(0);
    }

    fn signal_group(child: &Child, signal: libc::c_int) {
        let Ok(pgid) = libc::pid_t::try_from(child.id()) else {
            return;
        };
        // SAFETY: plain syscall; the group id is the child's own pid because
        // it was spawned with `process_group(0)`.
        unsafe { libc::killpg(pgid, signal) };
    }

    pub fn request_tree_stop(child: &Child) {
        signal_group(child, libc::SIGTERM);
    }

    pub fn force_tree_stop(child: &Child) {
        signal_group(child, libc::SIGKILL);
    }

    pub fn unknown_exit() -> ExitStatus {
        ExitStatus::from_raw(libc::SIGKILL)
    }
}

#[cfg(windows)]
mod platform {
    use std::os::windows::process::{CommandExt, ExitStatusExt};
    use std::process::{Child, Command, ExitStatus, Stdio};

    use windows_sys::Win32::System::Console::{
        CTRL_BREAK_EVENT, GenerateConsoleCtrlEvent, GetConsoleWindow,
    };
    use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};

    pub fn isolate_process_group(command: &mut Command) {
        // SAFETY: plain query of this process's console.
        let has_console = !unsafe { GetConsoleWindow() }.is_null();
        // Without a console of our own (a desktop launch), every console
        // child would otherwise open a window of its own.
        let hide_console = if has_console { 0 } else { CREATE_NO_WINDOW };
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | hide_console);
    }

    pub fn request_tree_stop(child: &Child) {
        // SAFETY: plain API call targeting the child's own process group.
        let delivered = unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child.id()) } != 0;
        if !delivered {
            // Without a shared console the control event cannot be delivered;
            // fall back to terminating the complete tree.
            force_tree_stop(child);
        }
    }

    pub fn force_tree_stop(child: &Child) {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    pub fn unknown_exit() -> ExitStatus {
        ExitStatus::from_raw(1)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::config::parse_manifest;

    fn manager(dir: &Path, services: &str, limits: LogLimits) -> ServiceManager {
        let manifest = parse_manifest(&format!(
            "http_port = 2800\nbind = [\"127.0.0.1\"]\n{services}"
        ))
        .unwrap();
        ServiceManager::new(&manifest, dir.join("logs"), dir, limits).unwrap()
    }

    fn status_of(manager: &ServiceManager, name: &str) -> ServiceStatus {
        manager
            .status()
            .into_iter()
            .find(|s| s.name == name)
            .unwrap()
            .status
    }

    fn wait_until(mut condition: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if condition() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        false
    }

    const IDLE: &str = "command = [\"sh\", \"-c\", \"exec sleep 60\"]";

    #[test]
    fn start_all_skips_services_with_auto_start_disabled() {
        let dir = tempfile::tempdir().unwrap();
        let manager = manager(
            dir.path(),
            &format!("[services.eager]\n{IDLE}\n[services.manual]\n{IDLE}\nauto_start = false\n"),
            LogLimits::default(),
        );
        manager.start_all();
        assert_eq!(status_of(&manager, "eager"), ServiceStatus::Running);
        assert_eq!(status_of(&manager, "manual"), ServiceStatus::Stopped);
        manager.stop_all();
        assert_eq!(status_of(&manager, "eager"), ServiceStatus::Stopped);
    }

    #[test]
    fn manual_service_stays_controllable() {
        let dir = tempfile::tempdir().unwrap();
        let manager = manager(
            dir.path(),
            &format!("[services.manual]\n{IDLE}\nauto_start = false\n"),
            LogLimits::default(),
        );
        manager.start_all();
        manager.start_service("manual").unwrap();
        let snapshot = &manager.status()[0];
        assert_eq!(snapshot.status, ServiceStatus::Running);
        assert!(snapshot.pid.is_some());

        manager.restart_service("manual").unwrap();
        let snapshot = &manager.status()[0];
        assert_eq!(snapshot.status, ServiceStatus::Running);
        assert_eq!(snapshot.restart_count, 1);

        manager.stop_service("manual").unwrap();
        let snapshot = &manager.status()[0];
        assert_eq!(snapshot.status, ServiceStatus::Stopped);
        assert_eq!(snapshot.pid, None);
        assert_eq!(snapshot.exit_code, Some(-libc::SIGTERM));
    }

    #[test]
    fn unknown_unmanaged_and_disabled_services_are_not_controllable() {
        let dir = tempfile::tempdir().unwrap();
        let manager = manager(
            dir.path(),
            &format!("[services.external]\n[services.off]\n{IDLE}\ndisabled = true\n"),
            LogLimits::default(),
        );
        assert_eq!(status_of(&manager, "external"), ServiceStatus::Unmanaged);
        assert_eq!(status_of(&manager, "off"), ServiceStatus::Disabled);
        assert_eq!(
            manager.start_service("external"),
            Err(ServiceError::Unmanaged("external".into()))
        );
        assert_eq!(
            manager.stop_service("off"),
            Err(ServiceError::Unmanaged("off".into()))
        );
        assert_eq!(
            manager.start_service("nope"),
            Err(ServiceError::Unknown("nope".into()))
        );
    }

    #[test]
    fn stdout_and_stderr_are_drained_through_rotation() {
        let dir = tempfile::tempdir().unwrap();
        let limits = LogLimits {
            max_bytes: 128,
            backup_count: 4,
        };
        let manager = manager(
            dir.path(),
            "[services.app]\ncommand = [\"sh\", \"-c\", \"echo OUT-MARKER; echo ERR-MARKER >&2; exec sleep 60\"]\n",
            limits,
        );
        manager.start_service("app").unwrap();
        let log_path = manager.log_path("app").unwrap();
        let read_all = || {
            let mut body = Vec::new();
            for index in (1..=4).rev() {
                body.extend(
                    std::fs::read(crate::log_rotation::backup_path(&log_path, index))
                        .unwrap_or_default(),
                );
            }
            body.extend(std::fs::read(&log_path).unwrap_or_default());
            String::from_utf8_lossy(&body).into_owned()
        };
        assert!(wait_until(|| {
            let body = read_all();
            body.contains("OUT-MARKER\n") && body.contains("ERR-MARKER\n")
        }));
        manager.stop_service("app").unwrap();
        for entry in std::fs::read_dir(log_path.parent().unwrap()).unwrap() {
            assert!(entry.unwrap().metadata().unwrap().len() <= 128);
        }
    }

    #[test]
    fn exited_process_is_reported_as_crashed() {
        let dir = tempfile::tempdir().unwrap();
        let manager = manager(
            dir.path(),
            "[services.app]\ncommand = [\"sh\", \"-c\", \"exit 3\"]\n",
            LogLimits::default(),
        );
        manager.start_service("app").unwrap();
        assert!(wait_until(|| {
            manager.poll_exits();
            status_of(&manager, "app") == ServiceStatus::Crashed
        }));
        assert_eq!(manager.status()[0].exit_code, Some(3));
    }

    #[test]
    fn missing_command_is_logged_and_marked_crashed() {
        let dir = tempfile::tempdir().unwrap();
        let manager = manager(
            dir.path(),
            "[services.app]\ncommand = [\"definitely-not-a-real-command-xyz\"]\n",
            LogLimits::default(),
        );
        manager.start_service("app").unwrap();
        assert_eq!(status_of(&manager, "app"), ServiceStatus::Crashed);
        let log = std::fs::read_to_string(manager.log_path("app").unwrap()).unwrap();
        assert!(log.contains("ERROR: Command not found: definitely-not-a-real-command-xyz"));
    }

    #[test]
    fn stop_kills_the_whole_process_group() {
        let dir = tempfile::tempdir().unwrap();
        let pid_file = dir.path().join("grandchild.pid");
        let manager = manager(
            dir.path(),
            &format!(
                "[services.app]\ncommand = [\"sh\", \"-c\", \"sleep 60 & echo $! > {}; wait\"]\n",
                pid_file.display()
            ),
            LogLimits::default(),
        );
        manager.start_service("app").unwrap();
        assert!(wait_until(
            || std::fs::read_to_string(&pid_file).is_ok_and(|s| s.ends_with('\n'))
        ));
        let grandchild: libc::pid_t = std::fs::read_to_string(&pid_file)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        manager.stop_service("app").unwrap();
        // SAFETY: signal 0 only probes for existence.
        assert!(wait_until(|| unsafe { libc::kill(grandchild, 0) } != 0));
    }

    #[test]
    fn command_placeholders_resolve_from_service_env() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("out.txt");
        let manager = manager(
            dir.path(),
            &format!(
                "[services.app]\ncommand = [\"sh\", \"-c\", \"echo {{LDP_TEST_ARG}} $LDP_TEST_ENV > {}\"]\n\
                 env = {{LDP_TEST_ARG = \"from-arg\", LDP_TEST_ENV = \"from-env\"}}\n",
                out.display()
            ),
            LogLimits::default(),
        );
        manager.start_service("app").unwrap();
        assert!(wait_until(
            || std::fs::read_to_string(&out).is_ok_and(|s| s == "from-arg from-env\n")
        ));
    }
}
