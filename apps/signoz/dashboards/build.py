#!/usr/bin/env python3
"""Generate the SigNoz dashboard JSON in this directory.

The dashboards are generated rather than hand-written because SigNoz's widget
schema is verbose and repetitive (every panel needs a UUID, a layout cell, and a
fully-specified builder query). Editing 200 lines of JSON by hand to move one
panel is how dashboards rot. Change this file and re-run it:

    python3 apps/signoz/dashboards/build.py

then apply with scripts/signoz-setup.sh. Panel IDs are derived from the dashboard
and panel title, so regenerating is stable — it produces the same JSON, and
re-applying updates the existing dashboard instead of spawning a duplicate.

Metric notes worth knowing before you add a panel:
  * container.cpu.utilization is a 0-1 FRACTION of total host CPU, not a percent
    — it renders with yAxisUnit "percentunit". Using "percent" reads ~100x low.
  * there is no system.cpu.utilization from this collector build; host CPU is
    rate(system.cpu.time) filtered to non-idle states, which yields "cores busy".
  * httpcheck.status emits one series per status class; an up-check must filter
    http.status_class = '2xx' or it is meaningless.
"""
import hashlib
import json
import pathlib

OUT = pathlib.Path(__file__).parent


def uid(*parts):
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:24]


def q(name, metric, *, agg="avg", space="max", temporality="unspecified",
      filt="", group=(), legend="", expr_name="A"):
    return {
        "aggregations": [{
            "metricName": metric,
            "temporality": temporality,
            "timeAggregation": agg,
            "spaceAggregation": space,
            "reduceTo": "avg",
        }],
        "dataSource": "metrics",
        "disabled": False,
        "expression": expr_name,
        "filter": {"expression": filt},
        "groupBy": [
            {"key": g, "dataType": "string", "type": "tag",
             "isColumn": False, "isJSON": False,
             "id": f"{g}--string--tag--false"}
            for g in group
        ],
        "having": {"expression": ""},
        "legend": legend,
        "limit": None,
        "orderBy": [],
        "queryName": expr_name,
        "stepInterval": 60,
    }


def qlogs(*, filt="", group=(), legend="", expr_name="A"):
    return {
        "aggregations": [{"expression": "count()"}],
        "dataSource": "logs",
        "disabled": False,
        "expression": expr_name,
        "filter": {"expression": filt},
        "groupBy": [
            {"key": g, "dataType": "string", "type": "tag",
             "isColumn": False, "isJSON": False,
             "id": f"{g}--string--tag--false"}
            for g in group
        ],
        "having": {"expression": ""},
        "legend": legend,
        "limit": None,
        "orderBy": [],
        "queryName": expr_name,
        "stepInterval": 60,
    }


def widget(dash, title, queries, *, panel="graph", unit="", desc=""):
    return {
        "id": uid(dash, title),
        "title": title,
        "description": desc,
        "panelTypes": panel,
        "yAxisUnit": unit,
        "fillSpans": False,
        "isStacked": False,
        "nullZeroValues": "zero",
        "opacity": "1",
        "softMax": None,
        "softMin": None,
        "thresholds": [],
        "timePreferance": "GLOBAL_TIME",
        "query": {
            "queryType": "builder",
            "builder": {"queryData": queries, "queryFormulas": []},
            "clickhouse_sql": [{"name": "A", "legend": "", "disabled": False, "query": ""}],
            "promql": [{"name": "A", "query": "", "legend": "", "disabled": False}],
            "id": uid(dash, title, "query"),
        },
    }


def dashboard(title, description, tags, widgets, cols=2):
    """Lay widgets out left-to-right, `cols` per row."""
    layout = []
    for i, w in enumerate(widgets):
        w_ = 12 // cols
        layout.append({
            "i": w["id"], "x": (i % cols) * w_, "y": (i // cols) * 3,
            "w": w_, "h": 3, "moved": False, "static": False,
        })
    return {
        "title": title,
        "name": title,
        "description": description,
        "tags": tags,
        "layout": layout,
        "widgets": widgets,
        "variables": {},
        "version": "v4",
    }


# ---------------------------------------------------------------- Plex
plex = "Plex"
plex_widgets = [
    widget(plex, "Plex up", [q(
        "up", "httpcheck.status", agg="min", space="min",
        filt="probe.service = 'plex' AND http.status_class = '2xx'",
        legend="plex")], panel="value",
        desc="1 = answering /identity. Filtered to the 2xx class: httpcheck emits a series per status class."),
    widget(plex, "Active streams", [q(
        "streams", "tautulli_streams", agg="latest", space="max", legend="streams")],
        panel="value", desc="From Tautulli."),
    widget(plex, "Streams by transcode decision", [q(
        "dec", "tautulli_streams_by_decision", agg="latest", space="max",
        group=("decision",), legend="{{decision}}")],
        desc="Transcoding is what actually loads the box — direct play is nearly free."),
    widget(plex, "Streams by user", [q(
        "user", "tautulli_streams_by_user", agg="latest", space="max",
        group=("user",), legend="{{user}}")]),
    widget(plex, "Playback bandwidth", [q(
        "bw", "tautulli_bandwidth_bits_per_second", agg="latest", space="max",
        group=("scope",), legend="{{scope}}")], unit="bps"),
    widget(plex, "Plex CPU (host, as Plex sees it)", [q(
        "cpu", "plex_host_cpu_util", agg="avg", space="max", legend="cpu %")], unit="percent"),
    widget(plex, "Library size on disk", [q(
        "lib", "plex_library_storage_total", agg="latest", space="max",
        group=("library",), legend="{{library}}")], unit="bytes"),
    widget(plex, "Items per library", [q(
        "items", "tautulli_library_items", agg="latest", space="max",
        group=("library",), legend="{{library}}")]),
    widget(plex, "Plex errors & warnings", [qlogs(
        filt="service.name = 'plex' AND severity_text IN ('ERROR','WARN')",
        group=("severity_text",), legend="{{severity_text}}")],
        desc="Plex's own log file. DEBUG is dropped at the collector."),
    widget(plex, "Plex memory", [q(
        "mem", "container.memory.usage.total", agg="avg", space="max",
        filt="service.name = 'plex'", legend="plex")], unit="bytes"),

    # --- viewing history, from Tautulli ---------------------------------
    # These are not live activity: they are Tautulli's record of what has
    # actually been watched, which predates this stack by months. The _daily
    # series are backfilled with real past timestamps (scripts/tautulli-backfill.py);
    # the rolling windows are republished by the exporter every scrape, so they
    # cover the full history regardless of the metrics TTL.
    widget(plex, "Plays per day", [q(
        "pd", "tautulli_plays_daily", agg="sum", space="sum",
        group=("media_type",), legend="{{media_type}}")],
        desc="Backfilled from Tautulli. Limited to the metrics retention window."),
    widget(plex, "Watch time per day", [q(
        "wd", "tautulli_watch_seconds_daily", agg="sum", space="sum",
        legend="watch time")], unit="s"),
    widget(plex, "Plays — 24h / 7d / 30d / all time", [q(
        "hp", "tautulli_history_plays", agg="latest", space="max",
        group=("window",), legend="{{window}}")],
        desc="Rolling windows over Tautulli's whole history — unaffected by retention."),
    widget(plex, "Watch time by user (all time)", [q(
        "wu", "tautulli_history_watch_seconds_by_user", agg="latest", space="max",
        group=("user",), legend="{{user}}")], unit="s"),
    widget(plex, "Plays by user (all time)", [q(
        "pu", "tautulli_history_plays_by_user", agg="latest", space="max",
        group=("user",), legend="{{user}}")]),
    widget(plex, "Direct play vs transcode (all time)", [q(
        "dp", "tautulli_history_plays_by_decision", agg="latest", space="max",
        group=("decision",), legend="{{decision}}")],
        desc="Transcoding is what actually costs CPU."),
]

# ------------------------------------------------------- debrid / decypharr
deb = "Debrid & decypharr"
deb_widgets = [
    widget(deb, "Debrid providers up", [q(
        "prov", "httpcheck.status", agg="min", space="min",
        filt="probe.kind = 'provider' AND http.status_class = '2xx'",
        group=("probe.service",), legend="{{probe.service}}")],
        desc="Probed at the provider's own API, so an outage here is the provider, not decypharr."),
    widget(deb, "decypharr up", [q(
        "dc", "httpcheck.status", agg="min", space="min",
        filt="probe.service = 'decypharr' AND http.status_class = '2xx'",
        legend="decypharr")], panel="value"),
    widget(deb, "REPAIR JOB — errors", [qlogs(
        filt="service.name = 'decypharr' AND component = 'repair' AND severity_text = 'ERROR'",
        legend="repair errors")],
        desc="Non-zero means repair is failing and broken symlinks are not being fixed."),
    widget(deb, "REPAIR JOB — runs started", [qlogs(
        filt="service.name = 'decypharr' AND component = 'repair' AND body CONTAINS 'Starting repair'",
        legend="runs")],
        desc="Repair is configured to run every 6h. A flat zero line means it stopped firing."),
    widget(deb, "decypharr log volume by component", [qlogs(
        filt="service.name = 'decypharr'", group=("component",), legend="{{component}}")]),
    widget(deb, "decypharr errors by component", [qlogs(
        filt="service.name = 'decypharr' AND severity_text = 'ERROR'",
        group=("component",), legend="{{component}}")]),
    widget(deb, "Activity by debrid provider", [qlogs(
        filt="service.name = 'decypharr' AND debrid EXISTS",
        group=("debrid",), legend="{{debrid}}")],
        desc="Parsed from decypharr's inline Debrid= tag."),
    widget(deb, "Activity by *arr", [qlogs(
        filt="service.name = 'decypharr' AND arr EXISTS",
        group=("arr",), legend="{{arr}}")]),
    widget(deb, "decypharr CPU", [q(
        "cpu", "container.cpu.utilization", agg="avg", space="max",
        filt="service.name = 'decypharr'", legend="decypharr")], unit="percentunit"),
    widget(deb, "decypharr memory", [q(
        "mem", "container.memory.usage.total", agg="avg", space="max",
        filt="service.name = 'decypharr'", legend="decypharr")], unit="bytes"),
]

# ------------------------------------------------------------- sonarr/radarr
arr = "Sonarr & Radarr"
arr_widgets = [
    widget(arr, "Queue", [
        q("qs", "sonarr_queue_total", agg="latest", space="max", legend="sonarr", expr_name="A"),
        q("qr", "radarr_queue_total", agg="latest", space="max", legend="radarr", expr_name="B"),
    ]),
    widget(arr, "Missing", [
        q("ms", "sonarr_episode_missing_total", agg="latest", space="max", legend="sonarr episodes", expr_name="A"),
        q("mr", "radarr_movie_missing_total", agg="latest", space="max", legend="radarr movies", expr_name="B"),
    ]),
    widget(arr, "Wanted / monitored", [
        q("wm", "radarr_movie_wanted_total", agg="latest", space="max", legend="radarr wanted", expr_name="A"),
        q("sm", "sonarr_series_monitored_total", agg="latest", space="max", legend="sonarr monitored series", expr_name="B"),
    ]),
    widget(arr, "System health issues", [
        q("hr", "radarr_system_health_issues", agg="latest", space="max", legend="radarr", expr_name="A"),
    ], desc="Non-zero means the *arr app is complaining in its own health page."),
    widget(arr, "Errors by subsystem", [qlogs(
        filt="service.name IN ('sonarr','radarr') AND severity_text = 'ERROR'",
        group=("logger",), legend="{{logger}}")],
        desc="`logger` is the *arr class that logged it — import, indexer, HTTP client, …"),
    widget(arr, "Log volume by service & level", [qlogs(
        filt="service.name IN ('sonarr','radarr')",
        group=("service.name", "severity_text"), legend="{{service.name}} {{severity_text}}")]),
    widget(arr, "Root folder free space", [
        q("fs", "sonarr_rootfolder_freespace_bytes", agg="latest", space="min", legend="sonarr", expr_name="A"),
        q("fr", "radarr_rootfolder_freespace_bytes", agg="latest", space="min", legend="radarr", expr_name="B"),
    ], unit="bytes"),
    widget(arr, "CPU", [q(
        "cpu", "container.cpu.utilization", agg="avg", space="max",
        filt="service.name IN ('sonarr','radarr')",
        group=("service.name",), legend="{{service.name}}")], unit="percentunit"),
]

# -------------------------------------------------------------- infra / host
inf = "Containers & Host"
inf_widgets = [
    widget(inf, "Host CPU (cores busy)", [q(
        "cpu", "system.cpu.time", agg="rate", space="sum",
        temporality="cumulative", filt="state != 'idle'", legend="cores busy")],
        desc="This collector emits no system.cpu.utilization, so CPU is rate(system.cpu.time) over non-idle states. 6 cores on this box."),
    widget(inf, "Host memory", [q(
        "mem", "system.memory.utilization", agg="avg", space="max",
        filt="state = 'used'", legend="used")], unit="percentunit"),
    widget(inf, "Root disk usage", [q(
        "disk", "system.filesystem.utilization", agg="avg", space="max",
        filt="mountpoint = '/'", legend="/")], unit="percentunit",
        desc="ClickHouse degrades badly on a full disk — this box runs close to the edge."),
    widget(inf, "Load average", [q(
        "load", "system.cpu.load_average.5m", agg="avg", space="max", legend="5m")]),
    widget(inf, "CPU by service", [q(
        "c", "container.cpu.utilization", agg="avg", space="max",
        group=("service.name",), legend="{{service.name}}")], unit="percentunit",
        desc="Fraction of TOTAL host CPU (0-1), not percent of one core."),
    widget(inf, "Memory by service", [q(
        "m", "container.memory.usage.total", agg="avg", space="max",
        group=("service.name",), legend="{{service.name}}")], unit="bytes"),
    widget(inf, "Container restarts", [q(
        "r", "container.restarts", agg="increase", space="max",
        temporality="cumulative", group=("service.name",), legend="{{service.name}}")],
        desc="A climbing line is a crash-loop."),
    widget(inf, "Everything up?", [q(
        "up", "httpcheck.status", agg="min", space="min",
        filt="http.status_class = '2xx'",
        group=("probe.service",), legend="{{probe.service}}")],
        desc="1 = up. Every probed service, including the debrid providers."),
    widget(inf, "Network by service", [q(
        "rx", "container.network.io.usage.rx_bytes", agg="rate", space="max",
        temporality="cumulative", group=("service.name",), legend="{{service.name}} rx")],
        unit="binBps"),
    widget(inf, "Errors across all logging services", [qlogs(
        filt="severity_text = 'ERROR'", group=("service.name",), legend="{{service.name}}")],
        desc="Log collection is opt-in (otel.logs=stdout), so this covers sonarr, radarr and decypharr, plus plex from its own log file."),
]

for name, d in [
    ("plex", dashboard(plex, "Plex, its streams, libraries and errors.", ["plex", "media"], plex_widgets)),
    ("debrid", dashboard(deb, "decypharr, the repair job, and the debrid providers.", ["debrid", "decypharr"], deb_widgets)),
    ("arr", dashboard(arr, "Sonarr and Radarr queues, health and errors.", ["arr", "automation"], arr_widgets)),
    ("infra", dashboard(inf, "Per-container and host resources, uptime, errors.", ["infra"], inf_widgets)),
]:
    p = OUT / f"{name}.json"
    p.write_text(json.dumps(d, indent=2) + "\n")
    print(f"wrote {p.name}: {len(d['widgets'])} panels")
