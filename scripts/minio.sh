#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data

exec minio server data --address :9000 --console-address :9001
