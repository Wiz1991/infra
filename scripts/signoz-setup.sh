#!/usr/bin/env bash
# signoz-setup.sh — apply retention, alert rules and dashboards to a running SigNoz.
#
# Run once after the stack is up and you've created the first (admin) user in the
# SigNoz UI. Idempotent: re-running updates the TTLs, skips alert rules that
# already exist by name, and updates dashboards in place.
#
#   SIGNOZ_EMAIL=you@example.com SIGNOZ_PASSWORD=... ./scripts/signoz-setup.sh
#
# Talks to SigNoz over the docker network, NOT via localhost: SigNoz's port 8080
# is not published on the host — Traefik owns host :8080 — so hitting
# localhost:8080 lands on Traefik's API and 404s every call. All curls therefore
# run inside a throwaway container on the traefik network.
#
# Notification channels are NOT created here — add one in Settings → Alert
# Channels first (Discord accepts Slack-format webhooks: use the Slack channel
# type with /slack appended to the webhook URL), otherwise rules fire nowhere.
set -euo pipefail

SIGNOZ_URL="${SIGNOZ_URL:-http://signoz-signoz-0:8080}"
SIGNOZ_NETWORK="${SIGNOZ_NETWORK:-traefik}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:latest}"

# Auth: an API key (Settings → API Keys) is simplest. Make sure the key's
# service account actually has a role — SigNoz will happily create a roleless key
# that 403s on everything.
SIGNOZ_API_KEY="${SIGNOZ_API_KEY:-}"
SIGNOZ_EMAIL="${SIGNOZ_EMAIL:-}"
SIGNOZ_PASSWORD="${SIGNOZ_PASSWORD:-}"

# Where alerts are delivered. SigNoz refuses to create ANY rule unless at least
# one notification channel exists — a rule builds an alertmanager route, and a
# route with no channel is invalid. Point this at a Discord webhook (append
# /slack to it and use the slack type), an ntfy endpoint, or anything that
# accepts a POST.
SIGNOZ_WEBHOOK_URL="${SIGNOZ_WEBHOOK_URL:-}"
SIGNOZ_CHANNEL="${SIGNOZ_CHANNEL:-default}"

# Retention. Logs dominate disk use; metrics are cheap. Raise these only after
# watching real growth — this host does not have much headroom.
LOGS_TTL_HOURS="${LOGS_TTL_HOURS:-168}"       # 7d
TRACES_TTL_HOURS="${TRACES_TTL_HOURS:-168}"   # 7d
METRICS_TTL_HOURS="${METRICS_TTL_HOURS:-720}" # 30d

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

# curl, from inside the docker network. Request body (if any) comes in on stdin.
sig() {
  docker run --rm -i --network "$SIGNOZ_NETWORK" "$CURL_IMAGE" -sS "$@"
}

if [ -n "$SIGNOZ_API_KEY" ]; then
  AUTH=(-H "SIGNOZ-API-KEY: $SIGNOZ_API_KEY")
  echo "==> using API key"
else
  [ -n "$SIGNOZ_EMAIL" ] && [ -n "$SIGNOZ_PASSWORD" ] || {
    echo "set SIGNOZ_API_KEY, or SIGNOZ_EMAIL + SIGNOZ_PASSWORD" >&2; exit 1; }

  # Auth is a two-step dance. /api/v1/login no longer exists (SigNoz replaced it
  # with /api/v2/sessions), and the session endpoint refuses to authenticate
  # without an orgId — which you get from the unauthenticated session-context
  # lookup. Note that an unknown path returns the frontend's index.html with a
  # 200, so a wrong URL looks like success until you try to parse it: hence -f
  # and the explicit token check.
  echo "==> logging in to $SIGNOZ_URL"
  ORG_ID=$(sig -f "$SIGNOZ_URL/api/v2/sessions/context?email=$(jq -rn --arg e "$SIGNOZ_EMAIL" '$e|@uri')" \
    | jq -r '.data.orgs[0].id // empty')
  [ -n "$ORG_ID" ] || { echo "could not resolve the SigNoz org — is the stack up?" >&2; exit 1; }

  LOGIN=$(jq -nc --arg e "$SIGNOZ_EMAIL" --arg p "$SIGNOZ_PASSWORD" --arg o "$ORG_ID" \
            '{email:$e,password:$p,orgId:$o}' \
    | sig -X POST -H 'Content-Type: application/json' --data-binary @- \
          "$SIGNOZ_URL/api/v2/sessions/email_password" 2>/dev/null)

  TOKEN=$(jq -r '.data.accessToken // .accessToken // empty' <<<"$LOGIN" 2>/dev/null || true)
  if [ -z "$TOKEN" ]; then
    echo "login failed: $(jq -r '.error.message // "unknown error"' <<<"$LOGIN" 2>/dev/null)" >&2
    echo "check SIGNOZ_EMAIL is the address you signed up with." >&2
    exit 1
  fi
  AUTH=(-H "Authorization: Bearer $TOKEN")
fi

# A notification channel must exist before ANY rule can be created — see the
# comment on SIGNOZ_WEBHOOK_URL above. Rules reference it by name via
# preferredChannels in apps/signoz/alerts/*.json.
echo "==> notification channel '$SIGNOZ_CHANNEL'"
have_channel=$(sig "${AUTH[@]}" "$SIGNOZ_URL/api/v1/channels" \
  | jq -r --arg n "$SIGNOZ_CHANNEL" '[(.data // [])[] | select(.name==$n)] | length' 2>/dev/null || echo 0)
if [ "$have_channel" = "0" ]; then
  if [ -z "$SIGNOZ_WEBHOOK_URL" ]; then
    echo "    no channel and no SIGNOZ_WEBHOOK_URL set — alert rules cannot be created." >&2
    echo "    set SIGNOZ_WEBHOOK_URL=<discord/ntfy/webhook url> and re-run." >&2
    exit 1
  fi
  jq -nc --arg n "$SIGNOZ_CHANNEL" --arg u "$SIGNOZ_WEBHOOK_URL" \
    '{name:$n, webhook_configs:[{send_resolved:true, url:$u}]}' \
    | sig -f -X POST "${AUTH[@]}" -H 'Content-Type: application/json' --data-binary @- \
          "$SIGNOZ_URL/api/v1/channels" >/dev/null && echo "    created"
else
  echo "    exists"
fi

echo "==> retention (logs ${LOGS_TTL_HOURS}h, traces ${TRACES_TTL_HOURS}h, metrics ${METRICS_TTL_HOURS}h)"
# TTL changes apply going forward; they do not retroactively shrink what is
# already on disk.
#
# Logs are the odd one out: they moved to a per-row retention column and the v1
# endpoint now rejects them outright with "SetTTLV2 only supported", so they go
# through /api/v2 with a days-based payload. Traces and metrics still use v1.
if jq -nc --argjson d "$((LOGS_TTL_HOURS / 24))" '{type:"logs", defaultTTLDays:$d, ttlConditions:[]}' \
     | sig -f -X POST "${AUTH[@]}" -H 'Content-Type: application/json' --data-binary @- \
           "$SIGNOZ_URL/api/v2/settings/ttl" >/dev/null 2>&1; then
  echo "    logs: $((LOGS_TTL_HOURS / 24))d"
else
  echo "    logs: FAILED (set it in Settings → General instead)" >&2
fi

for pair in "traces:$TRACES_TTL_HOURS" "metrics:$METRICS_TTL_HOURS"; do
  type="${pair%%:*}"; hours="${pair##*:}"
  if sig -f -X POST "${AUTH[@]}" \
       "$SIGNOZ_URL/api/v1/settings/ttl?type=${type}&duration=${hours}h" >/dev/null 2>&1; then
    echo "    ${type}: ${hours}h"
  else
    echo "    ${type}: FAILED (set it in Settings → General instead)" >&2
  fi
done

echo "==> alert rules from apps/signoz/alerts/"
existing_rules=$(sig "${AUTH[@]}" "$SIGNOZ_URL/api/v1/rules" \
  | jq -r '[.data.rules // .data // []] | flatten | .[] | (.alert // .data.alert) // empty' 2>/dev/null || true)

for f in "$REPO_ROOT"/apps/signoz/alerts/*.json; do
  name=$(jq -r '.alert' "$f")
  if grep -Fxq "$name" <<<"$existing_rules"; then
    echo "    skip (exists): $name"
    continue
  fi
  # No -f here: with it, curl throws the response body away and all you learn is
  # "400". The body is where SigNoz tells you which field it rejected.
  out=$(sig -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
          -w '\n%{http_code}' --data-binary @- "$SIGNOZ_URL/api/v1/rules" <"$f" 2>&1)
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  if [ "$code" = "200" ] || [ "$code" = "201" ]; then
    echo "    created: $name"
  else
    echo "    FAILED ($code): $name" >&2
    echo "      $(jq -r '.error.message // .error // .' <<<"$body" 2>/dev/null | head -c 300)" >&2
  fi
done

echo "==> dashboards from apps/signoz/dashboards/"
existing_dash=$(sig "${AUTH[@]}" "$SIGNOZ_URL/api/v1/dashboards" \
  | jq -r '[.data // []] | flatten | .[] | "\((.data.title // .title // ""))\t\((.id // .uuid // ""))"' 2>/dev/null || true)

for f in "$REPO_ROOT"/apps/signoz/dashboards/*.json; do
  title=$(jq -r '.title' "$f")
  id=$(awk -F'\t' -v t="$title" '$1==t {print $2; exit}' <<<"$existing_dash")
  if [ -n "$id" ]; then
    if sig -f -X PUT "${AUTH[@]}" -H 'Content-Type: application/json' \
         --data-binary @- "$SIGNOZ_URL/api/v1/dashboards/$id" <"$f" >/dev/null 2>&1; then
      echo "    updated: $title"
      continue
    fi
    echo "    update failed, creating instead: $title" >&2
  fi
  out=$(sig -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
          -w '\n%{http_code}' --data-binary @- "$SIGNOZ_URL/api/v1/dashboards" <"$f" 2>&1)
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  if [ "$code" = "200" ] || [ "$code" = "201" ]; then
    echo "    created: $title"
  else
    echo "    FAILED ($code): $title" >&2
    echo "      $(jq -r '.error.message // .error // .' <<<"$body" 2>/dev/null | head -c 300)" >&2
  fi
done

echo
echo "Done. Attach a notification channel to the alert rules in Settings → Alert"
echo "Channels, otherwise they will fire silently."
