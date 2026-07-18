Local dev proxy: built-in reverse proxy + process manager with a cross-platform manager window and a system-tray app.

> **No backward compatibility while on `v0.0.x`.** Any release may make breaking changes to
> the config format, CLI, or behavior without a deprecation path. Pin to an exact version.

## Installation

Cross-platform (macOS, Windows, Linux) — the manager window and system-tray icon
are built with [PySide6 / Qt 6](https://doc.qt.io/qtforpython-6/). Requires Python 3.13+.

### Install from a release wheel (recommended)

Install directly from a release asset URL — no checkout, no index config. Grab the wheel
URL from the [Releases page](https://github.com/andrewtheguy/local-dev-proxy/releases):

```shell
uv tool install https://github.com/andrewtheguy/local-dev-proxy/releases/download/vx.y.z/local_dev_proxy-x.y.z-py3-none-any.whl
```

Then run it with `local-dev-proxy`.

### Install from the GitHub Pages package index

Lets you pin by version instead of pasting a wheel URL:

```shell
uv tool install \
  --extra-index-url https://andrewtheguy.github.io/local-dev-proxy/simple/ \
  'local-dev-proxy==x.x.x'
```

### Install from a local checkout

Installs the current working tree as a global tool so `local-dev-proxy` is on your PATH
(works without a published release):

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
uv tool run --from git+https://github.com/andrewtheguy/local-dev-proxy.git@vx.x.x local-dev-proxy [command] [options]

# Or from a local checkout
uv run local-dev-proxy [command] [options]
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

Configuration lives in a per-user file:

```
~/.config/local-dev-proxy/services.toml
```

(honors `$XDG_CONFIG_HOME`). On first run it is created automatically from the bundled
sample. It holds proxy settings (`http_port`, `bind`), service commands, env/ports, and
routes. Route targets can be TCP ports or Unix domain sockets. Logs are written next to
it under `~/.config/local-dev-proxy/logs/`.
`http_port` and `bind` are required, values are type-checked without coercion, and
unknown keys are rejected rather than silently treated as an older config shape.

Edit it from the **Services** tab of the manager window (the config is only editable while
the services are stopped — press **View Config**, then **Stop All & Edit Config**), or by
hand.

## Usage

```sh
uv sync
uv run local-dev-proxy
```

`uv run local-dev-proxy` **starts the app detached** (it returns your terminal
immediately) and opens the **manager window**. It is a single process: the manager
window owns the in-process reverse proxy and the service processes and calls them
directly — there is no background admin port or application-control IPC. A system-tray
icon appears (in the macOS menu bar / Windows notification area / Linux tray); its menu
has **Open Manager** and **Quit**. Running the command again reports that it is already
running; use **Open Manager** from the tray to restore a hidden window. An advisory OS
file lock prevents multiple app instances; it is not used as a command or data channel.

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
  keep running. Choose **Open Manager** from the tray menu to reopen it.
- **Quit** (the in-window *Quit* button or the tray menu's *Quit*) → stops the
  proxy, stops all managed services, and exits the app.

`uv run local-dev-proxy --foreground` runs the app in the foreground (blocking) instead of
detaching — this is what the detached launcher and a packaged build use internally.

## How to add a service

Add a section to `services.toml` (command, port, and route together):

```toml
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

The bundled defaults live in `src/local_dev_proxy/services.toml.sample`.

## Troubleshooting

- **Service URL not proxying:** open the manager window's **Services** tab to check the
  service state, and confirm the port or Unix socket path is set in `services.toml`.
- **Proxy not responding:** check `~/.config/local-dev-proxy/logs/manager.log` for
  startup errors. Quit from the window (or the tray menu), then run `uv run local-dev-proxy` again.
- **View service logs:** use the **Logs** tab (with *Follow* for a live tail).
