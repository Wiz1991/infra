#!/usr/bin/env python3
"""Prometheus exporter for Tautulli's activity API.

Tautulli has no /metrics endpoint and there is no maintained exporter image for
it, but its API already knows everything worth knowing about Plex playback: who
is watching, what, whether it is transcoding, and how much bandwidth it costs.
This polls get_activity and republishes it as Prometheus metrics.

Standard library only, so there is no image to build and nothing to keep
patched — it runs on a stock python:alpine.

Env:
  TAUTULLI_URL      base URL, e.g. http://tautulli:8181
  TAUTULLI_API_KEY  API key (Settings -> Web Interface -> API)
  POLL_INTERVAL     seconds between polls (default 30)
  PORT              listen port (default 9487)
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://tautulli:8181").rstrip("/")
API_KEY = os.environ["TAUTULLI_API_KEY"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "9487"))
# Tautulli's playback history is the interesting part and it barely changes, so
# it's pulled on a much slower cadence than live activity and cached in between.
HISTORY_INTERVAL = int(os.environ.get("HISTORY_INTERVAL", "600"))

_history_cache = {"at": 0.0, "rows": []}

# Rendered Prometheus text, swapped in wholesale by the poller.
_metrics = "# tautulli exporter starting\n"
_lock = threading.Lock()


def _call(cmd, **params):
    q = {"apikey": API_KEY, "cmd": cmd, **params}
    url = f"{TAUTULLI_URL}/api/v2?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())["response"]["data"]


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def collect():
    out = []

    def metric(name, help_text, mtype, samples):
        # Skip empty families entirely rather than emit a bare HELP/TYPE pair.
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        out.extend(samples)

    a = _call("get_activity")
    sessions = a.get("sessions", [])

    metric("tautulli_up", "1 if the Tautulli API answered", "gauge", ["tautulli_up 1"])
    metric(
        "tautulli_streams",
        "Active Plex streams",
        "gauge",
        [f"tautulli_streams {len(sessions)}"],
    )

    # Bandwidth is reported in kbps by Tautulli; publish bits/sec so the
    # dashboard can use a normal bitrate unit.
    metric(
        "tautulli_bandwidth_bits_per_second",
        "Plex playback bandwidth",
        "gauge",
        [
            f'tautulli_bandwidth_bits_per_second{{scope="{label}"}} '
            f"{int(a.get(key) or 0) * 1000}"
            for key, label in (
                ("total_bandwidth", "total"),
                ("lan_bandwidth", "lan"),
                ("wan_bandwidth", "wan"),
            )
        ],
    )

    # Transcode vs direct play is the number that actually predicts whether the
    # box falls over, so break the streams down by decision.
    decisions = {}
    states = {}
    per_user = {}
    for s in sessions:
        d = (s.get("transcode_decision") or "unknown").lower()
        decisions[d] = decisions.get(d, 0) + 1
        st = (s.get("state") or "unknown").lower()
        states[st] = states.get(st, 0) + 1
        per_user[s.get("friendly_name") or s.get("user") or "unknown"] = (
            per_user.get(s.get("friendly_name") or s.get("user") or "unknown", 0) + 1
        )

    metric(
        "tautulli_streams_by_decision",
        "Active streams by transcode decision",
        "gauge",
        [
            f'tautulli_streams_by_decision{{decision="{_esc(k)}"}} {v}'
            for k, v in decisions.items()
        ],
    )
    metric(
        "tautulli_streams_by_state",
        "Active streams by playback state",
        "gauge",
        [f'tautulli_streams_by_state{{state="{_esc(k)}"}} {v}' for k, v in states.items()],
    )
    metric(
        "tautulli_streams_by_user",
        "Active streams by user",
        "gauge",
        [f'tautulli_streams_by_user{{user="{_esc(k)}"}} {v}' for k, v in per_user.items()],
    )

    # One series per active session. Cardinality is bounded by concurrent
    # viewers (a handful), and it dies with the session — safe to label richly.
    sess = []
    for s in sessions:
        labels = ",".join(
            f'{k}="{_esc(s.get(v, ""))}"'
            for k, v in (
                ("user", "friendly_name"),
                ("player", "player"),
                ("media_type", "media_type"),
                ("title", "full_title"),
                ("decision", "transcode_decision"),
                ("quality", "quality_profile"),
            )
        )
        sess.append(f"tautulli_session{{{labels}}} 1")
    metric("tautulli_session", "An active playback session", "gauge", sess)

    # ---- playback history -------------------------------------------------
    # Tautulli has been recording every play since long before this stack
    # existed. Rather than backfill that into ClickHouse (where retention would
    # delete it again), republish it as rolling aggregates: the numbers are
    # current values, so they populate a dashboard the moment it loads and they
    # never age out.
    now = time.time()
    if now - _history_cache["at"] > HISTORY_INTERVAL:
        try:
            _history_cache["rows"] = _call("get_history", length=100000).get("data", [])
            _history_cache["at"] = now
        except Exception:
            pass  # keep serving the last good history
    rows = _history_cache["rows"]

    if rows:
        def total(pred):
            return sum(1 for r in rows if pred(r))

        def secs(pred):
            return sum(int(r.get("duration") or 0) for r in rows if pred(r))

        windows = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
        plays, watch, transcodes = [], [], []
        for label, span in windows.items():
            def in_win(r, span=span):
                return span is None or (now - int(r.get("date") or 0)) <= span
            plays.append(f'tautulli_history_plays{{window="{label}"}} {total(in_win)}')
            watch.append(
                f'tautulli_history_watch_seconds{{window="{label}"}} {secs(in_win)}'
            )
            transcodes.append(
                f'tautulli_history_transcodes{{window="{label}"}} '
                f'{total(lambda r, w=in_win: w(r) and r.get("transcode_decision") == "transcode")}'
            )
        metric("tautulli_history_plays", "Plays in a rolling window", "gauge", plays)
        metric("tautulli_history_watch_seconds", "Watch time in a rolling window", "gauge", watch)
        metric("tautulli_history_transcodes", "Transcoded plays in a rolling window", "gauge", transcodes)

        # Per-user and per-media-type breakdowns, all-time. Cardinality is a
        # handful of users, so this is cheap.
        by_user_plays, by_user_secs = {}, {}
        by_type, by_decision = {}, {}
        for r in rows:
            u = r.get("friendly_name") or r.get("user") or "unknown"
            by_user_plays[u] = by_user_plays.get(u, 0) + 1
            by_user_secs[u] = by_user_secs.get(u, 0) + int(r.get("duration") or 0)
            t = r.get("media_type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
            d = r.get("transcode_decision") or "unknown"
            by_decision[d] = by_decision.get(d, 0) + 1

        metric("tautulli_history_plays_by_user", "All-time plays per user", "gauge",
               [f'tautulli_history_plays_by_user{{user="{_esc(k)}"}} {v}'
                for k, v in by_user_plays.items()])
        metric("tautulli_history_watch_seconds_by_user", "All-time watch time per user", "gauge",
               [f'tautulli_history_watch_seconds_by_user{{user="{_esc(k)}"}} {v}'
                for k, v in by_user_secs.items()])
        metric("tautulli_history_plays_by_media_type", "All-time plays per media type", "gauge",
               [f'tautulli_history_plays_by_media_type{{media_type="{_esc(k)}"}} {v}'
                for k, v in by_type.items()])
        metric("tautulli_history_plays_by_decision", "All-time plays per transcode decision", "gauge",
               [f'tautulli_history_plays_by_decision{{decision="{_esc(k)}"}} {v}'
                for k, v in by_decision.items()])

    # Library totals: how much media exists, per section.
    try:
        libs = [
            f'tautulli_library_items{{library="{_esc(l.get("section_name"))}",'
            f'type="{_esc(l.get("section_type"))}"}} {int(l.get("count") or 0)}'
            for l in _call("get_libraries")
        ]
        metric("tautulli_library_items", "Items in a Plex library", "gauge", libs)
    except Exception:
        pass

    return "\n".join(out) + "\n"


def poll_forever():
    global _metrics
    while True:
        try:
            text = collect()
        except Exception as e:  # Tautulli restarting, API key rotated, etc.
            text = f'tautulli_up 0\n# error: {str(e)[:200]}\n'
        with _lock:
            _metrics = text
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with _lock:
            body = _metrics.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # don't log every scrape
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_forever, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
