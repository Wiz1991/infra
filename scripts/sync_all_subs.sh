#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f ".env" ]]; then
  echo "Error: .env file not found in current directory" >&2
  echo "Run this script from the project root." >&2
  exit 1
fi

set -a
source .env
set +a

docker run -it --rm \
  -v "${APPDATA_DIR}/bazarr-sync/config.yaml:/usr/src/app/config.yaml" \
  --network traefik \
  ghcr.io/ajmandourah/bazarr-sync:latest \
  bazarr-sync sync shows
