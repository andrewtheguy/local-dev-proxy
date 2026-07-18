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

Edit it from the **Config** tab of the manager window (the config is only editable while
the manager is stopped — the tab has a *Stop Manager* button), or by hand.

## Usage

```sh
uv sync
uv run local-dev-proxy
```

Running `uv run local-dev-proxy` with no arguments **starts the manager detached** (it
returns your terminal immediately) and opens the **manager window**. The manager runs an
in-process reverse proxy, manages service processes, and shows a macOS menu bar icon.
Run it again to reopen the manager window.

### Manager window

A Tkinter window with four tabs:

- **Services** — status, PID, restart count and exit code for every service, with
  Start / Stop / Restart buttons.
- **Logs** — view or follow (tail) any service's log.
- **Routes** — every service URL; double-click to open it in your browser.
- **Config** — edit `services.toml`, Validate, and Save. Editing is locked while the
  manager is running; use *Stop Manager* first. Changes apply on the next start.

### Lifecycle commands

```sh
uv run local-dev-proxy                 # Start detached + open the manager window
uv run local-dev-proxy gui             # Just open the manager window
uv run local-dev-proxy stop-manager    # Stop the running manager
uv run local-dev-proxy restart-manager # Restart the manager (detached)
uv run local-dev-proxy start-manager -f  # Run the manager in the foreground (blocking)
```

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

The bundled defaults live in `src/local_dev_proxy/services.toml.sample`.

## Troubleshooting

- **Service URL not proxying:** open the manager window's **Services** tab to check the
  service state, and confirm the port is set in `services.toml`.
- **Proxy not responding:** check `~/.config/local-dev-proxy/logs/manager.log` for
  startup errors. Restart with `uv run local-dev-proxy restart-manager`.
- **View service logs:** use the **Logs** tab (with *Follow* for a live tail).
