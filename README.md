This repo manages a detached Caddy reverse proxy plus zellij-managed local services for MinIO and s3browser.

## What you get

| URL | Service |
|-----|---------|
| `http://s3browser.localhost:2800` | s3browser UI |
| `http://minios3.localhost:2800` | MinIO S3 API |
| `http://minioconsole.localhost:2800` | MinIO Console |

## Prerequisites

- `brew`
- `uv`
- `zellij` (tested with 0.43.x)
- `caddy`
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

## Install and start

1. Stop upstream Caddy service to avoid conflicts:
```sh
brew services stop caddy
```

2. Install custom formula from this repo:
```sh
brew install --HEAD /Users/it3/codes/andrew/zellij-test/Formula/local-dev-proxy.rb
```

3. Start detached Caddy service:
```sh
brew services start local-dev-proxy
```

4. Sync Python environment:
```sh
uv sync
```

5. Start zellij session for services:
```sh
uv run local-dev-proxy session up
```

This opens two panes:
- `minio`
- `s3browser`

Caddy stays detached and keeps running when zellij exits.

## Route lifecycle

Routes are managed programmatically through Caddy Admin API (`127.0.0.1:2019`).

- Starting `minio` activates both `minio` and `minioconsole` routes.
- Starting `s3browser` activates `s3browser` route.
- Stopping a service deactivates its route(s).
- A fallback route always remains and returns `Not Found caddy` with `404`.

## CLI reference

```sh
# run service process in foreground
uv run local-dev-proxy service minio
uv run local-dev-proxy service s3browser
uv run local-dev-proxy service weed

# caddy route controls
uv run local-dev-proxy caddy activate minio
uv run local-dev-proxy caddy deactivate minio
uv run local-dev-proxy caddy sync
uv run local-dev-proxy caddy status

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
  - Confirm detached service is running: `brew services list | rg local-dev-proxy`
  - Check logs: `tail -n 100 ~/Library/Logs/Homebrew/local-dev-proxy-caddy.log`
- Service starts but URL does not proxy:
  - Confirm service process is alive in zellij pane.
  - Confirm corresponding `*_PORT` is set in `config.env`.
  - Run `uv run local-dev-proxy caddy sync` to reconcile state and routes.
