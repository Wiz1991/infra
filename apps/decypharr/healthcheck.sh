#!/bin/sh
# Decypharr healthcheck: verify the API is up AND every FUSE mount under
# /mnt/remote is listable. A stale rclone mount returns ENOTCONN via ls
# — fails here, which prevents dependents (Plex) from starting against
# dead mounts. Iterates /mnt/remote generically so adding or removing a
# provider needs no edit here.
set -eu

/usr/bin/healthcheck --config /app/

cd /mnt/remote
for d in *; do
    [ "$d" = "*" ] && exit 1   # nothing mounted yet
    timeout 5 ls "$d" >/dev/null || exit 1
done
