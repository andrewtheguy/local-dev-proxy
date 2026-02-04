#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source config.env

mkdir -p data/weed

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-weedadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-weedadmin}"

exec weed mini -dir=data/weed \
  -ip=127.0.0.1 \
  -ip.bind=127.0.0.1 \
  -s3.port="$WEED_S3_PORT" \
  -master.port=39333 \
  -filer.port=38888 \
  -volume.port=39340 \
  -webdav=false \
  -admin.ui=false
