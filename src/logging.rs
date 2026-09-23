//! Manager logging to stderr and the profile's rotating `manager.log`.

use std::io::{self, IsTerminal};
use std::path::Path;

use tracing_subscriber::filter::LevelFilter;
use tracing_subscriber::fmt::time::ChronoLocal;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

use crate::log_rotation::{LogLimits, RotatingLogWriter, SharedLog};

const TIME_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

/// Install the global subscriber. Call once, before any other thread starts
/// logging.
pub fn init(manager_log: &Path) -> io::Result<()> {
    let file = SharedLog::new(RotatingLogWriter::open(manager_log, LogLimits::default())?);
    let file_layer = tracing_subscriber::fmt::layer()
        .with_writer(file)
        .with_ansi(false)
        .with_target(false)
        .with_timer(ChronoLocal::new(TIME_FORMAT.to_owned()));
    let stderr_layer = tracing_subscriber::fmt::layer()
        .with_writer(io::stderr)
        .with_ansi(io::stderr().is_terminal())
        .with_target(false)
        .with_timer(ChronoLocal::new(TIME_FORMAT.to_owned()));
    tracing_subscriber::registry()
        .with(LevelFilter::INFO)
        .with(file_layer)
        .with(stderr_layer)
        .try_init()
        .map_err(io::Error::other)
}
