# primordia

Media server stack on Docker Compose with Traefik + Cloudflare DNS for TLS.

## Services


| Service        | Subdomain        | Port            | Notes                    |
| -------------- | ---------------- | --------------- | ------------------------ |
| traefik        | `traefik.*`      | 80, 443         | Reverse proxy, dashboard |
| plex           | —                | host networking | Not behind Traefik       |
| radarr         | `radarr.*`       | 7878            | Movies                   |
| sonarr (anime) | `sonarr-anime.*` | 8989            | Anime series             |
| sonarr (tv)    | `sonarr-tv.*`    | 8989            | TV series                |
| prowlarr       | `prowlarr.*`     | 9696            | Indexer manager          |
| bazarr         | `bazarr.*`       | 6767            | Subtitles                |
| seerr          | `seerr.*`        | 5055            | Media requests           |
| decypharr      | `decypharr.*`    | 8282            | Debrid + rclone mounts   |
| profilarr      | `profilarr.*`    | 6868            | Quality profile sync     |
| anibridge      | `anibridge.*`    | 4848            | Anime list sync          |


## Setup

```bash
cp .env.example .env
# fill in your values (see table below)
set -a && source .env && set +a

mkdir -p /mnt/{media,symlinks}
chown -R ${PUID}:${PGID} /mnt/media
chown -R ${PUID}:${PGID} /mnt/symlinks

sudo mkdir -p ${APPDATA_DIR}
sudo chown ${PUID}:${PGID} ${APPDATA_DIR}

docker network create traefik

# start everything — containers need to run once to generate default configs
docker compose up -d
```

Requires Docker Engine with Compose v2.20+, a Cloudflare-managed domain, and `jq` for the debrid script.

## Post-startup configuration

Some services generate their default config on first launch. The scripts below overwrite those defaults with the correct settings, so they must run **after** `docker compose up -d` and the containers have finished initializing.

```bash
set -a && source .env && set +a

# set up decypharr with your RealDebrid keys
./scripts/update-debrid-key.sh
docker compose restart decypharr

# install custom Prowlarr indexers (torrentio, zilean, etc.)
./scripts/install-prowlarr-indexers.sh
docker compose restart prowlarr
```

## Environment variables


| Variable           | What it's for                                                            |
| ------------------ | ------------------------------------------------------------------------ |
| `APPDATA_DIR`      | Base path for all app config volumes (e.g. `/srv/homelab`)               |
| `PUID`             | User ID for container file ownership                                     |
| `PGID`             | Group ID for container file ownership                                    |
| `TZ`               | Timezone (e.g. `Europe/Paris`)                                           |
| `LOG_LEVEL`        | Log verbosity for services that support it (e.g. `info`)                 |
| `DOMAIN_NAME`      | Your domain — used in Traefik routing rules                              |
| `ACME_EMAIL`       | Let's Encrypt registration email                                         |
| `CF_DNS_API_TOKEN` | Cloudflare API token (Zone > DNS > Edit)                                 |
| `PLEX_CLAIM`       | One-time claim token from [https://plex.tv/claim](https://plex.tv/claim) |
| `POSTGRES_PASSWORD`| Postgres password for setting up zilean                                  |


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