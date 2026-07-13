#!/usr/bin/env python3
"""Backfill Plex viewing history from Tautulli into SigNoz as daily metrics.

Tautulli has been recording every play since long before this stack existed, so
new dashboards would otherwise start as flat lines. This reads its history and
pushes one datapoint per day, stamped with that day's real timestamp, straight
to the OTLP ingester.

    docker run --rm --network traefik \
      -v "$PWD/scripts/tautulli-backfill.py:/app/b.py:ro" \
      -e TAUTULLI_API_KEY="$TAUTULLI_API_KEY" \
      python:3.12-alpine python /app/b.py

RETENTION IS THE CATCH. SigNoz's metrics TTL is 30 days, and ClickHouse enforces
it on the datapoint's own timestamp — so anything you backfill older than the TTL
is deleted at the next merge, no matter that you just wrote it. DAYS defaults to
the TTL for that reason. Raising the TTL to cover the full history would multiply
the storage of *every* metric in the stack (they're all on the same TTL), which
this host does not have the disk for.

The longer history isn't lost: the exporter republishes it as all-time rolling
aggregates (tautulli_history_*), and Tautulli itself remains the system of record.

Env:
  TAUTULLI_URL      default http://tautulli:8181
  TAUTULLI_API_KEY  required
  OTLP_ENDPOINT     default http://signoz-ingester:4318
  DAYS              how far back to backfill (default 30, matching the TTL)
"""
import collections
import datetime
import json
import os
import urllib.parse
import urllib.request

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://tautulli:8181").rstrip("/")
API_KEY = os.environ["TAUTULLI_API_KEY"]
OTLP = os.environ.get("OTLP_ENDPOINT", "http://signoz-ingester:4318").rstrip("/")
DAYS = int(os.environ.get("DAYS", "30"))


def tautulli(cmd, **params):
    q = {"apikey": API_KEY, "cmd": cmd, **params}
    url = f"{TAUTULLI_URL}/api/v2?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())["response"]["data"]


def attrs(**kw):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in kw.items()]


def main():
    rows = tautulli("get_history", length=100000).get("data", [])
    print(f"fetched {len(rows)} history records from Tautulli")

    cutoff = datetime.datetime.now() - datetime.timedelta(days=DAYS)
    cutoff_ts = cutoff.timestamp()

    # day -> (media_type, user, decision) -> [plays, seconds]
    daily = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    skipped = 0
    for r in rows:
        ts = int(r.get("date") or 0)
        if ts < cutoff_ts:
            skipped += 1
            continue
        day = datetime.date.fromtimestamp(ts)
        key = (
            r.get("media_type") or "unknown",
            r.get("friendly_name") or r.get("user") or "unknown",
            r.get("transcode_decision") or "unknown",
        )
        d = daily[day][key]
        d[0] += 1
        d[1] += int(r.get("duration") or 0)

    if skipped:
        print(f"skipped {skipped} records older than {DAYS}d — the metrics TTL would "
              f"delete them anyway (see the note at the top of this file)")

    plays, watch = [], []
    for day, keys in sorted(daily.items()):
        # Stamp each day at noon UTC: unambiguous, and safely inside the day
        # whatever the viewer's timezone.
        when = datetime.datetime.combine(day, datetime.time(12, 0))
        nanos = str(int(when.timestamp() * 1_000_000_000))
        for (mtype, user, decision), (n, secs) in keys.items():
            a = attrs(media_type=mtype, user=user, decision=decision)
            plays.append({"timeUnixNano": nanos, "asInt": str(n), "attributes": a})
            watch.append({"timeUnixNano": nanos, "asInt": str(secs), "attributes": a})

    if not plays:
        print("nothing to backfill")
        return

    payload = {
        "resourceMetrics": [{
            "resource": {"attributes": attrs(**{
                "service.name": "tautulli",
                "service.namespace": "media",
                "deployment.environment": "homelab",
            })},
            "scopeMetrics": [{
                "scope": {"name": "tautulli-backfill"},
                "metrics": [
                    {"name": "tautulli_plays_daily",
                     "description": "Plex plays per day, backfilled from Tautulli",
                     "unit": "1",
                     "gauge": {"dataPoints": plays}},
                    {"name": "tautulli_watch_seconds_daily",
                     "description": "Plex watch time per day, backfilled from Tautulli",
                     "unit": "s",
                     "gauge": {"dataPoints": watch}},
                ],
            }],
        }]
    }

    req = urllib.request.Request(
        f"{OTLP}/v1/metrics",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()[:200]
        print(f"pushed {len(plays)} datapoints across {len(daily)} days -> HTTP {r.status} {body}")


if __name__ == "__main__":
    main()
