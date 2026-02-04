This repo contains a Zellij layout and Caddy config to run an `s3browser` web UI on a friendly local hostname.

What you get:
- `http://s3browser.localhost:2800` -> proxied to `http://localhost:8170`
- a Zellij session layout that starts both `caddy` and `s3browser`

## Prereqs
- `zellij` (layout tested with `zellij 0.43.x`)
- `caddy` v2
- `s3browser` available on your `PATH` and configured to listen on `localhost:8170`

Install Caddy on macOS:
```sh
brew install caddy
```

## Run (Zellij)
Start a new session using the layout:
```sh
zellij -s s3browser -n layouts/s3browser.kdl
```

This opens two panes:
- `caddy`: runs `caddy run --config Caddyfile --adapter caddyfile`
- `s3browser`: runs `s3browser` (edit the command in `layouts/s3browser.kdl` if needed)

Then open:
- `http://s3browser.localhost:2800`

## Notes / Troubleshooting
- Caddy is configured to listen on `:2800` by default (see `Caddyfile`). Change it if `2800` is already in use.
- Visiting `http://localhost:2800` (or any other host on port `2800`) will return a 404 by design; use `http://s3browser.localhost:2800`.
- `.localhost` domains resolve to `127.0.0.1` without editing `/etc/hosts`.
- This setup expects `s3browser` to listen on `localhost:8170`. If it uses a different port, update `Caddyfile`.
- Caddy is bound only to `127.0.0.1` and `::1` (dual‑stack localhost) via `bind` in the `Caddyfile`.
