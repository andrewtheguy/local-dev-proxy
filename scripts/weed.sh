#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source config.env

mkdir -p data/weed

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-weedadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-weedadmin}"

exec weed mini -dir=data/weed
