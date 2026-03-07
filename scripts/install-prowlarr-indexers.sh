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

SOURCE_DIR="./apps/prowlarr/config"
TARGET_DIR="${APPDATA_DIR}/prowlarr/Definitions/Custom"

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Error: source directory not found at $SOURCE_DIR" >&2
  exit 1
fi

ymls=("$SOURCE_DIR"/*.yml)
if [[ ! -e "${ymls[0]}" ]]; then
  echo "Error: no .yml files found in $SOURCE_DIR" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"

for file in "${ymls[@]}"; do
  name="$(basename "$file")"
  cp "$file" "$TARGET_DIR/$name"
  echo "Installed $name"
done

echo ""
echo "Custom indexers installed to $TARGET_DIR"
echo "Restart prowlarr for changes to take effect: docker compose -f apps/prowlarr/docker-compose.yml restart prowlarr"
