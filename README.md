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
| signoz         | `signoz.*`       | 8080            | Logs, metrics, traces    |


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
| `SIGNOZ_POSTGRES_PASSWORD` | Password for SigNoz's own Postgres metadata store (not pgvector) |


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
4. Give it the `service.name` / `service.namespace` labels and the `logging`
   anchor — see [Service naming](#service-naming--read-this-before-adding-an-app).
   Without them the app's logs land in SigNoz with no service attached.

## Observability

SigNoz (`apps/signoz`) stores logs, metrics and traces in ClickHouse; a single
OpenTelemetry Collector (`apps/otel-collector`) does all the collecting and ships
to it over OTLP. This replaced Grafana + Loki + Tempo + Prometheus + Alloy.

Nothing scrapes or tails anything else — if a signal isn't in SigNoz, it's
because the collector isn't configured for it.

| Source                                | How it's collected                                               |
| ------------------------------------- | ---------------------------------------------------------------- |
| Container stdout/stderr               | `filelog` over `/var/lib/docker/containers/*/*-json.log`          |
| Plex's own log                        | `filelog` over the Plex `Logs/` dir (stdout is only s6 noise)     |
| Traefik access log (4xx/5xx)          | `filelog`, JSON-parsed                                            |
| Container CPU/mem/restarts            | `docker_stats` receiver                                           |
| Host CPU/mem/disk/network             | `hostmetrics` receiver                                            |
| *arr / redis / postgres / traefik     | `prometheus` receiver against the existing exporters              |
| Plex libraries + bandwidth            | `apps/plex-exporter` (Plex API) → scraped as job `plex`            |
| Plex streams, users, transcodes       | `apps/tautulli-exporter` (Tautulli API) → job `tautulli`           |
| Uptime of Plex, *arr, Real-Debrid, AllDebrid | `httpcheck` receiver → `httpcheck.status`                  |
| moe-radar (workerd)                   | App sends OTLP straight to `signoz-ingester:4318`                 |

### Which logs are stored — collection is OPT-IN

A container's stdout is **not** collected unless it asks for it:

```yaml
    labels:
      - otel.logs=stdout     # collect this container's stdout
```

Today that's `sonarr`, `radarr` and `decypharr`. Everything else — redis, adguard,
cloudflared, the workerd fleet, SigNoz's own containers — produces no logs at all.
A container that ships its **own** logs (an OTel SDK, a pino OTLP transport) simply
omits the label and so can never be double-ingested.

Plex is deliberately *not* in that list: its stdout is only s6 supervisor noise, and
its real log is read from the file instead (`filelog/plex`). Logs read from files —
Plex's log, Traefik's access log — are configured explicitly in their own receivers,
which is opt-in by construction.

This governs logs only. **Metrics and uptime probes still cover every service**, and
logs are the only signal that meaningfully consumes disk.

### Plex viewing history

Tautulli has been recording every play since long before this stack existed, so the
Plex dashboard doesn't start from a blank slate. Two mechanisms, because retention
forces the split:

* **Rolling aggregates** (`tautulli_history_*`) — plays, watch time and transcode
  counts over 24h / 7d / 30d / all-time, plus per-user and per-media-type
  breakdowns. The exporter recomputes these from Tautulli's full history on every
  scrape, so they're *current values* and never age out.
* **Daily series** (`tautulli_plays_daily`, `tautulli_watch_seconds_daily`) —
  backfilled with real past timestamps by `scripts/tautulli-backfill.py`:

  ```bash
  set -a && source .env && set +a
  docker run --rm --network traefik \
    -v "$PWD/scripts/tautulli-backfill.py:/app/b.py:ro" \
    -e TAUTULLI_API_KEY="$TAUTULLI_API_KEY" \
    python:3.12-alpine python /app/b.py
  ```

You cannot backfill further than the metrics TTL (30d). ClickHouse enforces TTL on
the datapoint's *own* timestamp, so anything older is deleted at the next merge no
matter that you just wrote it — and raising the TTL would multiply the storage of
every other metric in the stack, which this disk can't take. That's what the
all-time rolling aggregates are for; Tautulli remains the system of record for
deeper history.

### Dashboards

Four, in `apps/signoz/dashboards/`, applied by `scripts/signoz-setup.sh`: **Plex**
(streams, transcode decisions, bandwidth, libraries, errors), **Debrid & decypharr**
(provider uptime, the repair job, activity by provider/arr), **Sonarr & Radarr**
(queues, missing, errors by subsystem), and **Containers & Host**.

They're generated by `apps/signoz/dashboards/build.py` rather than hand-written —
edit that and re-run it, because SigNoz's widget JSON is far too verbose to maintain
by hand. Three metric traps are documented at the top of that file; read them before
adding a panel.

Note the SigNoz **Services** page will only ever show moe-radar's workerd processes.
That page is APM and is built from *traces*; Plex, the *arr apps and decypharr are
closed-source and emit none. Their data lives in Logs, Metrics and the dashboards
above — this is SigNoz working as designed, not a misconfiguration.

### Service naming — read this before adding an app

Every service is labeled, and **the labels are load-bearing**. Docker's json-file
driver copies container labels into each log line only when the container asks it
to, so each service carries both halves:

```yaml
    labels:
      - service.name=radarr
      - service.namespace=automation
    logging: *logging          # the x-logging anchor at the top of the file
```

Without them the log file path contains only the container ID, and the logs
arrive in SigNoz unnamed. A new app that skips this block will look like it isn't
logging at all. `service.namespace` is one of: `media`, `automation`, `debrid`,
`infra`, `observability`, `moe-radar`.

The `logging` anchor also caps each container's log at 10MB × 3 files, which is
what keeps `/var/lib/docker` from quietly eating the disk.

### First-time setup

```bash
docker compose up -d signoz otel-collector
# 1. create the admin user at https://signoz.${DOMAIN_NAME}
#    (until an org exists, the ingester has NO receivers — opamp pushes it a stub
#    config and OTLP port 4317 stays closed. This is not obvious from the logs.)
# 2. then:
SIGNOZ_API_KEY=... SIGNOZ_WEBHOOK_URL=https://discord.com/api/webhooks/... \
  ./scripts/signoz-setup.sh
```

Two traps worth knowing:

* **A notification channel must exist before any alert rule can be created.** A rule
  builds an alertmanager route, and a route with no channel is invalid — so every
  rule 400s with *"at least one channel is required"* until one exists. Hence
  `SIGNOZ_WEBHOOK_URL`.
* **An API key created without a role 403s on everything** ("only viewers/editors/
  admins can access this resource"). SigNoz will happily issue one. Check the key's
  service account has a role.

That sets retention (7d logs/traces, 30d metrics) and creates the alert rules in
`apps/signoz/alerts/`: Plex down, any service down, a debrid provider down,
decypharr's repair job failing *or* silently not running, container restart
loops, and disk over 90%. Rules fire nowhere until you add a notification channel
under Settings → Alert Channels (Discord accepts Slack-format webhooks — use the
Slack channel type with `/slack` appended to your Discord webhook URL).

### Upgrading SigNoz

SigNoz no longer ships a maintained compose file; it generates one via Foundry.
`apps/signoz/config/casting.yaml` is the input that produced what's committed
here. To upgrade, re-run `foundryctl forge -f apps/signoz/config/casting.yaml`,
diff its `pours/deployment/` output against `apps/signoz/`, and re-apply the
local deltas listed at the top of `apps/signoz/docker-compose.yml`.

## Traefik

Static config in `apps/traefik/config/traefik.yml`. Dynamic configs in `apps/traefik/config/dynamic/` — drop new `.yml` files there, Traefik picks them up without a restart.

Ships with `security-headers` (HSTS, XSS, nosniff, frame deny) applied to all routers, and a `rate-limit` middleware available but not attached by default.