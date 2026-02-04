#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data

export MINIO_BROWSER_REDIRECT=off

exec minio server data --address :9000 --console-address :9001
