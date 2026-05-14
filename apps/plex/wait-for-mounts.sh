#!/command/with-contenv sh
# Block Plex startup until every FUSE mount under /mnt/remote is listable.
# Runs from /custom-cont-init.d, gating s6 service startup. Without this,
# a Docker auto-restart (which bypasses compose's depends_on) could let
# Plex scan a dead-mount tree and prune the library.

MAX_ATTEMPTS=60   # 60 * 10s = 10 minutes
SLEEP=10

i=0
until /usr/local/bin/check-mounts; do
    i=$((i + 1))
    if [ "$i" -ge "$MAX_ATTEMPTS" ]; then
        echo "[wait-for-mounts] mounts still dead after $((MAX_ATTEMPTS * SLEEP))s — aborting Plex startup to protect library"
        exit 1
    fi
    echo "[wait-for-mounts] attempt $i/$MAX_ATTEMPTS — mounts not ready, sleeping ${SLEEP}s"
    sleep "$SLEEP"
done
echo "[wait-for-mounts] all /mnt/remote mounts ready, starting Plex"
