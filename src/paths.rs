//! Profile-rooted application paths.
//!
//! Every path the application touches (configuration, logs, instance lock,
//! activation channel) is derived from a single profile root so an isolated
//! development or test profile can never contend with the normal one.

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

pub const APP_NAME: &str = "local-dev-proxy";
pub const ORGANIZATION_NAME: &str = "andrewtheguy";
/// Explicit development/test override for the profile root.
pub const CONFIG_DIR_ENV: &str = "LOCAL_DEV_PROXY_CONFIG_DIR";

/// All application paths derived from one canonical profile root.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectPaths {
    root: PathBuf,
}

impl ProjectPaths {
    /// Build paths for `root`, expanding `~` and making it absolute.
    ///
    /// An existing root is canonicalized so that equivalent spellings (and
    /// symlinks such as macOS `/tmp`) map to the same profile identity.
    pub fn new(root: impl AsRef<Path>) -> io::Result<Self> {
        let expanded = expand_tilde(root.as_ref());
        let absolute = std::path::absolute(&expanded)?;
        let root = absolute.canonicalize().unwrap_or(absolute);
        Ok(Self { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn services_file(&self) -> PathBuf {
        self.root.join("services.toml")
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.root.join("logs")
    }

    pub fn manager_log(&self) -> PathBuf {
        self.logs_dir().join("manager.log")
    }

    pub fn instance_lock(&self) -> PathBuf {
        self.root.join(".instance.lock")
    }

    /// Create the profile and log directories without creating configuration.
    ///
    /// Idempotent. The root is re-canonicalized afterwards because it may not
    /// have existed when these paths were built.
    pub fn ensure(self) -> io::Result<Self> {
        std::fs::create_dir_all(&self.root)?;
        std::fs::create_dir_all(self.logs_dir())?;
        Ok(Self {
            root: self.root.canonicalize()?,
        })
    }
}

/// Return the platform-standard per-user application config directory.
///
/// `LOCAL_DEV_PROXY_CONFIG_DIR` is an explicit development/test override.
/// Otherwise this follows the native convention: `~/Library/Preferences` on
/// macOS, `%APPDATA%` on Windows, and `$XDG_CONFIG_HOME` (or `~/.config`) on
/// Linux, each followed by `andrewtheguy/local-dev-proxy`.
pub fn user_config_dir() -> io::Result<PathBuf> {
    user_config_dir_from(std::env::var_os(CONFIG_DIR_ENV))
}

fn user_config_dir_from(override_dir: Option<OsString>) -> io::Result<PathBuf> {
    if let Some(dir) = override_dir.filter(|dir| !dir.is_empty()) {
        return Ok(PathBuf::from(dir));
    }
    let base = dirs::preference_dir().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "The operating system did not provide a config directory",
        )
    })?;
    Ok(base.join(ORGANIZATION_NAME).join(APP_NAME))
}

/// Resolve the profile paths, honouring the environment override.
pub fn default_paths() -> io::Result<ProjectPaths> {
    ProjectPaths::new(user_config_dir()?)
}

fn expand_tilde(path: &Path) -> PathBuf {
    let Ok(rest) = path.strip_prefix("~") else {
        return path.to_path_buf();
    };
    match dirs::home_dir() {
        Some(home) => home.join(rest),
        None => path.to_path_buf(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn environment_override_selects_an_isolated_profile() {
        let dir = tempfile::tempdir().unwrap();
        let chosen = user_config_dir_from(Some(dir.path().as_os_str().to_owned())).unwrap();
        assert_eq!(chosen, dir.path());
    }

    #[test]
    fn default_profile_uses_platform_preference_dir() {
        let chosen = user_config_dir_from(None).unwrap();
        let expected = dirs::preference_dir()
            .unwrap()
            .join("andrewtheguy")
            .join("local-dev-proxy");
        assert_eq!(chosen, expected);
    }

    #[test]
    fn one_profile_root_determines_every_path() {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProjectPaths::new(dir.path().join("profile"))
            .unwrap()
            .ensure()
            .unwrap();
        let root = dir.path().canonicalize().unwrap().join("profile");
        assert_eq!(paths.root(), root);
        assert_eq!(paths.services_file(), root.join("services.toml"));
        assert_eq!(paths.logs_dir(), root.join("logs"));
        assert_eq!(paths.manager_log(), root.join("logs").join("manager.log"));
        assert_eq!(paths.instance_lock(), root.join(".instance.lock"));
        assert!(paths.logs_dir().is_dir());
    }

    #[test]
    fn ensure_does_not_create_or_replace_configuration() {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProjectPaths::new(dir.path()).unwrap().ensure().unwrap();
        assert!(!paths.services_file().exists());
        std::fs::write(paths.services_file(), "custom").unwrap();
        let paths = paths.ensure().unwrap();
        assert_eq!(
            std::fs::read_to_string(paths.services_file()).unwrap(),
            "custom"
        );
    }

    #[test]
    fn tilde_expands_to_home() {
        let home = dirs::home_dir().unwrap();
        assert_eq!(expand_tilde(Path::new("~/x")), home.join("x"));
        assert_eq!(expand_tilde(Path::new("/abs")), PathBuf::from("/abs"));
    }
}
