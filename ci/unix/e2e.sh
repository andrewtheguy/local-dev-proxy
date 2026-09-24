#!/usr/bin/env bash
# End-to-end test of the desktop app on this Linux host: the real binary, a
# real window on the headless labwc session, driven by keyboard shortcuts.
#
#   ci/unix/e2e.sh              # build, run the scenario, leave tmp/e2e/ behind
#   ci/unix/e2e.sh --available  # exit 0 if this machine can run it, else say why
#
# The scenario launches the manager against a throwaway profile whose one
# service is a busybox httpd, then walks every keyboard shortcut: select the
# service, stop, start and restart it, open its log and the routes, edit the
# configuration while it keeps running, cancel, edit again, save, validate,
# apply it unchanged (nothing restarts), apply a change (everything
# restarts), and quit. After each step it checks what a user would check: the
# proxy's answer over HTTP, and the text on screen (a screenshot of the
# session, read back with tesseract). tmp/e2e/ keeps the numbered screenshots
# and the app's logs, for looking at after a failure.
#
# The app is launched without WAYLAND_DISPLAY so winit connects to X11 (Slint
# does that under WSL regardless) and the window is an Xwayland window:
# xdotool then drives it through the session's Xwayland, the one input path
# that reaches it (wlroots does not forward wtype's virtual keyboard to X
# clients). Screenshots are taken from the Wayland side with grim.
#
# Needs: the labwc session (its DISPLAY comes from the systemd user
# environment the session's autostart imports; LOCAL_DEV_PROXY_E2E_DISPLAY
# overrides), xdotool, grim, ImageMagick, tesseract, busybox, curl, ss.
set -euo pipefail

cd "$(dirname "$0")/../.."
export PATH="$HOME/.cargo/bin:$PATH"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
display=${LOCAL_DEV_PROXY_E2E_DISPLAY:-$(systemctl --user show-environment 2>/dev/null | sed -n 's/^DISPLAY=//p')}

unavailable() {
    if [ "$(uname -s)" != Linux ]; then echo 'not Linux'; return; fi
    if [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then echo "no Wayland socket at $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"; return; fi
    if [ -z "$display" ]; then echo 'no DISPLAY in the systemd user environment (is the labwc session up?)'; return; fi
    local tool
    for tool in xdotool grim magick tesseract busybox curl ss; do
        command -v "$tool" >/dev/null || { echo "no $tool"; return; }
    done
}

if [ "${1:-}" = --available ]; then
    reason=$(unavailable)
    [ -z "$reason" ] || { echo "$reason"; exit 1; }
    exit 0
fi
reason=$(unavailable)
[ -z "$reason" ] || { echo "cannot run the e2e here: $reason" >&2; exit 1; }
export DISPLAY=$display

out=tmp/e2e
rm -rf "$out"
mkdir -p "$out"
shot=0
app=
profile=

info() { echo "[e2e] $*"; }
fail() {
    echo "[e2e] FAILED: $*" >&2
    if [ -n "$app" ]; then
        echo '--- app.log' >&2
        tail -30 "$out/app.log" >&2 || true
    fi
    exit 1
}
cleanup() {
    if [ -n "$app" ] && kill -0 "$app" 2>/dev/null; then
        kill "$app" 2>/dev/null || true
        wait "$app" 2>/dev/null || true
    fi
    [ -z "$profile" ] || rm -rf "$profile"
}
trap cleanup EXIT

free_port() {
    local port
    while :; do
        port=$((20000 + RANDOM % 20000))
        ss -ltn 2>/dev/null | grep -q ":$port " || { echo "$port"; return; }
    done
}

# Screenshot the session and OCR it; the text goes to stdout. The UI text is
# small, so it is read from a 2x upscale, in two page-segmentation modes:
# sparse text (11) finds isolated words such as the status line, which the
# default block mode (3) skips, and vice versa for the log view's lines.
screen_text() {
    grim "$1"
    magick "$1" -resize 200% "$out/ocr.png"
    tesseract "$out/ocr.png" - --psm 11 2>/dev/null
    tesseract "$out/ocr.png" - --psm 3 2>/dev/null
}

# expect_screen NAME REGEX — retry screenshots for up to 15s until the OCR
# text matches; the last screenshot is kept as tmp/e2e/NN-NAME.png either way.
# The match runs over the text with all whitespace collapsed to single
# spaces: sparse mode reads a phrase as one word per line.
expect_screen() {
    local name=$1 pattern=$2 file text
    shot=$((shot + 1))
    file=$(printf '%s/%02d-%s.png' "$out" "$shot" "$name")
    local deadline=$((SECONDS + 15))
    while :; do
        text=$(screen_text "$file")
        if tr -s '[:space:]' ' ' <<<"$text" | grep -Eq "$pattern"; then
            info "screen: $name ($pattern)"
            return
        fi
        if [ $SECONDS -ge $deadline ]; then
            echo "--- OCR of $file:" >&2
            grep -v '^\s*$' <<<"$text" >&2
            fail "screen never showed $pattern (see $file)"
        fi
        sleep 0.5
    done
}

# expect_http NAME URL_HOST STATUS — retry for up to 15s until the proxy
# answers with STATUS for that Host; STATUS 000 means connection refused.
expect_http() {
    local name=$1 host=$2 want=$3 got
    local deadline=$((SECONDS + 15))
    while :; do
        got=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$http_port/" || true)
        if [ "$got" = "$want" ]; then
            info "http: $name -> $want"
            return
        fi
        [ $SECONDS -lt $deadline ] || fail "$name: proxy answered $got for $host, wanted $want"
        sleep 0.5
    done
}

key() {
    info "key: $1"
    xdotool key --clearmodifiers "$1"
}

info 'building'
cargo build --quiet
binary="${CARGO_TARGET_DIR:-target}/debug/local-dev-proxy"

profile=$(mktemp -d "${TMPDIR:-/tmp}/local-dev-proxy-e2e.XXXXXX")
http_port=$(free_port)
web_port=$(free_port)
mkdir -p "$profile/www"
echo '<h1>e2e web ok</h1>' > "$profile/www/index.html"
cat > "$profile/services.toml" <<TOML
http_port = $http_port
bind = ["127.0.0.1"]

[services.web]
command = ["sh", "-c", "echo web starting on \$WEB_PORT; exec busybox httpd -f -vv -p 127.0.0.1:\$WEB_PORT -h $profile/www"]
env = {WEB_PORT = "$web_port"}

[[services.web.routes]]
id = "web"
hosts = ["web.localhost"]
target_port_env = "WEB_PORT"
TOML

info "launching on DISPLAY=$DISPLAY (profile $profile, proxy :$http_port, web :$web_port)"
env -u WAYLAND_DISPLAY LOCAL_DEV_PROXY_CONFIG_DIR="$profile" "$binary" >"$out/app.log" 2>&1 &
app=$!

# The window, focused, with the service auto-started.
expect_http 'auto-start' web.localhost 200
[ "$(curl -s -H 'Host: web.localhost' "http://127.0.0.1:$http_port/")" = '<h1>e2e web ok</h1>' ] || fail 'the proxied page is not the fixture'
window=
for _ in $(seq 1 40); do
    window=$(xdotool search --name '^Local Dev Proxy' 2>/dev/null | head -1) && [ -n "$window" ] && break
    sleep 0.25
done
[ -n "$window" ] || fail 'no manager window appeared'
xdotool windowactivate --sync "$window"
expect_screen services 'web +running'

# A second launch reaches the running instance instead of starting another.
second=$(LOCAL_DEV_PROXY_CONFIG_DIR="$profile" "$binary" 2>&1 || true)
grep -q 'asked it to show itself' <<<"$second" || fail "second launch said: $second"
info 'second launch was redirected to the running instance'

# Service controls: select, stop, start, restart.
key ctrl+Down
expect_screen selected 'Selected service: web \(running\)'
key ctrl+shift+x
expect_screen stopped 'web stopped'
expect_http 'stopped service' web.localhost 502
key ctrl+shift+s
expect_screen started 'web started'
expect_http 'started service' web.localhost 200
key ctrl+shift+r
expect_screen restarted 'web restarted'
expect_http 'restarted service' web.localhost 200
grep -c 'web starting on' "$profile/logs/web.log" | grep -qx 3 || fail 'the service log does not show three starts'

# The other tabs.
key ctrl+l
expect_screen logs "starting on $web_port"
key ctrl+3
# The target column; tesseract skips the blue URL text.
expect_screen routes "localhost:$web_port"
key ctrl+1

# Edit while everything keeps running; applying it unchanged restarts nothing.
key ctrl+e
expect_screen editing 'services keep running'
expect_http 'proxy up while editing' web.localhost 200
key Escape
expect_screen cancelled 'Edit Config'
key ctrl+e
expect_screen editing-again 'services keep running'
key ctrl+s
expect_screen saved '\bsaved\b'
key ctrl+k
# Word-bounded: the banner says "validates".
expect_screen valid '\bvalid\b'
key ctrl+Return
expect_screen unchanged 'no changes'
expect_http 'still up after an unchanged apply' web.localhost 200
grep -c 'web starting on' "$profile/logs/web.log" | grep -qx 3 || fail 'an unchanged apply restarted the service'

# A changed configuration (written to disk; the editor loads it) restarts
# everything with it.
new_web_port=$(free_port)
sed -i "s/WEB_PORT = \"$web_port\"/WEB_PORT = \"$new_web_port\"/" "$profile/services.toml"
key ctrl+e
expect_screen edited "$new_web_port"
key ctrl+Return
expect_screen restarted-all 'saved.{1,3}restarted'
expect_http 'restarted with the change' web.localhost 200
grep -q "web starting on $new_web_port" "$profile/logs/web.log" || fail 'the service did not restart on the new port'
web_port=$new_web_port

# Quit takes the service down with it.
key ctrl+q
for _ in $(seq 1 40); do
    kill -0 "$app" 2>/dev/null || break
    sleep 0.25
done
if kill -0 "$app" 2>/dev/null; then fail 'the app did not quit on Ctrl+Q'; fi
wait "$app" && code=0 || code=$?
app=
[ "$code" -eq 0 ] || fail "the app exited with $code"
curl -s "http://127.0.0.1:$web_port/" >/dev/null && fail 'the service outlived the app'
grep -q 'Stopped web' "$profile/logs/manager.log" || fail 'manager.log does not record stopping web'
cp "$profile/logs/manager.log" "$profile/logs/web.log" "$out/"

info "passed; screenshots and logs in $out/"
