use std::ffi::OsString;
use std::process::ExitCode;
use std::sync::mpsc;
use std::time::Duration;

use local_dev_proxy::config::load_manifest;
use local_dev_proxy::frontend::{AppEvent, Frontend, Headless};
use local_dev_proxy::instance::{
    ActivationServer, LockError, acquire_instance_lock, activate_running_instance,
};
use local_dev_proxy::manager::Manager;
use local_dev_proxy::paths::{CONFIG_DIR_ENV, ProjectPaths, default_paths};
use local_dev_proxy::routes::RouteTable;
use local_dev_proxy::{SAMPLE_CONFIG, VERSION, logging, shell_env};

const ACTIVATION_TIMEOUT: Duration = Duration::from_millis(1500);

#[derive(Debug, PartialEq, Eq)]
enum Command {
    Run,
    CheckConfig,
    SampleConfig,
    Version,
    Help,
}

fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Command, String> {
    let args: Vec<OsString> = args.into_iter().collect();
    match args.as_slice() {
        [] => Ok(Command::Run),
        [arg] => match arg.to_str() {
            Some("--check-config") => Ok(Command::CheckConfig),
            Some("--sample-config") => Ok(Command::SampleConfig),
            Some("-V" | "--version") => Ok(Command::Version),
            Some("-h" | "--help") => Ok(Command::Help),
            _ => Err(format!("unrecognized argument: {}", arg.to_string_lossy())),
        },
        _ => Err("expected at most one argument".to_owned()),
    }
}

fn usage() -> String {
    format!(
        "local-dev-proxy {VERSION}\n\
         Local development process manager with a built-in reverse proxy.\n\n\
         Usage: local-dev-proxy [OPTION]\n\n\
         With no option, starts the proxy and services from the profile's services.toml\n\
         and runs until interrupted.\n\n\
         Options:\n  \
           --check-config   Validate the profile's services.toml and exit\n  \
           --sample-config  Print a reference services.toml and exit\n  \
           -V, --version    Print the version and exit\n  \
           -h, --help       Print this help and exit\n\n\
         Environment:\n  \
           {CONFIG_DIR_ENV}  Use an isolated profile directory\n"
    )
}

fn main() -> ExitCode {
    match parse_args(std::env::args_os().skip(1)) {
        Ok(Command::Run) => run(),
        Ok(Command::CheckConfig) => check_config(),
        Ok(Command::SampleConfig) => {
            print!("{SAMPLE_CONFIG}");
            ExitCode::SUCCESS
        }
        Ok(Command::Version) => {
            println!("local-dev-proxy {VERSION}");
            ExitCode::SUCCESS
        }
        Ok(Command::Help) => {
            print!("{}", usage());
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("error: {message}\n\n{}", usage());
            ExitCode::from(2)
        }
    }
}

fn profile() -> Result<ProjectPaths, ExitCode> {
    default_paths()
        .and_then(ProjectPaths::ensure)
        .map_err(|err| {
            eprintln!("error: could not prepare the profile directory: {err}");
            ExitCode::FAILURE
        })
}

fn check_config() -> ExitCode {
    let paths = match profile() {
        Ok(paths) => paths,
        Err(code) => return code,
    };
    let services_file = paths.services_file();
    let result = load_manifest(&services_file).and_then(|manifest| {
        RouteTable::from_manifest(&manifest, &|key| std::env::var(key).ok(), paths.root())
            .map(|_| manifest)
    });
    match result {
        Ok(manifest) => {
            println!(
                "{}: OK ({} services)",
                services_file.display(),
                manifest.services.len()
            );
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("{}: {err}", services_file.display());
            ExitCode::FAILURE
        }
    }
}

fn run() -> ExitCode {
    let paths = match profile() {
        Ok(paths) => paths,
        Err(code) => return code,
    };

    let lock = match acquire_instance_lock(&paths) {
        Ok(lock) => lock,
        Err(LockError::AlreadyRunning) => {
            if activate_running_instance(&paths, ACTIVATION_TIMEOUT) {
                eprintln!("local-dev-proxy is already running; asked it to show itself.");
            } else {
                eprintln!("local-dev-proxy is already running but did not answer activation.");
            }
            return ExitCode::SUCCESS;
        }
        Err(err) => {
            eprintln!("error: {err}");
            return ExitCode::FAILURE;
        }
    };

    if let Err(err) = logging::init(&paths.manager_log()) {
        eprintln!(
            "error: could not open {}: {err}",
            paths.manager_log().display()
        );
        return ExitCode::FAILURE;
    }
    tracing::info!(
        "local-dev-proxy {VERSION} using profile {}",
        paths.root().display()
    );

    // Recover the login-shell PATH before any service captures the
    // environment and before the runtime starts its threads.
    shell_env::restore_login_shell_path();

    let manager = match Manager::new(paths.clone()) {
        Ok(manager) => manager,
        Err(err) => {
            tracing::error!("Could not start the async runtime: {err}");
            return ExitCode::FAILURE;
        }
    };

    let (events, receiver) = mpsc::channel();
    let activation_events = events.clone();
    let activation = match ActivationServer::start(&paths, &lock, move || {
        let _ = activation_events.send(AppEvent::Activate);
    }) {
        Ok(server) => server,
        Err(err) => {
            tracing::error!("Could not initialize the application activation channel: {err}");
            return ExitCode::FAILURE;
        }
    };
    manager.runtime().spawn(async move {
        wait_for_shutdown_signal().await;
        let _ = events.send(AppEvent::Shutdown);
    });

    let frontend: Box<dyn Frontend> = Box::new(Headless);
    let code = frontend.run(manager, receiver);
    drop(activation);
    drop(lock);
    code
}

#[cfg(unix)]
async fn wait_for_shutdown_signal() {
    use tokio::signal::unix::{SignalKind, signal};

    let (Ok(mut terminate), Ok(mut interrupt), Ok(mut hangup)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
        signal(SignalKind::hangup()),
    ) else {
        tracing::warn!("Could not install signal handlers");
        return std::future::pending().await;
    };
    tokio::select! {
        _ = terminate.recv() => {}
        _ = interrupt.recv() => {}
        _ = hangup.recv() => {}
    }
}

#[cfg(windows)]
async fn wait_for_shutdown_signal() {
    use tokio::signal::windows::{ctrl_break, ctrl_c, ctrl_close};

    let (Ok(mut ctrl_c), Ok(mut ctrl_break), Ok(mut ctrl_close)) =
        (ctrl_c(), ctrl_break(), ctrl_close())
    else {
        tracing::warn!("Could not install console control handlers");
        return std::future::pending().await;
    };
    tokio::select! {
        _ = ctrl_c.recv() => {}
        _ = ctrl_break.recv() => {}
        _ = ctrl_close.recv() => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Command, String> {
        parse_args(args.iter().map(OsString::from))
    }

    #[test]
    fn arguments_select_commands() {
        assert_eq!(parse(&[]), Ok(Command::Run));
        assert_eq!(parse(&["--check-config"]), Ok(Command::CheckConfig));
        assert_eq!(parse(&["--sample-config"]), Ok(Command::SampleConfig));
        assert_eq!(parse(&["-V"]), Ok(Command::Version));
        assert_eq!(parse(&["--help"]), Ok(Command::Help));
        assert!(parse(&["--bogus"]).is_err());
        assert!(parse(&["--help", "--version"]).is_err());
    }
}
