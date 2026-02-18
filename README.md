Local dev proxy: Caddy reverse proxy + zellij-managed services for MinIO and s3browser.

## What you get

| URL | Service |
|-----|---------|
| `http://s3browser.localhost:2800` | s3browser UI |
| `http://minios3.localhost:2800` | MinIO S3 API |
| `http://minioconsole.localhost:2800` | MinIO Console |

## Prerequisites

- `uv`
- `caddy`
- `zellij`
- `minio`
- `s3browser`

## Configuration

Everything lives in `services.toml`: caddy settings, service commands, env/ports, and routes.

## Usage

```sh
uv sync
uv run local-dev-proxy tray
```

This starts a macOS menu bar app that runs Caddy as a subprocess and launches minio/s3browser in a headless zellij session. Click a service name in the tray menu to open it in your browser.

To view service logs, attach to the zellij session manually:

```sh
zellij attach local-dev-proxy
```

To list service URLs:

```sh
uv run local-dev-proxy routes
```

## How to add a service

1. Add a section to `services.toml` (command, port, and route together):

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

2. Add a tab in `layouts/caddy.kdl`:

```kdl
tab name="myservice" {
  pane name="myservice" command="uv" {
    args "run" "local-dev-proxy" "run" "myservice"
  }
}
```

## Troubleshooting

- **Service URL not proxying:** attach to the zellij session (`zellij attach local-dev-proxy`) to check the service tab, and confirm the port is set in `services.toml`.
- **Caddy not responding:** check the tray app — if Caddy crashes, you'll get a macOS notification. Restart with `uv run local-dev-proxy tray`.
- **Session attached elsewhere:** to disconnect other clients, press `Ctrl+O` then `W` to open the session manager, then `Ctrl+X` to detach them.
