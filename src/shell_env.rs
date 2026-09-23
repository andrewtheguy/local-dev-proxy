//! Recover the user's login-shell `PATH` for desktop launches.
//!
//! An app launched from Finder/Dock on macOS (and some Linux desktop
//! environments) inherits a minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`)
//! rather than the login shell's. User-installed tools in `~/.local/bin`,
//! Homebrew, pyenv, etc. are then invisible and every managed service command
//! resolves to "command not found". Asking the login shell to print its `PATH`
//! recovers the real value.

use std::ffi::OsString;
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Bracketing markers isolate `PATH` from any banner text an interactive rc
/// file may print to stdout before our command runs.
const MARKER: &str = "__LOCAL_DEV_PROXY_PATH__";
const SHELL_TIMEOUT: Duration = Duration::from_secs(5);

/// Return `PATH` as seen by the user's interactive login shell, if available.
///
/// An interactive login shell (`-ilc`) sources both login files
/// (`.zprofile`/`.profile`) and interactive files (`.zshrc`/`.bashrc`), where
/// users commonly extend `PATH`.
pub fn query_login_shell_path() -> Option<String> {
    let shell = std::env::var_os("SHELL").filter(|shell| !shell.is_empty())?;
    let script = format!("printf %s \"{MARKER}${{PATH}}{MARKER}\"");
    let mut child = Command::new(&shell)
        .args(["-ilc", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .inspect_err(|err| tracing::warn!("Could not query {shell:?} for the login PATH: {err}"))
        .ok()?;

    let mut stdout = child.stdout.take()?;
    let (sender, receiver) = std::sync::mpsc::channel();
    // Detached on timeout: a process the rc files started may hold the pipe.
    std::thread::spawn(move || {
        let mut output = Vec::new();
        let _ = stdout.read_to_end(&mut output);
        let _ = sender.send(output);
    });

    let deadline = Instant::now() + SHELL_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                tracing::warn!("Timed out querying {shell:?} for the login PATH");
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    let Ok(output) = receiver.recv_timeout(remaining) else {
        tracing::warn!("Timed out reading the login PATH from {shell:?}");
        return None;
    };
    let output = String::from_utf8_lossy(&output).into_owned();
    extract_marked_path(&output)
}

fn extract_marked_path(output: &str) -> Option<String> {
    let start = output.find(MARKER)? + MARKER.len();
    let end = start + output[start..].find(MARKER)?;
    let path = &output[start..end];
    (!path.is_empty()).then(|| path.to_owned())
}

/// Merge two `PATH` strings, `resolved` entries first, order-preserving.
///
/// Entries from the login shell take precedence; any directory present only
/// in `current` is appended so nothing already visible is lost. Duplicates
/// are removed while preserving first-seen order.
pub fn merge_path(current: &str, resolved: &str) -> String {
    let separator = if cfg!(windows) { ';' } else { ':' };
    let mut seen = std::collections::HashSet::new();
    resolved
        .split(separator)
        .chain(current.split(separator))
        .filter(|entry| !entry.is_empty() && seen.insert(*entry))
        .collect::<Vec<_>>()
        .join(&separator.to_string())
}

/// Repair this process's `PATH` from the login shell; returns true if changed.
///
/// A no-op on Windows, which has no equivalent desktop-launch `PATH` gap. Must
/// run before any other thread starts and before any service captures the
/// environment.
pub fn restore_login_shell_path() -> bool {
    if cfg!(windows) {
        return false;
    }
    let Some(resolved) = query_login_shell_path() else {
        return false;
    };
    let current = std::env::var_os("PATH")
        .map(OsString::into_string)
        .and_then(Result::ok)
        .unwrap_or_default();
    let merged = merge_path(&current, &resolved);
    if merged == current {
        return false;
    }
    // SAFETY: called at startup from `main` before any other thread exists.
    unsafe { std::env::set_var("PATH", merged) };
    tracing::info!("Merged the login shell's PATH into the environment");
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marked_path_is_extracted_from_noisy_output() {
        let output = format!("welcome banner\n{MARKER}/opt/bin:/usr/bin{MARKER}trailing");
        assert_eq!(
            extract_marked_path(&output).as_deref(),
            Some("/opt/bin:/usr/bin")
        );
        assert_eq!(extract_marked_path("no markers"), None);
        assert_eq!(extract_marked_path(&format!("{MARKER}{MARKER}")), None);
        assert_eq!(extract_marked_path(&format!("{MARKER}/unterminated")), None);
    }

    #[cfg(unix)]
    #[test]
    fn merge_prefers_resolved_and_keeps_current_extras() {
        assert_eq!(
            merge_path("/usr/bin:/bin:/extra", "/opt/homebrew/bin:/usr/bin:/bin"),
            "/opt/homebrew/bin:/usr/bin:/bin:/extra"
        );
        assert_eq!(merge_path("/a::/a", ""), "/a");
    }
}
