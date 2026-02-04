This repo contains a Zellij layout and Caddy config to run MinIO and s3browser on friendly local hostnames.

## What you get

| URL | Service |
|-----|---------|
| `http://s3browser.localhost:2800` | s3browser UI |
| `http://minios3.localhost:2800` | MinIO S3 API |
| `http://minioconsole.localhost:2800` | MinIO Console |

## Prereqs

- `zellij` (tested with 0.43.x)
- `caddy` v2
- `minio` server
- `s3browser` configured to connect to MinIO

Install on macOS:
```sh
brew install caddy
go install github.com/minio/minio@latest
```

## Configuration

Ports are configured in `config.env`:
```
MINIO_PORT=19000
MINIO_CONSOLE_PORT=19001
S3BROWSER_PORT=18170
```

## Run (Zellij)

```sh
zellij -s s3browser -n layouts/s3browser.kdl
```

This opens three panes:
- `caddy`: reverse proxy
- `minio`: S3-compatible object storage
- `s3browser`: web UI

## Notes / Troubleshooting

- Caddy listens on port `2800` (see `Caddyfile`). Change `http_port` if already in use.
- `.localhost` domains resolve to `127.0.0.1` without `/etc/hosts` changes.
- MinIO default credentials: `minioadmin` / `minioadmin`
- Data is stored in `data/` (gitignored).

## Adding more services

1. Add port to `config.env`:
```
MYSERVICE_PORT=18000
```

2. Add site block to `Caddyfile`:
```caddy
http://myservice.localhost {
	bind 127.0.0.1 ::1
	reverse_proxy localhost:{$MYSERVICE_PORT}
}
```
