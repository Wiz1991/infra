#!/usr/bin/env python3
"""Nightly: fix media symlinks whose debrid item no longer exists.

decypharr v2's repair only probes torrents it still has in its store; a
symlink pointing at an item that was removed from the debrid account entirely
(expired, DMCA'd) is skipped by its sweep and stays dead forever. This script
finds those links under the Arr root folders, and for each one deletes the
file record in Sonarr/Radarr (which removes the dead symlink) and re-searches
if monitored. The old release is deliberately not blocklisted: an item gone
from one provider is often still cached on the other, and decypharr rejects
the grab itself if nobody has it.

Capped per run so a bad night (provider wipes a batch) becomes a few days of
re-grabs instead of one indexer-hammering storm. `--dry-run` only reports.

Runs on the host: symlink targets are container paths (/mnt/remote/...), so
they're rewritten to /mnt/media/remote/... before the existence check.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HOST_MEDIA = "/mnt/media"          # containers see this as /mnt
ARRS = {
    # name: (config.xml, container IP, root folders as the Arr sees them)
    "sonarr": ("/srv/homelab/sonarr/config.xml", "10.0.1.31", ("/mnt/hardlinks/tv", "/mnt/hardlinks/anime")),
    "radarr": ("/srv/homelab/radarr/config.xml", "10.0.1.34", ("/mnt/hardlinks/movies",)),
}
DEFAULT_CAP = 25


class Arr:
    def __init__(self, name, cfg, ip, roots):
        xml = open(cfg).read()
        self.name = name
        self.roots = roots
        self.key = re.search(r"<ApiKey>([^<]+)", xml).group(1)
        self.base = f"http://{ip}:{re.search(r'<Port>([^<]+)', xml).group(1)}/api/v3"
        self.tv = name == "sonarr"

    def call(self, method, path, params=None, body=None):
        url = f"{self.base}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, method=method, headers={"X-Api-Key": self.key, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def dead_links(self):
        """Yield Arr-side file paths under our roots whose symlink target is gone."""
        for root in self.roots:
            for dirpath, _, files in os.walk(root.replace("/mnt/", HOST_MEDIA + "/", 1)):
                for f in files:
                    p = os.path.join(dirpath, f)
                    if not os.path.islink(p):
                        continue
                    t = os.readlink(p)
                    if t.startswith("/mnt/"):
                        t = t.replace("/mnt/", HOST_MEDIA + "/", 1)
                    if not os.path.exists(t):
                        yield p.replace(HOST_MEDIA + "/", "/mnt/", 1)

    # --- resolving a path to the Arr's file record -------------------------

    def media_for(self, path):
        """The series/movie owning `path`, by longest matching folder."""
        if not hasattr(self, "_media"):
            self._media = sorted(self.call("GET", "series" if self.tv else "movie"),
                                 key=lambda m: -len(m["path"]))
        for m in self._media:
            if path.startswith(m["path"].rstrip("/") + "/"):
                return m
        return None

    def file_for(self, media, path):
        kind = "episodefile" if self.tv else "moviefile"
        idkey = "seriesId" if self.tv else "movieId"
        for f in self.call("GET", kind, {idkey: media["id"]}):
            if f["path"] == path:
                return f
        return None

    # --- the fix -----------------------------------------------------------

    def fix(self, media, file, dry):
        """Delete the dead file record; return search key(s) if monitored."""
        kind = "episodefile" if self.tv else "moviefile"
        if self.tv:
            eps = [e for e in self.call("GET", "episode", {"seriesId": media["id"]}) if e.get("episodeFileId") == file["id"]]
            monitored = media["monitored"] and any(e["monitored"] for e in eps)
            keys = [(media["id"], e["seasonNumber"], e["id"]) for e in eps]
        else:
            monitored = media["monitored"]
            keys = [(media["id"],)]
        label = f"{media['title']} :: {os.path.basename(file['path'])[:80]}"
        print(f"{'DRY  ' if dry else 'FIXED'} {self.name} {label} | search={'yes' if monitored else 'unmonitored'}")
        dry or self.call("DELETE", f"{kind}/{file['id']}")
        return keys if monitored else []

    def search(self, keys, dry):
        """One search per movie; per season when >=3 of its episodes died together, else per episode."""
        if not self.tv:
            ids = sorted({k[0] for k in keys})
            if ids:
                print(f"{'DRY  ' if dry else 'SEARCH'} {self.name} MoviesSearch x{len(ids)}")
                dry or self.call("POST", "command", body={"name": "MoviesSearch", "movieIds": ids})
            return
        by_season = {}
        for series, season, ep in keys:
            by_season.setdefault((series, season), set()).add(ep)
        for (series, season), eps in by_season.items():
            if len(eps) >= 3:
                print(f"{'DRY  ' if dry else 'SEARCH'} {self.name} SeasonSearch series={series} season={season} ({len(eps)} eps)")
                dry or self.call("POST", "command", body={"name": "SeasonSearch", "seriesId": series, "seasonNumber": season})
            else:
                print(f"{'DRY  ' if dry else 'SEARCH'} {self.name} EpisodeSearch {sorted(eps)}")
                dry or self.call("POST", "command", body={"name": "EpisodeSearch", "episodeIds": sorted(eps)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP, help="max files to fix this run")
    a = ap.parse_args()
    fixed = skipped = 0
    for name, (cfg, ip, roots) in ARRS.items():
        arr = Arr(name, cfg, ip, roots)
        keys = []
        for path in arr.dead_links():
            if fixed >= a.cap:
                skipped += 1
                continue
            media = arr.media_for(path)
            file = media and arr.file_for(media, path)
            if not file:
                # dead link the Arr doesn't track (already removed from Arr, or a stray): just drop the link
                print(f"{'DRY  ' if a.dry_run else 'RM   '} untracked {path[:110]}")
                a.dry_run or os.remove(path.replace("/mnt/", HOST_MEDIA + "/", 1))
                continue
            try:
                keys += arr.fix(media, file, a.dry_run)
                fixed += 1
            except Exception as e:
                print(f"FAILED {name} {path[:100]}: {e}", file=sys.stderr)
        if keys:
            arr.search(keys, a.dry_run)
    print(f"done fixed={fixed} deferred={skipped}" + (" (dry run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
