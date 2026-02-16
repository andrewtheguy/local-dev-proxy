This repo manages a detached Caddy reverse proxy plus zellij-managed local services for MinIO and s3browser.

## What you get

| URL | Service |
|-----|---------|
| `http://s3browser.localhost:2800` | s3browser UI |
| `http://minios3.localhost:2800` | MinIO S3 API |
| `http://minioconsole.localhost:2800` | MinIO Console |

## Prerequisites

- `uv`
- `caddy`
- `zellij` (tested with 0.43.x)
- `minio`
- `s3browser`

Install on macOS:
```sh
brew install caddy uv zellij
# MinIO and s3browser are expected on PATH
```

## Configuration

Service ports live in `/Users/it3/codes/andrew/zellij-test/config.env`:
```env
MINIO_PORT=19000
MINIO_CONSOLE_PORT=19001
S3BROWSER_PORT=18170
WEED_S3_PORT=18333
```

Routing config is centralized in `/Users/it3/codes/andrew/zellij-test/routes.toml`.

## Start and stop

1. Install Python dependencies:
```sh
uv sync
```

2. Start zellij session:
```sh
uv run local-dev-proxy session up
```

This launches three panes:
- `caddy` (`./scripts/caddy.py`)
- `minio` (`./scripts/minio.py`)
- `s3browser` (`./scripts/s3browser.py`)

Each service pane starts the process and syncs Caddy routes automatically.

4. To stop Caddy:
- If running in zellij pane, close the pane/session.
- If running detached via `caddy start`, run:
```sh
uv run local-dev-proxy caddy stop
```

## Route lifecycle

Routes are managed programmatically through Caddy Admin API (`127.0.0.1:2020`).

- Starting `minio` activates both `minio` and `minioconsole` routes.
- Starting `s3browser` activates `s3browser` route.
- Stopping a service deactivates its route(s).
- A fallback route always remains and returns `Not Found caddy` with `404`.

## CLI reference

```sh
# detached caddy lifecycle
uv run local-dev-proxy caddy start
uv run local-dev-proxy caddy stop
uv run local-dev-proxy caddy restart
uv run local-dev-proxy caddy status

# run service process in foreground
uv run local-dev-proxy service minio
uv run local-dev-proxy service s3browser
uv run local-dev-proxy service weed

# caddy route controls
uv run local-dev-proxy caddy sync

# zellij launcher
uv run local-dev-proxy session up
```

## Add or change routes

Edit `/Users/it3/codes/andrew/zellij-test/routes.toml`:

```toml
[services.myservice]
hosts = ["myservice.localhost"]
target_port_env = "MYSERVICE_PORT"
# optional
# target_host = "127.0.0.1"
# enabled = true
```

Then set `MYSERVICE_PORT` in `/Users/it3/codes/andrew/zellij-test/config.env` and call:

```sh
uv run local-dev-proxy caddy sync
```

## Troubleshooting

- `uv run local-dev-proxy caddy status` fails with admin API error:
  - Ensure Caddy is running: `uv run local-dev-proxy caddy start`
  - Restart cleanly: `uv run local-dev-proxy caddy restart`
- Service starts but URL does not proxy:
  - Confirm service process is alive in zellij pane.
  - Confirm corresponding `*_PORT` is set in `config.env`.
  - Run `uv run local-dev-proxy caddy sync` to reconcile state and routes.
