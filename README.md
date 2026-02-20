Local dev proxy: built-in reverse proxy + process manager with a macOS menu bar app.

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

Everything lives in `services.toml`: proxy settings (`http_port`, `bind`), service commands, env/ports, and routes.

## Usage

```sh
uv sync
uv run local-dev-proxy start-manager
```

This starts a macOS menu bar app that runs an in-process reverse proxy and manages service processes directly. Click a service name in the tray menu to open it in your browser.

### CLI commands

```sh
uv run local-dev-proxy routes             # List all service URLs
uv run local-dev-proxy status             # Show service status, PIDs, restart counts
uv run local-dev-proxy logs <name>        # Show last 100 lines of a service log
uv run local-dev-proxy logs <name> -f     # Follow (tail) a service log
uv run local-dev-proxy restart <name>     # Restart a managed service
uv run local-dev-proxy stop <name>        # Stop a managed service
uv run local-dev-proxy start <name>       # Start a stopped managed service
uv run local-dev-proxy sync              # Push routes to the running proxy
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

For services managed externally (not started by the proxy), omit `command` to create an unmanaged proxy-only route:

```toml
[services.vite]

[[services.vite.routes]]
id = "vite"
hosts = ["vite.localhost"]
target_port = 5173
```

## Troubleshooting

- **Service URL not proxying:** run `uv run local-dev-proxy status` to check the service state, and confirm the port is set in `services.toml`.
- **Proxy not responding:** check the tray app console output for errors. Restart with `uv run local-dev-proxy start-manager`.
- **View service logs:** run `uv run local-dev-proxy logs <name> -f` to tail the log output.
