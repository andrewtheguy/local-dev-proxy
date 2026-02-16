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
uv run local-dev-proxy session up
```

This launches a zellij session with panes for caddy, minio, and s3browser. Each service syncs its routes to Caddy automatically on startup.

Re-running `session up` reattaches if the session is still active, or creates a new one if it exited.

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

2. Add a pane in `layouts/caddy.kdl`:

```kdl
pane name="myservice" command="uv" {
  args "run" "local-dev-proxy" "run" "myservice"
}
```

## Troubleshooting

- **Service URL not proxying:** confirm the service process is alive in its zellij pane and the port is set in `services.toml`.
- **Caddy not responding:** check the caddy pane for errors. It must be running before other services can sync routes.
- **Session attached elsewhere:** to disconnect other clients, press `Ctrl+O` then `W` to open the session manager, then `Ctrl+X` to detach them.
