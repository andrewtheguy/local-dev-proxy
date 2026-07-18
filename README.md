Local dev proxy: built-in reverse proxy + process manager with a macOS menu bar app and a Tkinter manager UI.

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
- `s3browser`

## Configuration

Configuration lives in a per-user file:

```
~/.config/local-dev-proxy/services.toml
```

(honors `$XDG_CONFIG_HOME`). On first run it is created automatically from the bundled
sample. It holds proxy settings (`http_port`, `bind`), service commands, env/ports, and
routes. Logs are written next to it under `~/.config/local-dev-proxy/logs/`.

Edit it from the **Services** tab of the manager window (the config is only editable while
the services are stopped — press **View Config**, then **Stop All & Edit Config**), or by
hand.

## Usage

```sh
uv sync
uv run local-dev-proxy
```

`uv run local-dev-proxy` **starts the app detached** (it returns your terminal
immediately) and opens the **manager window**. It is a single process: the Tkinter
window owns the in-process reverse proxy and the service processes and calls them
directly — there is no background admin port or IPC. A macOS menu-bar icon appears; click
it to bring the window back. Running the command again just raises the existing window.

### Manager window

A Tkinter window with three tabs:

- **Services** — status, PID, restart count and exit code for every service, with
  per-service Start / Stop / Restart buttons. This tab doubles as the config editor:
  press **View Config** to see `services.toml` (read-only, services still running), then
  **Stop All & Edit Config** to stop the proxy and services and swap to the editor
  (Validate / Save / Reload), then **Start All** to validate, save, and relaunch
  live (no app restart needed) — swapping back to the service list.
- **Logs** — view or follow (tail) any service's log.
- **Routes** — every service URL; double-click to open it in your browser.

### Lifecycle

- **Close the window** → it hides to the menu-bar icon; the proxy and services keep
  running. Click the icon (or re-run `uv run local-dev-proxy`) to reopen it.
- **Quit** (the in-window *Quit* button or ⌘Q) → stops the proxy, stops all managed
  services, and exits the app.

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
  service state, and confirm the port is set in `services.toml`.
- **Proxy not responding:** check `~/.config/local-dev-proxy/logs/manager.log` for
  startup errors. Quit from the window (or ⌘Q), then run `uv run local-dev-proxy` again.
- **View service logs:** use the **Logs** tab (with *Follow* for a live tail).
