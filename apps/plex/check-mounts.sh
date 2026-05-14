#!/bin/sh
# Probe every FUSE mount under /mnt/remote. Exits 0 if all listable, 1
# otherwise. Used by Plex's healthcheck and by wait-for-mounts.sh.
# Iterates generically so adding or removing a decypharr provider needs
# no edit here.
set -eu

cd /mnt/remote
for d in *; do
    [ "$d" = "*" ] && exit 1   # nothing mounted yet
    timeout 5 ls "$d" >/dev/null || exit 1
done
