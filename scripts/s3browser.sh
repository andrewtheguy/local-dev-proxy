#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source config.env

exec s3browser -b "127.0.0.1:$S3BROWSER_PORT"
