#!/usr/bin/env python3
"""Pre-warm the next episode of whatever is playing into the rclone VFS cache.

Runs from cron every minute on the host. For each active Plex episode session
it finds the next episode (same season, or first of the next season), resolves
its symlink to the debrid mount and reads the first and last few MB. With
vfs_cache_mode=full those bytes stay on NVMe for 24h, so when the viewer hits
"next episode" Plex's probe (header + cues at EOF) is served from disk instead
of costing several round trips to the debrid CDN.

Host-side quirks: the symlinks under /mnt/media/hardlinks point at container
paths (/mnt/remote/...), so they never resolve on the host; we rewrite the
target to /mnt/media/remote/... and read that directly. A tiny state file
avoids re-warming the same episode every minute.
"""
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

PLEX = "http://localhost:32400"
PREFS = "/srv/homelab/plex/Library/Application Support/Plex Media Server/Preferences.xml"
STATE = "/tmp/plex-prewarm-state.json"
HEAD_BYTES = 8 * 1024 * 1024   # container header, first cluster, enough for the probe + start
TAIL_BYTES = 4 * 1024 * 1024   # MKV cues/index live at the end
WARM_TTL = 12 * 3600           # rclone keeps cache 24h; re-warm after 12h
HOST_MEDIA = "/mnt/media"      # containers see this as /mnt


def token():
    with open(PREFS) as f:
        return re.search(r'PlexOnlineToken="([^"]+)"', f.read()).group(1)


def get(path, tok):
    req = urllib.request.Request(f"{PLEX}{path}", headers={"X-Plex-Token": tok})
    with urllib.request.urlopen(req, timeout=10) as r:
        return ET.fromstring(r.read())


def next_episode(video, tok):
    """Return the <Video> element of the episode after `video`, or None."""
    cur_idx = int(video.get("index") or 0)
    season = video.get("parentRatingKey")
    show = video.get("grandparentRatingKey")
    if not season or not show:
        return None
    eps = sorted(get(f"/library/metadata/{season}/children", tok).iter("Video"),
                 key=lambda e: int(e.get("index") or 0))
    later = [e for e in eps if int(e.get("index") or 0) > cur_idx]
    if later:
        return later[0]
    # last episode of the season: first episode of the next season, if any
    cur_season_idx = int(video.get("parentIndex") or 0)
    seasons = sorted(get(f"/library/metadata/{show}/children", tok).iter("Directory"),
                     key=lambda d: int(d.get("index") or 0))
    nxt = [s for s in seasons if int(s.get("index") or 0) > cur_season_idx]
    if not nxt:
        return None
    eps = sorted(get(f"/library/metadata/{nxt[0].get('ratingKey')}/children", tok).iter("Video"),
                 key=lambda e: int(e.get("index") or 0))
    return eps[0] if eps else None


def host_path(plex_file):
    """/mnt/hardlinks/... as Plex sees it -> readable host path through the mount."""
    p = plex_file.replace("/mnt/", HOST_MEDIA + "/", 1)
    if os.path.islink(p):
        t = os.readlink(p)
        if t.startswith("/mnt/"):
            t = t.replace("/mnt/", HOST_MEDIA + "/", 1)
        return t
    return p


def warm(path):
    size = os.stat(path).st_size
    with open(path, "rb", buffering=0) as f:
        f.read(min(HEAD_BYTES, size))
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.read(TAIL_BYTES)
    return size


def main():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    state = {k: v for k, v in state.items() if now - v < WARM_TTL}

    tok = token()
    sessions = get("/status/sessions", tok)
    for video in sessions.iter("Video"):
        if video.get("type") != "episode":
            continue
        try:
            nxt = next_episode(video, tok)
        except Exception as e:
            print(f"next-episode lookup failed for {video.get('ratingKey')}: {e}", file=sys.stderr)
            continue
        if nxt is None:
            continue
        key = nxt.get("ratingKey")
        if key in state:
            continue
        part = nxt.find(".//Part")
        if part is None or not part.get("file"):
            continue
        path = host_path(part.get("file"))
        t0 = time.time()
        try:
            size = warm(path)
        except OSError as e:
            print(f"warm failed {nxt.get('grandparentTitle')} S{nxt.get('parentIndex')}E{nxt.get('index')}: {e}", file=sys.stderr)
            state[key] = now  # don't hammer a dead link every minute
            continue
        state[key] = now
        print(f"warmed {nxt.get('grandparentTitle')} S{nxt.get('parentIndex')}E{nxt.get('index')} "
              f"({size // 1048576} MB) in {time.time() - t0:.1f}s")

    with open(STATE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
