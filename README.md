Local dev proxy: built-in reverse proxy + process manager with a cross-platform manager window and a system-tray app.

> **No backward compatibility while on `v0.0.x`.** Any release may make breaking changes to
> the configuration format or behavior without a deprecation path. Pin to an exact version.

## Installation

Cross-platform (macOS, Windows, Linux) — the manager window and system-tray icon
are built with [PySide6 / Qt 6](https://doc.qt.io/qtforpython-6/).

### Install the native desktop application (recommended)

Download the matching installer from the
[Releases page](https://github.com/andrewtheguy/local-dev-proxy/releases):

- **macOS 13 or newer (Apple silicon/ARM64 only):** download the `.dmg`, open it, and copy
  **Local Dev Proxy.app** to **Applications**.
- **Windows:** download the `.msi` and run the installer. Launch **Local Dev Proxy**
  from the Start menu.

The native packages include Python, Qt, and the application's Python dependencies. You
do not need to install Python or use a terminal to launch the manager. Release builds
do not require paid signing certificates. macOS Gatekeeper or Windows SmartScreen may
therefore require you to explicitly approve the first launch.

### Install from a release wheel

The universal Python wheel remains available for Linux, development environments, and
users who prefer Python tooling. This method requires Python 3.13+ and `uv`.

Install directly from the wheel URL on the Releases page:

```shell
uv tool install https://github.com/andrewtheguy/local-dev-proxy/releases/download/vx.y.z/local_dev_proxy-x.y.z-py3-none-any.whl
```

Then run it with `local-dev-proxy`.

### Install the wheel from the GitHub Pages package index

Lets you pin by version instead of pasting a wheel URL:

```shell
uv tool install \
  --extra-index-url https://andrewtheguy.github.io/local-dev-proxy/simple/ \
  'local-dev-proxy==x.x.x'
```

### Install from a local checkout

Installs the current working tree as a global tool so `local-dev-proxy` is on your PATH
(works without a published release). This requires Python 3.13+ and `uv`:

```shell
uv tool install .
```

Re-run with `uv tool install --reinstall .` to pick up local changes.

### Install from source (git)
```shell
uv tool install git+https://github.com/andrewtheguy/local-dev-proxy.git@(tag or branch)
```

### Run without installing
```shell
uv tool run --from git+https://github.com/andrewtheguy/local-dev-proxy.git@vx.x.x local-dev-proxy

# Or from a local checkout
uv run local-dev-proxy
```

## What you get

| URL | Service |
|-----|---------|
| `http://s3browser.localhost:2800` | s3browser UI |
| `http://minios3.localhost:2800` | MinIO S3 API |
| `http://minioconsole.localhost:2800` | MinIO Console |
| `http://vite.localhost:2800` | Vite dev server (unmanaged) |
| `http://localhost:2800` | Portal (links to all services) |

## Prerequisites

- `uv`
- `minio`
- `s3browser` 0.1.9:

  ```shell
  uv tool install \
    https://github.com/andrewtheguy/s3browser/releases/download/v0.1.9/s3browser-0.1.9-py3-none-any.whl
  ```

## Configuration

Configuration lives in Qt's platform-standard per-user application configuration
directory:

| Platform | Default directory |
|----------|-------------------|
| macOS | `~/Library/Preferences/andrewtheguy/local-dev-proxy/` |
| Windows | `%APPDATA%\andrewtheguy\local-dev-proxy\` |
| Linux | `$XDG_CONFIG_HOME/andrewtheguy/local-dev-proxy/` (or `~/.config/...`) |

The application does not create or seed `services.toml`. On first run it opens the
configuration editor empty and remains stopped until you enter and save a valid
configuration. The bundled `src/local_dev_proxy/services.toml.sample` is reference
material only; it is never copied into the user profile and is not intended to run
unchanged.

`services.toml` holds proxy settings (`http_port`, `bind`), service commands,
environment values, ports, and routes. Route targets can be TCP ports or Unix domain
sockets. Logs are written in the `logs/` directory beside it.

For an isolated development or test profile, set
`LOCAL_DEV_PROXY_CONFIG_DIR=/path/to/profile`. The selected profile controls the
configuration, logs, cached icons, single-instance lock, and application activation
channel. This directory is the single profile root; every application path is derived
from it, so paths cannot be configured into inconsistent combinations and the profile
does not touch or contend with the normal profile.
`http_port` and `bind` are required, values are type-checked without coercion, and
unknown keys are rejected rather than silently treated as an older config shape.

Manager and service logs rotate at 10 MiB, retaining five numbered backups alongside
the active file (`.log.1` is newest). This bounds each log to about 60 MiB. Rotation
renames completed files rather than truncating a file while it is being written; once
the retention limit is reached, only the oldest backup is removed.

Edit it from the **Services** tab of the manager window (the config is only editable while
the services are stopped — press **View Config**, then **Stop All & Edit Config**), or by
hand.

## Usage

```sh
uv sync
uv run local-dev-proxy
```

`local-dev-proxy` is installed as a GUI entry point and runs the Qt application directly
in that process. There is no CLI layer, foreground flag, self-spawn, or launcher process.
When invoked from a terminal on macOS or Linux, the command remains attached until the
application quits; the GUI entry point prevents a console window on Windows.

The manager window owns the in-process reverse proxy and the managed service processes
and calls them directly — there is no background admin port. A system-tray icon appears
in the macOS menu bar, Windows notification area, or Linux tray, with **Open Manager**
and **Quit** actions. Launching the application again sends a local Qt activation request
to the existing instance, which restores and raises its manager window instead of
starting another copy. The activation endpoint and instance lock are scoped to the
selected configuration profile.

### Manager window

A window with three tabs:

- **Services** — a native Qt tree view showing status, PID, restart count and exit code
  for every service, with
  per-service Start / Stop / Restart buttons. This tab doubles as the config editor:
  press **View Config** to see `services.toml` (read-only, services still running), then
  **Stop All & Edit Config** to stop the proxy and services and swap to the editor
  (Validate / Save / Reload), then **Start All** to validate, save, and relaunch
  live (no app restart needed) — swapping back to the service list.
- **Logs** — view or follow (tail) any service's log.
- **Routes** — a hierarchical tree view with services as parents and their route URLs
  as children; click a URL row to open it in your browser.

### Lifecycle

- **Close the window** → it hides to the system-tray icon; the proxy and services
  keep running. Opening the tray menu does not also restore the window; choose
  **Open Manager** (or double-click the icon where supported) to reopen it.
- **Quit** (the in-window *Quit* button or the tray menu's *Quit*) → stops the
  proxy, stops all managed services, and exits the app.
- **No system tray available** → closing the manager window quits cleanly so the
  application cannot become invisible and unreachable.

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
excluded from the proxy and the portal — it still appears in the **Services** list with a
`disabled` status. Remove the line (or set it to `false`) to re-enable it.

The bundled reference at `src/local_dev_proxy/services.toml.sample` demonstrates
managed and externally managed processes, all TCP and Unix-socket target forms,
multiple and wildcard hosts, inherited environment values, route-free workers, and
disabled services. Copy and adapt only the relevant sections.

## Troubleshooting

- **Service URL not proxying:** open the manager window's **Services** tab to check the
  service state, and confirm the port or Unix socket path is set in `services.toml`.
- **Proxy not responding:** check `logs/manager.log` inside the platform configuration
  directory for startup errors. Quit from the window (or the tray menu), then run
  `uv run local-dev-proxy` again.
- **View service logs:** use the **Logs** tab (with *Follow* for a live tail).
