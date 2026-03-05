# primordia

Media server stack on Docker Compose with Traefik + Cloudflare DNS for TLS.

## Services

| Service | Subdomain | Port | Notes |
|---|---|---|---|
| traefik | `traefik.*` | 80, 443 | Reverse proxy, dashboard |
| plex | — | host networking | Not behind Traefik |
| radarr | `radarr.*` | 7878 | Movies |
| sonarr (anime) | `sonarr-anime.*` | 8989 | Anime series |
| sonarr (tv) | `sonarr-tv.*` | 8989 | TV series |
| prowlarr | `prowlarr.*` | 9696 | Indexer manager |
| bazarr | `bazarr.*` | 6767 | Subtitles |
| seerr | `seerr.*` | 5055 | Media requests |
| decypharr | `decypharr.*` | 8282 | Debrid + rclone mounts |
| profilarr | `profilarr.*` | 6868 | Quality profile sync |

## Setup

```bash
cp .env.example .env
# fill in your values (see table below)

# create appdata dir with correct ownership
sudo mkdir -p /srv/homelab
sudo chown 1000:1000 /srv/homelab

# set up decypharr with your RealDebrid keys
./scripts/update-debrid-key.sh

# start everything
docker compose up -d
```

Requires Docker Engine with Compose v2.20+, a Cloudflare-managed domain, and `jq` for the debrid script.

## Environment variables

| Variable | What it's for |
|---|---|
| `APPDATA_DIR` | Base path for all app config volumes (e.g. `/srv/homelab`) |
| `DOMAIN_NAME` | Your domain — used in Traefik routing rules |
| `ACME_EMAIL` | Let's Encrypt registration email |
| `CF_DNS_API_TOKEN` | Cloudflare API token (Zone > DNS > Edit) |
| `PLEX_CLAIM` | One-time claim token from https://plex.tv/claim |

## Decypharr config

Live `config.json` is gitignored. Template lives at `apps/decypharr/config/config.example.json`.

```bash
./scripts/update-debrid-key.sh          # first time — prompts for keys
./scripts/update-debrid-key.sh update   # rebuild from template, keep existing keys
```

## Adding a new app

1. Create `apps/myapp/docker-compose.yml` (copy any existing one as reference)
2. Add it to the root `docker-compose.yml` include list
3. Add `depends_on` in the root services section if needed

## Traefik

Static config in `apps/traefik/config/traefik.yml`. Dynamic configs in `apps/traefik/config/dynamic/` — drop new `.yml` files there, Traefik picks them up without a restart.

Ships with `security-headers` (HSTS, XSS, nosniff, frame deny) applied to all routers, and a `rate-limit` middleware available but not attached by default.
