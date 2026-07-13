#!/usr/bin/env python3
"""Generate the SigNoz alert rules in this directory.

    python3 apps/signoz/alerts/build.py     # then apply with scripts/signoz-setup.sh

Rules are generated so that every alert is named after the thing that's actually
broken. A single grouped rule called "Service is down" tells you nothing when it
lands in Discord at 3am — you'd have to open SigNoz to find out *which* service.
One rule per probed service costs nothing and the name alone is the diagnosis.

Hard-won details, don't regress them:
  * temporality is LOWERCASE ("unspecified"/"cumulative"). Capitalised = 400.
  * the field is `selectedQueryName`, not `selectedQuery`.
  * httpcheck.status emits one series per HTTP status class, so an up-check MUST
    filter http.status_class = '2xx' — without it the 0-valued series for the
    other classes always exist and the alert fires permanently.
  * every rule needs preferredChannels: SigNoz refuses to create a rule that has
    no notification channel ("at least one channel is required").
"""
import json
import pathlib

OUT = pathlib.Path(__file__).parent
CHANNELS = ["default"]

# probe.service -> (human name, severity, why it matters)
PROBES = {
    "plex":       ("Plex", "critical",
                   "Plex is gated on decypharr's FUSE mounts (apps/plex/wait-for-mounts.sh), "
                   "so check the mounts first — a dead debrid mount takes Plex down with it."),
    "decypharr":  ("decypharr", "critical",
                   "Downloads and symlinking stop. Plex will follow if the mounts go stale."),
    "radarr":     ("Radarr", "warning", "Movie automation is down."),
    "sonarr":     ("Sonarr", "warning", "TV automation is down."),
    "prowlarr":   ("Prowlarr", "warning", "Indexers are down, so nothing new can be found."),
    "bazarr":     ("Bazarr", "warning", "Subtitle fetching is down."),
    "seerr":      ("Seerr", "warning", "Requests can't be made."),
    "tautulli":   ("Tautulli", "warning", "Playback stats stop — Plex itself is unaffected."),
    "zilean":     ("Zilean", "warning", "The DMM index is down."),
}

# Debrid providers, probed at their own public API so an outage is attributable
# to the provider rather than to decypharr.
PROVIDERS = {
    "realdebrid": ("Real-Debrid", "critical"),
    "alldebrid":  ("AllDebrid", "warning"),
}


def rule(name, *, alert_type, severity, desc, summary, detail,
         signal, aggregations, filt, evalw, freq, op, target, match,
         group=(), step="1m", extra_condition=None, labels=None):
    cond = {
        "compositeQuery": {
            "queryType": "builder",
            "panelType": "graph",
            "queries": [{
                "type": "builder_query",
                "spec": {
                    "name": "A",
                    "signal": signal,
                    "stepInterval": step,
                    "filter": {"expression": filt},
                    "aggregations": aggregations,
                    **({"groupBy": [{"name": g} for g in group]} if group else {}),
                },
            }],
        },
        "op": op,
        "target": target,
        "matchType": match,
        "selectedQueryName": "A",
    }
    if extra_condition:
        cond.update(extra_condition)
    return {
        "alert": name,
        "alertType": alert_type,
        "ruleType": "threshold_rule",
        "description": desc,
        "evalWindow": evalw,
        "frequency": freq,
        "version": "v5",
        "condition": cond,
        "labels": {"severity": severity, **(labels or {})},
        "annotations": {"summary": summary, "description": detail},
        "preferredChannels": CHANNELS,
    }


def httpcheck(filt):
    return [{
        "metricName": "httpcheck.status",
        "temporality": "unspecified",
        "timeAggregation": "min",
        "spaceAggregation": "min",
    }], f"{filt} AND http.status_class = '2xx'"


rules = {}

# ---- one "X is down" rule per probed service -----------------------------
for key, (label, sev, why) in PROBES.items():
    aggs, filt = httpcheck(f"probe.service = '{key}'")
    rules[f"down-{key}"] = rule(
        f"{label} is down",
        alert_type="METRIC_BASED_ALERT", severity=sev,
        desc=f"{label} has failed its HTTP health check for 5 minutes straight. {why}",
        summary=f"{label} is not responding",
        detail=why,
        signal="metrics", aggregations=aggs, filt=filt,
        evalw="5m", freq="1m", op="2", target=1.0, match="2",
        labels={"service": key},
    )

# ---- debrid providers ----------------------------------------------------
for key, (label, sev) in PROVIDERS.items():
    aggs, filt = httpcheck(f"probe.service = '{key}'")
    rules[f"provider-{key}"] = rule(
        f"{label} is down",
        alert_type="METRIC_BASED_ALERT", severity=sev,
        desc=f"{label}'s public API has been unreachable for 10 minutes. Probed at the "
             f"provider directly, so this is the provider being down, not decypharr.",
        summary=f"{label} is unreachable",
        detail="Downloads and repairs against this provider will fail until it recovers.",
        signal="metrics", aggregations=aggs, filt=filt,
        evalw="10m", freq="5m", op="2", target=1.0, match="2",
        labels={"service": key, "provider": key},
    )

# ---- container health ----------------------------------------------------
# The name stays generic because the container varies; the summary names it.
rules["container-unhealthy"] = rule(
    "Container is UNHEALTHY (docker healthcheck failing)",
    alert_type="METRIC_BASED_ALERT", severity="warning",
    desc="A container has been failing its own docker healthcheck for 5 minutes. This is "
         "the gap httpcheck can't see: 27 containers define a healthcheck and the otel "
         "docker_stats receiver reports none of them.",
    summary="{{$labels.container}} is failing its healthcheck",
    detail="Check `docker ps` and the container's logs.",
    signal="metrics",
    aggregations=[{"metricName": "docker_container_healthy", "temporality": "unspecified",
                   "timeAggregation": "min", "spaceAggregation": "min"}],
    filt="", group=("container",),
    evalw="5m", freq="1m", op="2", target=1.0, match="2",
)

rules["container-restart-loop"] = rule(
    "Container is CRASH-LOOPING",
    alert_type="METRIC_BASED_ALERT", severity="critical",
    desc="Docker is restart-looping a container. restic_backup and restic_prune have been "
         "doing this unnoticed (their R2 credentials are missing from .env).",
    summary="{{$labels.container}} is crash-looping",
    detail="Docker keeps restarting it because it exits non-zero. Check its logs.",
    signal="metrics",
    aggregations=[{"metricName": "docker_container_restarting", "temporality": "unspecified",
                   "timeAggregation": "max", "spaceAggregation": "max"}],
    filt="", group=("container",),
    evalw="5m", freq="1m", op="1", target=0.0, match="2",
)

# ---- decypharr repair job ------------------------------------------------
rules["decypharr-repair-failing"] = rule(
    "decypharr repair job is FAILING",
    alert_type="LOGS_BASED_ALERT", severity="critical",
    desc="The repair job logged an error. Repair is what fixes broken symlinks when a "
         "debrid link dies; if it errors, broken media stays broken.",
    summary="decypharr repair job errored",
    detail="Repair runs every 6h. Check `docker logs decypharr | grep '\\[repair\\]'`.",
    signal="logs",
    aggregations=[{"expression": "count()"}],
    filt="service.name = 'decypharr' AND component = 'repair' AND severity_text = 'ERROR'",
    evalw="10m", freq="5m", op="1", target=0.0, match="1",
    labels={"service": "decypharr"},
)

rules["decypharr-repair-stalled"] = rule(
    "decypharr repair job has STOPPED RUNNING",
    alert_type="LOGS_BASED_ALERT", severity="warning",
    desc="No '[repair] Starting repair' line for 8 hours, though it's configured to run "
         "every 6h. A job that never starts logs no errors, so the failing-alert above "
         "cannot catch this — hence alertOnAbsent (absentFor is in MINUTES).",
    summary="decypharr repair has not run in 8h",
    detail="Check repair.enabled is still true in decypharr's config.json.",
    signal="logs",
    aggregations=[{"expression": "count()"}],
    filt="service.name = 'decypharr' AND component = 'repair' AND body CONTAINS 'Starting repair'",
    evalw="8h", freq="30m", op="2", target=1.0, match="1", step="30m",
    extra_condition={"alertOnAbsent": True, "absentFor": 480},
    labels={"service": "decypharr"},
)

# ---- host ----------------------------------------------------------------
rules["disk-nearly-full"] = rule(
    "Host disk is nearly full",
    alert_type="METRIC_BASED_ALERT", severity="warning",
    desc="Root filesystem over 90%. This box runs close to the edge and ClickHouse "
         "degrades badly on a full disk.",
    summary="Root filesystem over 90% full",
    detail="Free space with `docker system prune`, or lower retention in SigNoz.",
    signal="metrics",
    aggregations=[{"metricName": "system.filesystem.utilization", "temporality": "unspecified",
                   "timeAggregation": "avg", "spaceAggregation": "max"}],
    filt="mountpoint = '/'",
    evalw="10m", freq="10m", op="1", target=0.9, match="2", step="5m",
    labels={"service": "host"},
)

for old in OUT.glob("*.json"):
    old.unlink()
for name, r in rules.items():
    (OUT / f"{name}.json").write_text(json.dumps(r, indent=2) + "\n")
print(f"wrote {len(rules)} alert rules:")
for r in rules.values():
    print(f"  [{r['labels']['severity']:8s}] {r['alert']}")
