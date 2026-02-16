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

Everything lives in `services.toml`: caddy settings, port assignments (`[env]`), service commands, and routes.

## Usage

```sh
uv sync
uv run local-dev-proxy session up
```

This launches a zellij session with panes for caddy, minio, and s3browser. Each service syncs its routes to Caddy automatically on startup.

Re-running `session up` reattaches if the session is still active, or creates a new one if it exited.

## How to add a service

1. Add a section to `services.toml`:

```toml
[services.myservice]
command = ["myservice", "--port", "{MYSERVICE_PORT}"]

[[services.myservice.routes]]
id = "myservice"
hosts = ["myservice.localhost"]
target_port_env = "MYSERVICE_PORT"
```

2. Add the port to the `[env]` table in `services.toml`:

```toml
[env]
MYSERVICE_PORT = "18200"
```

3. Add a pane in `layouts/caddy.kdl`:

```kdl
pane name="myservice" command="uv" {
  args "run" "local-dev-proxy" "run" "myservice"
}
```

## Troubleshooting

- **Service URL not proxying:** confirm the service process is alive in its zellij pane and the port is set in `services.toml`.
- **Caddy not responding:** check the caddy pane for errors. It must be running before other services can sync routes.
