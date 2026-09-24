#!/usr/bin/env bash
# The steps from .github/workflows/ci.yml, in the same order, with the same
# flags: fmt, clippy with -D warnings, then the tests. When this file and that
# workflow disagree, the workflow is right and this is stale — it exists to say
# what CI will say before CI is asked.
#
# This runs natively on whatever Unix machine it is invoked on: a CI box (see
# ci/unix/remote.sh) or a dev machine. It installs nothing and changes no
# machine state, so running it locally is safe — the build dependency the
# workflow apt-gets on Linux (libfontconfig1-dev, for Slint's text stack) is
# assumed present; `remote.sh doctor` checks for it. The desktop tests drive
# the window on Slint's headless testing backend, so no display is needed.
#
# One step beyond ci.yml: on a Linux host with the headless labwc session
# up, ci/unix/e2e.sh drives the real window through its keyboard shortcuts.
# GitHub's runners have no compositor, so the workflow never runs it; a
# machine without the session skips it, loudly.
#
# Not covered here, on purpose: the release-profile builds and the .dmg
# packaging, which belong to release.yml.
set -euo pipefail

# Invoked over ssh the working directory is the login user's home, not the
# checkout, so anchor to the repo root this script sits in. A non-interactive
# ssh shell also skips the profile that puts rustup's bin dir on PATH.
cd "$(dirname "$0")/../.."
export PATH="$HOME/.cargo/bin:$PATH"

step() {
    local name=$1; shift
    echo ''
    echo "== $name =="
    echo "   cargo $*"
    cargo "$@"
}

echo '== toolchain =='
uname -sm
rustc --version
cargo --version
cargo clippy --version
cargo fmt --version
[ -n "${CARGO_TARGET_DIR:-}" ] && echo "   CARGO_TARGET_DIR=$CARGO_TARGET_DIR"

step 'Check formatting' fmt --check
step 'Clippy' clippy --locked --all-targets -- -D warnings
step 'Test' test --locked

echo ''
echo '== Desktop e2e (Linux, local only) =='
if [ "$(uname -s)" != Linux ]; then
    echo '   SKIPPED: Linux only'
elif reason=$(./ci/unix/e2e.sh --available); then
    echo '   ./ci/unix/e2e.sh'
    ./ci/unix/e2e.sh
else
    echo "   SKIPPED: $reason"
fi

echo ''
echo 'all steps passed'
