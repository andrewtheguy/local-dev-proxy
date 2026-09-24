Local dev proxy: a built-in reverse proxy plus a process manager for local development,
shipped as a single self-contained binary.

> **No backward compatibility while on `v0.x`.** Any release may make breaking changes to
> the configuration format or behavior without a deprecation path. Pin to an exact version.

It opens a manager window with a system-tray (menu bar) icon for controlling services,
editing the configuration, reading logs, and browsing routes. `--headless` runs the same
proxy and services with no window, controlled with signals.

## Installation

Cross-platform (macOS, Windows, Linux). The binary embeds its UI, assets, and proxy; on
macOS and Windows there is nothing else to install. On Linux it links the system
fontconfig library (`libfontconfig.so.1`), which desktop distributions ship by default;
on a minimal system install `libfontconfig1` (Debian/Ubuntu) or `fontconfig` (Fedora).

### Download a release binary

Download the file for your platform from the
[Releases page](https://github.com/andrewtheguy/local-dev-proxy/releases):

- **macOS (Apple silicon only):** open `local-dev-proxy-macos-arm64.dmg` and drag
  `Local Dev Proxy.app` into `/Applications`. The command-line binary is
  `/Applications/Local Dev Proxy.app/Contents/MacOS/local-dev-proxy`.
- **Windows:** download `local-dev-proxy-windows-amd64.exe` (stable releases only;
  prereleases have no Windows build).
- **Linux:** download `local-dev-proxy-linux-amd64` or `local-dev-proxy-linux-arm64`,
  `chmod +x` it, and put it on your `PATH`. The window needs an X11 or Wayland session;
  the tray icon needs a StatusNotifierItem host (KDE, or GNOME with the AppIndicator
  extension).

Release builds are unsigned, so macOS Gatekeeper or Windows SmartScreen may ask you to
approve the first launch. If macOS says the app is damaged, clear the download quarantine:

```shell
xattr -dr com.apple.quarantine "/Applications/Local Dev Proxy.app"
```

### Build from source

Requires a Rust toolchain. On Linux, also install the fontconfig
development package (`libfontconfig1-dev` on Debian/Ubuntu, `fontconfig-devel` on
Fedora):

```shell
cargo install --locked --git https://github.com/andrewtheguy/local-dev-proxy.git --tag vx.y.z

# Or from a local checkout
cargo install --locked --path .
```

### Run without installing

```shell
cargo run --release
```

## What you get

Nothing is configured on first launch (see [Configuration](#configuration)). Once a
configuration is saved, each route is served at `http://<host>:<http_port>` for the
hosts it lists, and `http://localhost:<http_port>` shows a portal linking to every
route. Managed services' commands must be on your `PATH`.

## Configuration

Configuration lives in the platform-standard per-user application configuration
directory:

| Platform | Default directory |
|----------|-------------------|
| macOS | `~/Library/Preferences/andrewtheguy/local-dev-proxy/` |
| Windows | `%APPDATA%\andrewtheguy\local-dev-proxy\` |
| Linux | `$XDG_CONFIG_HOME/andrewtheguy/local-dev-proxy/` (or `~/.config/...`) |

The application does not seed `services.toml`. Without one, the manager window opens in
the configuration editor; save a configuration there (or run Start All) to create the
file. `--headless` instead exits with an error that names the expected path.
`local-dev-proxy --sample-config` prints a reference configuration; it is never written
into the profile and is not intended to run unchanged.

`services.toml` holds proxy settings (`http_port`, `bind`), service commands,
environment values, ports, and routes. Route targets can be TCP ports or Unix domain
sockets. Logs are written in the `logs/` directory beside it.

For an isolated development or test profile, set
`LOCAL_DEV_PROXY_CONFIG_DIR=/path/to/profile`. The selected profile controls the
configuration, logs, single-instance lock, and application activation channel. This
directory is the single profile root; every application path is derived from it, so
paths cannot be configured into inconsistent combinations and the profile does not
touch or contend with the normal profile.
`http_port` and `bind` are required, values are type-checked without coercion, and
unknown keys are rejected rather than silently treated as an older config shape.

Manager and service logs rotate at 10 MiB, retaining five numbered backups alongside
the active file (`.log.1` is newest). This bounds each log to about 60 MiB. Rotation
renames completed files rather than truncating a file while it is being written; once
the retention limit is reached, only the oldest backup is removed.

Edit the configuration in the manager window (**Edit Config**, then **Validate**,
**Save**, or **Apply**) while everything keeps running, or edit the file by hand, check
it with `local-dev-proxy --check-config`, and apply it from the editor (or restart the
application).

## Usage

```sh
local-dev-proxy                  # open the manager window and run the proxy and services
local-dev-proxy --headless       # run the proxy and services without a window until interrupted
local-dev-proxy --check-config   # validate services.toml (including routes) and exit
local-dev-proxy --sample-config  # print a reference services.toml
local-dev-proxy --version
```

On start the application loads `services.toml`, launches every managed service with
`auto_start` enabled, and binds the proxy on each `bind` address. Service status,
proxied requests, and errors are logged to stderr and to `logs/manager.log`; each
service's combined stdout/stderr goes to `logs/<service>.log`.

The manager window has three tabs:

- **Services** — each service's status, PID, restart count, and last exit code. Select a
  row to start, stop, or restart it; double-click a row to open its log. **Edit Config**
  opens the configuration editor (with TOML syntax highlighting) while the proxy and
  services keep running. **Save** only writes the file; **Apply** validates and saves it,
  then restarts the proxy and every service only if the configuration differs from the
  running one (comment and formatting changes do not count). While stopped the button is
  **Start All** instead. If the configuration fails to start, everything stays stopped
  and the editor opens with the error.
- **Logs** — the tail of a service's log, with a line count and **Follow** to keep it
  updating.
- **Routes** — every service's hosts and targets. Click a URL to open it in the browser.

Every control also has a keyboard shortcut (Command instead of Ctrl on macOS). A
shortcut does exactly what its button would do and is inert while that button is
absent or disabled:

| Shortcut | Action |
| --- | --- |
| Ctrl+1 / Ctrl+2 / Ctrl+3 | Services / Logs / Routes tab |
| Ctrl+Up / Ctrl+Down | Select the previous / next service |
| Ctrl+Shift+S / Ctrl+Shift+X / Ctrl+Shift+R | Start / Stop / Restart the selected service |
| Ctrl+L | Open the selected service's log |
| Ctrl+E | Edit Config, or back to the service list |
| Ctrl+K / Ctrl+S | Validate / Save the configuration being edited |
| Ctrl+Enter | Apply (validate, save, and restart if it changed), or Start All while stopped |
| Ctrl+R | Reload what the current tab shows: the editor from disk, the log, or the routes |
| Ctrl+Q | Quit |

Only one instance runs per profile. Launching it again while it is running brings the
running instance's window to the front and exits immediately (a headless instance only
logs the request).

Before launching services, the application merges the `PATH` reported by your login
shell into its own environment, so tools installed in `~/.local/bin`, Homebrew, and
similar locations resolve even when the binary is started outside a terminal.

### Lifecycle

- **Closing the manager window** → hides it; the proxy and services keep running. Reopen
  it from the tray icon's **Open Manager** (or by launching the application again). On
  macOS the Dock icon is shown only while the window is open. Without a tray icon,
  closing the window quits.
- **Quit** (the window's button, the tray menu, or Ctrl/Cmd-Q) → stops the proxy, stops
  every managed service together with its child processes, and exits.
- **SIGTERM, SIGINT (Ctrl-C), or SIGHUP** (Ctrl-C, Ctrl-Break, or closing the console
  window on Windows) → the same as Quit.
- **A managed service exits on its own** → it is reported as `crashed` with its exit
  code. It is not restarted automatically.
- **Services are stopped** with SIGTERM to their process group (a console Ctrl-Break
  on Windows), then force-killed after five seconds.

## How to configure services

A new configuration must define the proxy listener and at least one service. For
example:

```toml
http_port = 2800
bind = ["127.0.0.1", "::1"]

[services.myservice]
command = ["myservice", "--port", "{MYSERVICE_PORT}"]
env = {MYSERVICE_PORT = "18200"}

[[services.myservice.routes]]
id = "myservice"
hosts = ["myservice.localhost"]
target_port_env = "MYSERVICE_PORT"
```

If the service has a hard-coded port that isn't configurable, use `target_port` instead:

```toml
[services.webapp]
command = ["webapp"]

[[services.webapp.routes]]
id = "webapp"
hosts = ["webapp.localhost"]
target_port = 3000
```

For an HTTP service listening on a Unix domain socket, use `target_socket_env` in the
same way as `target_port_env` (or `target_socket` for a fixed path):

```toml
[services.socketapp]
command = ["socketapp", "--bind", "unix:{SOCKETAPP_SOCKET}"]
env = {SOCKETAPP_SOCKET = "socketapp.sock"}

[[services.socketapp.routes]]
id = "socketapp"
hosts = ["socketapp.localhost"]
target_socket_env = "SOCKETAPP_SOCKET"
```

Set exactly one of `target_port`, `target_port_env`, `target_socket`, or
`target_socket_env` on each route. `target_host` only applies to TCP targets. Unix
socket paths must name a writable location and stay within the operating system's
socket-path length limit. Relative socket paths are resolved from the directory that
contains `services.toml`, which is also the working directory for managed services.

For services managed externally (not started by the proxy), omit `command` to create an
unmanaged proxy-only route:

```toml
[services.vite]

[[services.vite.routes]]
id = "vite"
hosts = ["vite.localhost"]
target_port = 5173
```

To turn a service off without deleting its config, add `disabled = true` to its
`[services.x]` table. A disabled service is not started or managed, and its routes are
excluded from the proxy and the portal — it is still listed, with a
`disabled` status. Remove the line (or set it to `false`) to re-enable it.

To keep a service managed but not launch it with the others, add `auto_start = false`
to its `[services.x]` table. It is skipped on startup, and its
routes stay registered with the proxy. It shows a `stopped` status until you start it
from the Services tab.
`auto_start` defaults to `true` and requires `command`, since a service without one is
not started by the manager at all.

The reference printed by `local-dev-proxy --sample-config` demonstrates
managed and externally managed processes, all TCP and Unix-socket target forms,
multiple and wildcard hosts, inherited environment values, route-free workers,
manual-start services, and disabled services. Copy and adapt only the relevant
sections.

## Troubleshooting

- **Service URL not proxying:** check the service's state on the Services tab (or in
  `logs/manager.log`), and confirm the port or Unix socket path is set in `services.toml`.
- **Proxy not responding:** check `logs/manager.log` inside the platform configuration
  directory for startup errors, run `local-dev-proxy --check-config`, then start the
  application again.
- **View service logs:** use the Logs tab, or read `logs/<service>.log` (for a live view,
  `tail -f logs/<service>.log`).
- **No window on a server or over SSH:** run with `--headless`.
- **Unix socket routes on Windows:** not supported; such routes answer `502`.

## Development

```sh
cargo fmt
cargo clippy --all-targets -- -D warnings
cargo test
```

`manager::Manager` owns the proxy and services and exposes every operation a UI needs
(per-service start/stop/restart, status, log tails, route listing, config
read/validate/save). `frontend::Frontend` is the trait a UI implements:
`desktop::Desktop` is the Slint manager window and tray (`ui/app.slint`), and
`frontend::Headless` runs without a UI. The desktop tests drive the real window on
Slint's headless testing backend, so they need no display.

### Running CI locally

`ci/` runs the steps of `.github/workflows/ci.yml` (fmt, clippy with `-D warnings`,
tests) against the working tree as it is, uncommitted changes included, on each of the
three platforms the workflow covers. `ci/unix/ci.sh` and `ci/windows/ci.ps1` are the
workflow's steps and run natively on the machine they are invoked on; `ci/unix/remote.sh`
and `ci/windows/remote.ps1` ship the tree to another machine over ssh and run them there.
The remote drivers are thin wrappers over the sibling
[`devtools`](https://github.com/andrewtheguy/devtools) repo (cloned next to this one, or
`DEVTOOLS_DIR`), parameterized by `.devtools.conf`.

```sh
ci/unix/ci.sh                        # this machine (Linux or macOS)
ci/unix/remote.sh -H macvm           # the macOS VM
pwsh -File ci/windows/remote.ps1     # the Windows CI VM
```

Both remote drivers also take `shell`, `doctor` (report the machine's toolchain, change
nothing) and `clean` (drop its cargo target cache). When a `ci.*` script and the workflow
disagree, the workflow is right and the script is stale.

### Desktop end-to-end test (Linux)

`ci/unix/e2e.sh` launches the real binary against a throwaway profile, on the headless
labwc session of the development host, and drives the manager window through its
keyboard shortcuts: select, stop, start and restart a service, open its log and the
routes, edit the configuration while it keeps running, save, validate, apply it unchanged
(nothing restarts) and changed (everything restarts), and quit. After each step it checks the proxy's answer over HTTP and the text on screen,
read from a screenshot with tesseract. `ci/unix/ci.sh` runs it on a Linux host where the
session is up and skips it elsewhere; `ci/unix/e2e.sh --available` says which. It needs
the labwc session's `DISPLAY` in the systemd user environment (or
`LOCAL_DEV_PROXY_E2E_DISPLAY`), plus xdotool, grim, ImageMagick, tesseract and busybox.
The numbered screenshots and the app's logs are left in `tmp/e2e/`.
