//! Single-instance guard and the local activation channel.
//!
//! The first launch for a profile holds an exclusive lock on the profile's
//! `.instance.lock` and listens on a profile-specific local socket (a Unix
//! domain socket, or a named pipe on Windows). A later launch fails to take the
//! lock, connects to that socket to ask the running instance to show itself,
//! and exits.

use std::fs::{File, OpenOptions, TryLockError};
use std::io::{self, Read, Write};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use interprocess::local_socket::{
    GenericNamespaced, ListenerNonblockingMode, ListenerOptions, Stream, ToNsName, prelude::*,
};
use sha2::{Digest, Sha256};

use crate::paths::{APP_NAME, ProjectPaths};

const ACTIVATE_MESSAGE: u8 = 0;
const ACCEPT_POLL: Duration = Duration::from_millis(100);
const CONNECT_RETRY: Duration = Duration::from_millis(50);
const CLIENT_IO_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Debug, thiserror::Error)]
pub enum LockError {
    #[error("local-dev-proxy is already running for this profile")]
    AlreadyRunning,
    #[error("could not open the instance lock: {0}")]
    Io(#[from] io::Error),
}

/// Held for the manager's lifetime; dropping it releases the lock.
#[derive(Debug)]
pub struct InstanceLock {
    _file: File,
}

/// Take the profile's single-instance lock without blocking.
pub fn acquire_instance_lock(paths: &ProjectPaths) -> Result<InstanceLock, LockError> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(paths.instance_lock())?;
    match file.try_lock() {
        Ok(()) => Ok(InstanceLock { _file: file }),
        Err(TryLockError::WouldBlock) => Err(LockError::AlreadyRunning),
        Err(TryLockError::Error(err)) => Err(LockError::Io(err)),
    }
}

/// A stable, profile-specific local socket name.
pub fn instance_socket_name(paths: &ProjectPaths) -> String {
    let root = paths.root().to_string_lossy();
    let root = if cfg!(windows) {
        root.to_lowercase()
    } else {
        root.into_owned()
    };
    let digest = Sha256::digest(root.as_bytes());
    let hex: String = digest[..10]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    format!("{APP_NAME}-{hex}")
}

/// Ask the running instance to show itself; true if it was reached.
///
/// Retries for up to `timeout` to cover the window in which the primary
/// holds its lock but has not started listening yet.
pub fn activate_running_instance(paths: &ProjectPaths, timeout: Duration) -> bool {
    let name = instance_socket_name(paths);
    let deadline = Instant::now() + timeout;
    loop {
        if send_activation(&name).is_ok() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(CONNECT_RETRY);
    }
}

fn send_activation(name: &str) -> io::Result<()> {
    let mut stream = Stream::connect(name.to_ns_name::<GenericNamespaced>()?)?;
    timeout_if_supported(stream.set_send_timeout(Some(CLIENT_IO_TIMEOUT)))?;
    stream.write_all(&[ACTIVATE_MESSAGE])?;
    stream.flush()
}

/// Windows named pipes have no I/O timeouts, and `interprocess` reports that
/// as `Unsupported` rather than ignoring it. The exchange is one byte against
/// a listener that is known to be alive, so going without is acceptable there.
fn timeout_if_supported(result: io::Result<()>) -> io::Result<()> {
    match result {
        Err(err) if err.kind() == io::ErrorKind::Unsupported => Ok(()),
        other => other,
    }
}

/// Receives activation requests from later launches on a background thread.
#[derive(Debug)]
pub struct ActivationServer {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl ActivationServer {
    /// Listen for activation requests, calling `on_activate` for each one.
    ///
    /// The instance lock must already be held: any existing endpoint with this
    /// name can then only be stale data left by a terminated process, so it is
    /// overwritten.
    pub fn start(
        paths: &ProjectPaths,
        _lock: &InstanceLock,
        on_activate: impl Fn() + Send + 'static,
    ) -> io::Result<Self> {
        let name = instance_socket_name(paths);
        let listener = ListenerOptions::new()
            .name(name.to_ns_name::<GenericNamespaced>()?)
            .try_overwrite(true)
            .nonblocking(ListenerNonblockingMode::Accept)
            .create_sync()?;
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let thread = std::thread::Builder::new()
            .name("activation-server".into())
            .spawn(move || {
                while !thread_stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok(mut stream) => {
                            let _ = stream.set_nonblocking(false);
                            let _ = timeout_if_supported(
                                stream.set_recv_timeout(Some(CLIENT_IO_TIMEOUT)),
                            );
                            let mut message = [0u8; 1];
                            if stream.read_exact(&mut message).is_ok()
                                && message[0] == ACTIVATE_MESSAGE
                            {
                                on_activate();
                            }
                        }
                        Err(err) if err.kind() == io::ErrorKind::WouldBlock => {
                            std::thread::sleep(ACCEPT_POLL);
                        }
                        Err(err) => {
                            tracing::warn!("Activation channel accept failed: {err}");
                            std::thread::sleep(ACCEPT_POLL);
                        }
                    }
                }
            })?;
        Ok(Self {
            stop,
            thread: Some(thread),
        })
    }
}

impl Drop for ActivationServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn profile() -> (tempfile::TempDir, ProjectPaths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProjectPaths::new(dir.path()).unwrap().ensure().unwrap();
        (dir, paths)
    }

    #[test]
    fn socket_name_is_stable_and_profile_specific() {
        let (_a, first) = profile();
        let (_b, second) = profile();
        assert_eq!(instance_socket_name(&first), instance_socket_name(&first));
        assert_ne!(instance_socket_name(&first), instance_socket_name(&second));
        assert!(instance_socket_name(&first).starts_with("local-dev-proxy-"));
        assert_eq!(
            instance_socket_name(&first).len(),
            "local-dev-proxy-".len() + 20
        );
    }

    #[test]
    fn second_lock_is_rejected_until_released() {
        let (_dir, paths) = profile();
        let lock = acquire_instance_lock(&paths).unwrap();
        assert!(matches!(
            acquire_instance_lock(&paths),
            Err(LockError::AlreadyRunning)
        ));
        drop(lock);
        acquire_instance_lock(&paths).unwrap();
    }

    #[test]
    fn activation_reaches_the_running_instance() {
        let (_dir, paths) = profile();
        let lock = acquire_instance_lock(&paths).unwrap();
        let (tx, rx) = mpsc::channel();
        let server = ActivationServer::start(&paths, &lock, move || {
            let _ = tx.send(());
        })
        .unwrap();

        assert!(activate_running_instance(&paths, Duration::from_secs(2)));
        rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(activate_running_instance(&paths, Duration::from_secs(2)));
        rx.recv_timeout(Duration::from_secs(2)).unwrap();

        drop(server);
        // A child that another test forked but has not yet exec'd holds a
        // copy of every open descriptor, the listener's included, so the
        // socket can outlive the server for a moment. Wait for that window
        // to close rather than sampling once.
        let deadline = Instant::now() + Duration::from_secs(5);
        while activate_running_instance(&paths, Duration::ZERO) {
            assert!(
                Instant::now() < deadline,
                "activation still reached something after the server was dropped"
            );
            std::thread::sleep(CONNECT_RETRY);
        }
    }
}
