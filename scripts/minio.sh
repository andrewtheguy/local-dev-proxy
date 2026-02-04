#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source config.env
mkdir -p data

export MINIO_BROWSER_REDIRECT=off

exec minio server data --address ":$MINIO_PORT" --console-address ":$MINIO_CONSOLE_PORT"
