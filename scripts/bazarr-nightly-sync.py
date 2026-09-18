#!/usr/bin/env python3
"""Run Bazarr's ffsubsync pass nightly instead of right after every download.

Bazarr can only sync as a post-download hook, and each sync decodes the whole
media file's audio (4-20 GB over the debrid mount). With automatic sync turned
off in Bazarr, this script asks it to sync everything downloaded/upgraded in
the last LOOKBACK hours that still deserves it (score below the threshold and
no sync recorded since). Runs from cron in the night so the bandwidth hit
doesn't land on top of people watching. Stops at DEADLINE regardless.

Reads Bazarr's sqlite history directly (host mount of /config) and calls
PATCH /api/subtitles?action=sync, which is synchronous per file.
"""
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

DB = "/srv/homelab/bazarr/db/bazarr.db"
CFG = "/srv/homelab/bazarr/config/config.yaml"
BAZARR = "http://10.0.1.35:6767"     # container IP; port isn't published on the host
TZ = ZoneInfo("Europe/Paris")        # Bazarr writes history timestamps in its own TZ
LOOKBACK_H = 26                      # overlap the nightly window a little
THRESHOLD = 96.0                     # same as subsync_threshold: only sync below this score
DEADLINE = dt.time(5, 0)             # host is UTC -> 07:00 CEST, before anyone watches
PER_FILE_TIMEOUT = 20 * 60
HOST_MEDIA = "/mnt/media"            # containers see this as /mnt


def apikey():
    with open(CFG) as f:
        return re.search(r"^auth:\n(?:  .*\n)*?  apikey: (\S+)", f.read(), re.M).group(1)


def pending(db):
    """(type, id, language, hi, forced, path) for subs that still need a sync."""
    since = (dt.datetime.now(TZ) - dt.timedelta(hours=LOOKBACK_H)).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    for table, kind, idcol in (("table_history", "episode", "sonarrEpisodeId"),
                               ("table_history_movie", "movie", "radarrId")):
        rows = db.execute(
            f"select timestamp, action, language, subtitles_path, {idcol}, score, score_out_of "
            f"from {table} where timestamp >= ? and subtitles_path is not null", (since,)).fetchall()
        downloads, syncs = {}, {}
        for ts, action, lang, path, mid, score, out_of in rows:
            if action in (1, 3) and ts >= downloads.get(path, ("",))[0]:   # latest download/upgrade wins
                downloads[path] = (ts, kind, mid, lang, (score or 0) * 100.0 / (out_of or 1))
            elif action == 5:
                syncs[path] = max(ts, syncs.get(path, ""))
        for path, (ts, kind, mid, lang, pct) in downloads.items():
            if pct >= THRESHOLD:
                continue
            # Bazarr logs the post-download sync a few ms *before* the download row itself,
            # so anything synced from a minute before the download onwards counts as covered.
            floor = (dt.datetime.fromisoformat(ts) - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            if syncs.get(path, "") >= floor:
                continue
            out[path] = (kind, mid, lang, ts)
    return out


def api(key, method, path, data=None):
    req = urllib.request.Request(f"{BAZARR}{path}", data=urllib.parse.urlencode(data).encode() if data else None,
                                 method=method, headers={"X-API-KEY": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def queued(key, path):
    """True while Bazarr still has a pending/running job for this subtitle."""
    name = f"Syncing {path}"
    return any(j["job_name"] == name and j["status"] in ("pending", "running")
               for j in api(key, "GET", "/api/system/jobs")["data"])


def sync(key, kind, mid, lang, path):
    """Queue the sync in Bazarr and block until that job has left the queue.

    Bazarr 1.6 runs API syncs as background jobs and runs several at once, so
    submitting everything up front would hammer the mount; one at a time keeps
    the read rate to a single file.
    """
    code, _, flag = lang.partition(":")
    api(key, "PATCH", "/api/subtitles", {
        "action": "sync", "type": kind, "id": mid, "language": code, "path": path,
        "hi": str(flag == "hi"), "forced": str(flag == "forced"),
        "gss": "True", "no_fix_framerate": "True",   # match Bazarr's subsync settings
    })
    t0 = time.time()
    time.sleep(2)
    while queued(key, path):
        if time.time() - t0 > PER_FILE_TIMEOUT:
            raise TimeoutError(f"still queued after {PER_FILE_TIMEOUT}s")
        time.sleep(5)


def main():
    key = apikey()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    todo = pending(db)
    print(f"{dt.datetime.now():%F %T} {len(todo)} subtitles to sync")
    done = skipped = failed = 0
    for path, (kind, mid, lang, ts) in sorted(todo.items(), key=lambda kv: kv[1][3]):
        now = dt.datetime.now()
        if now.time() >= DEADLINE and now.time() < dt.time(12):
            print(f"deadline {DEADLINE} reached, leaving {len(todo) - done - skipped - failed} for tomorrow")
            break
        host = path.replace("/mnt/", HOST_MEDIA + "/", 1)
        try:
            open(host, "rb").close()
        except OSError:
            skipped += 1
            continue   # sub was removed/replaced since; Bazarr would 500 on it
        t0 = dt.datetime.now()
        try:
            sync(key, kind, mid, lang, path)
            done += 1
            print(f"synced {lang:6} {path.rsplit('/', 1)[-1][:90]} in {(dt.datetime.now() - t0).seconds}s")
        except Exception as e:
            failed += 1
            print(f"FAILED {lang:6} {path.rsplit('/', 1)[-1][:90]}: {e}", file=sys.stderr)
    print(f"{dt.datetime.now():%F %T} done={done} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
