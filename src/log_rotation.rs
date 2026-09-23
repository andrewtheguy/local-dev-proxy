//! Size-bounded, rename-rotated log files.
//!
//! Rotation renames completed files (`.log` → `.log.1` → … → `.log.N`) rather
//! than truncating a file that is being written; `.log.1` is the newest
//! backup and only the oldest backup is ever deleted.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub const LOG_MAX_BYTES: u64 = 10 * 1024 * 1024;
pub const LOG_BACKUP_COUNT: u32 = 5;
const READ_SIZE: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LogLimits {
    pub max_bytes: u64,
    pub backup_count: u32,
}

impl Default for LogLimits {
    fn default() -> Self {
        Self {
            max_bytes: LOG_MAX_BYTES,
            backup_count: LOG_BACKUP_COUNT,
        }
    }
}

/// Writes bytes to a size-bounded, rename-rotated log file.
#[derive(Debug)]
pub struct RotatingLogWriter {
    path: PathBuf,
    limits: LogLimits,
    file: File,
    size: u64,
}

impl RotatingLogWriter {
    pub fn open(path: impl Into<PathBuf>, limits: LogLimits) -> io::Result<Self> {
        if limits.max_bytes < 1 || limits.backup_count < 1 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "log limits must be positive",
            ));
        }
        let path = path.into();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let (file, size) = open_append(&path)?;
        Ok(Self {
            path,
            limits,
            file,
            size,
        })
    }

    fn backup_path(&self, index: u32) -> PathBuf {
        backup_path(&self.path, index)
    }

    fn rotate(&mut self) -> io::Result<()> {
        let oldest = self.backup_path(self.limits.backup_count);
        match std::fs::remove_file(&oldest) {
            Err(err) if err.kind() != io::ErrorKind::NotFound => return Err(err),
            _ => {}
        }
        for index in (1..self.limits.backup_count).rev() {
            let source = self.backup_path(index);
            if source.exists() {
                std::fs::rename(&source, self.backup_path(index + 1))?;
            }
        }
        // Renaming the open file is safe: std opens files with delete
        // sharing on Windows, and the handle is replaced right after.
        if self.path.exists() {
            std::fs::rename(&self.path, self.backup_path(1))?;
        }
        let (file, size) = open_append(&self.path)?;
        self.file = file;
        self.size = size;
        Ok(())
    }

    /// Append `data`, splitting it across files so none exceeds the cap.
    pub fn write_bytes(&mut self, mut data: &[u8]) -> io::Result<()> {
        while !data.is_empty() {
            if self.size >= self.limits.max_bytes {
                self.rotate()?;
            }
            let room = (self.limits.max_bytes - self.size).min(data.len() as u64) as usize;
            self.file.write_all(&data[..room])?;
            self.size += room as u64;
            data = &data[room..];
        }
        self.file.flush()
    }
}

impl Write for RotatingLogWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.write_bytes(buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

/// Path of backup `index`; `name.log.1` is the newest.
pub fn backup_path(path: &Path, index: u32) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{index}"));
    path.with_file_name(name)
}

fn open_append(path: &Path) -> io::Result<(File, u64)> {
    let file = OpenOptions::new().create(true).append(true).open(path)?;
    let size = file.metadata()?.len();
    Ok((file, size))
}

/// A cloneable, thread-safe handle to a rotating log, usable as a
/// `tracing_subscriber` writer.
#[derive(Debug, Clone)]
pub struct SharedLog(Arc<Mutex<RotatingLogWriter>>);

impl SharedLog {
    pub fn new(writer: RotatingLogWriter) -> Self {
        Self(Arc::new(Mutex::new(writer)))
    }
}

impl Write for SharedLog {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let mut writer = self.0.lock().unwrap_or_else(|poison| poison.into_inner());
        writer.write_bytes(buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        let mut writer = self.0.lock().unwrap_or_else(|poison| poison.into_inner());
        writer.flush()
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for SharedLog {
    type Writer = SharedLog;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// Drain `source` into `writer` until end of stream.
///
/// Once `discard` is set, remaining bytes are still drained (so a lingering
/// grandchild holding the pipe never blocks on a full buffer) but no longer
/// written. A write failure likewise disables writing without stopping the
/// drain.
pub fn pump_log_stream(mut source: impl Read, mut writer: RotatingLogWriter, discard: &AtomicBool) {
    let mut buffer = vec![0u8; READ_SIZE];
    let mut write_enabled = true;
    loop {
        let read = match source.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => read,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        if write_enabled
            && !discard.load(Ordering::Relaxed)
            && writer.write_bytes(&buffer[..read]).is_err()
        {
            write_enabled = false;
        }
    }
}

/// Read the last `lines` lines of `path` without loading the whole file.
///
/// Returns an empty string if the file does not exist or cannot be read.
pub fn tail_file(path: &Path, lines: usize) -> String {
    use std::io::{Seek, SeekFrom};

    if lines == 0 {
        return String::new();
    }
    let Ok(mut file) = File::open(path) else {
        return String::new();
    };
    const CHUNK: u64 = 8192;
    let Ok(mut position) = file.seek(SeekFrom::End(0)) else {
        return String::new();
    };
    let mut buffer: Vec<u8> = Vec::new();
    while position > 0 && buffer.iter().filter(|b| **b == b'\n').count() <= lines {
        let read_size = CHUNK.min(position);
        position -= read_size;
        let mut chunk = vec![0u8; read_size as usize];
        if file.seek(SeekFrom::Start(position)).is_err() || file.read_exact(&mut chunk).is_err() {
            return String::new();
        }
        chunk.extend_from_slice(&buffer);
        buffer = chunk;
    }
    let text = String::from_utf8_lossy(&buffer);
    let all: Vec<&str> = text.lines().collect();
    let tail = &all[all.len().saturating_sub(lines)..];
    if tail.is_empty() {
        return String::new();
    }
    let mut out = tail.join("\n");
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits(max_bytes: u64, backup_count: u32) -> LogLimits {
        LogLimits {
            max_bytes,
            backup_count,
        }
    }

    #[test]
    fn rotating_writer_preserves_newest_bytes_within_cap() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("service.log");
        let mut writer = RotatingLogWriter::open(&path, limits(10, 2)).unwrap();
        writer
            .write_bytes(b"0123456789abcdefghijKLMNOPQRSTuvwxyz")
            .unwrap();
        drop(writer);

        assert_eq!(std::fs::read(&path).unwrap(), b"uvwxyz");
        assert_eq!(std::fs::read(backup_path(&path, 1)).unwrap(), b"KLMNOPQRST");
        assert_eq!(std::fs::read(backup_path(&path, 2)).unwrap(), b"abcdefghij");
        assert!(!backup_path(&path, 3).exists());
    }

    #[test]
    fn rotating_writer_appends_to_existing_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("service.log");
        std::fs::write(&path, b"12345678").unwrap();
        let mut writer = RotatingLogWriter::open(&path, limits(10, 1)).unwrap();
        writer.write_bytes(b"abcd").unwrap();
        drop(writer);
        assert_eq!(std::fs::read(&path).unwrap(), b"cd");
        assert_eq!(std::fs::read(backup_path(&path, 1)).unwrap(), b"12345678ab");
    }

    #[test]
    fn invalid_limits_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("x.log");
        assert!(RotatingLogWriter::open(&path, limits(0, 1)).is_err());
        assert!(RotatingLogWriter::open(&path, limits(1, 0)).is_err());
    }

    #[test]
    fn pump_drains_stream_and_honours_discard() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("pump.log");
        let writer = RotatingLogWriter::open(&path, LogLimits::default()).unwrap();
        pump_log_stream(&b"hello\n"[..], writer, &AtomicBool::new(false));
        assert_eq!(std::fs::read(&path).unwrap(), b"hello\n");

        let writer = RotatingLogWriter::open(&path, LogLimits::default()).unwrap();
        pump_log_stream(&b"dropped\n"[..], writer, &AtomicBool::new(true));
        assert_eq!(std::fs::read(&path).unwrap(), b"hello\n");
    }

    #[test]
    fn tail_returns_last_lines() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("tail.log");
        let body: String = (0..5000).map(|i| format!("line {i}\n")).collect();
        std::fs::write(&path, body).unwrap();
        assert_eq!(tail_file(&path, 2), "line 4998\nline 4999\n");
        assert_eq!(tail_file(&path, 0), "");
        assert_eq!(tail_file(&dir.path().join("missing.log"), 5), "");
        std::fs::write(&path, "a\nb").unwrap();
        assert_eq!(tail_file(&path, 10), "a\nb\n");
    }
}
