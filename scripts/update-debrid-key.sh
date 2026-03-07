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

EXAMPLE_FILE="./apps/decypharr/config/config.example.json"
CONFIG_FILE="${APPDATA_DIR}/decypharr/config.json"
COMMAND="${1:-}"

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed" >&2
  exit 1
fi

if [[ ! -f "$EXAMPLE_FILE" ]]; then
  echo "Error: template not found at $EXAMPLE_FILE" >&2
  exit 1
fi

prompt_keys() {
  read -rp "Enter RealDebrid API key: " API_KEY
  if [[ -z "$API_KEY" ]]; then
    echo "Error: API key cannot be empty" >&2
    exit 1
  fi
  read -rp "Enter RealDebrid download API key (blank = same as above): " DOWNLOAD_KEY
  DOWNLOAD_KEY="${DOWNLOAD_KEY:-$API_KEY}"
}

extract_keys() {
  API_KEY="$(jq -r '.debrids[0].api_key' "$CONFIG_FILE")"
  DOWNLOAD_KEY="$(jq -r '.debrids[0].download_api_keys[0]' "$CONFIG_FILE")"

  if [[ -z "$API_KEY" || "$API_KEY" == "null" || "$API_KEY" == '${REALDEBRID_API_KEY}' ]]; then
    echo "Error: no valid keys found in existing config" >&2
    echo "Run without arguments to set keys for the first time." >&2
    exit 1
  fi
}

write_config() {
  local tmp
  tmp="$(mktemp)"
  jq \
    --arg api_key "$API_KEY" \
    --arg dl_key "$DOWNLOAD_KEY" \
    '.debrids[0].api_key = $api_key | .debrids[0].download_api_keys = [$dl_key]' \
    "$EXAMPLE_FILE" > "$tmp" && mv "$tmp" "$CONFIG_FILE"
}

case "$COMMAND" in
  update)
    if [[ ! -f "$CONFIG_FILE" ]]; then
      echo "Error: no existing config.json to pull keys from" >&2
      echo "Run without arguments first to initialize." >&2
      exit 1
    fi
    extract_keys
    write_config
    echo "Config rebuilt from template, existing keys preserved."
    ;;
  "")
    if [[ -f "$CONFIG_FILE" ]]; then
      read -rp "config.json already exists. Overwrite? [y/N] " confirm
      [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
    fi
    prompt_keys
    write_config
    echo "Config initialized with your keys."
    ;;
  *)
    echo "Usage: $(basename "$0") [update]"
    echo ""
    echo "  (no args)  Initialize config.json from template, prompting for keys"
    echo "  update     Rebuild config.json from template, preserving existing keys"
    exit 1
    ;;
esac

echo "Restart decypharr for changes to take effect: docker compose restart decypharr"
