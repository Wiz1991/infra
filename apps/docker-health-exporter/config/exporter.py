#!/usr/bin/env python3
"""Prometheus exporter for Docker container health.

The otel docker_stats receiver reports CPU, memory and restart counts but NOT
healthcheck state — so a container can be failing its own healthcheck, or sitting
in a restart loop, and nothing in the telemetry says so. (restic_backup was doing
exactly that, unnoticed, for the entire time this stack has been running.)

This reads the Docker socket and publishes the missing signals:

  docker_container_up          1 if running
  docker_container_restarting  1 if docker is restart-looping it
  docker_container_healthy     1 healthy / 0 unhealthy — only for containers that
                               actually define a healthcheck
  docker_container_oneshot     1 for containers designed to exit (restart policy
                               "no"/"on-failure"), so alerts can ignore them

Standard library only — no docker SDK, nothing to build.

Env:
  DOCKER_SOCKET   default /var/run/docker.sock
  POLL_INTERVAL   seconds (default 30)
  PORT            listen port (default 9488)
"""
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "9488"))

_metrics = "# docker health exporter starting\n"
_lock = threading.Lock()


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a unix socket — the Docker API speaks plain HTTP."""

    def __init__(self, path):
        super().__init__("localhost")
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self.unix_path)
        self.sock = s


def docker(path):
    conn = UnixHTTPConnection(SOCKET)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def collect():
    out = []
    containers = docker("/containers/json?all=1")

    up, restarting, healthy, oneshot = [], [], [], []
    for c in containers:
        cid = c["Id"]
        try:
            info = docker(f"/containers/{cid}/json")
        except Exception:
            continue

        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        labels = c.get("Labels") or {}
        # Reuse the same service identity the logs and metrics carry, so a
        # container's health lines up with everything else about it.
        svc = labels.get("service.name", name)
        ns = labels.get("service.namespace", "")
        lbl = f'container="{_esc(name)}",service_name="{_esc(svc)}",service_namespace="{_esc(ns)}"'

        state = info.get("State") or {}
        policy = ((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")

        up.append(f'docker_container_up{{{lbl}}} {1 if state.get("Running") else 0}')
        restarting.append(
            f'docker_container_restarting{{{lbl}}} {1 if state.get("Restarting") else 0}'
        )
        # A one-shot (schema migrator, init job) is *supposed* to exit; alerting
        # on it being down would be noise.
        oneshot.append(
            f'docker_container_oneshot{{{lbl}}} '
            f'{1 if policy in ("", "no", "on-failure") else 0}'
        )

        health = (state.get("Health") or {}).get("Status")
        if health:  # absent when the image defines no healthcheck
            # "starting" is neither healthy nor failing — don't report it as
            # unhealthy or every deploy would page.
            if health in ("healthy", "unhealthy"):
                healthy.append(
                    f'docker_container_healthy{{{lbl}}} {1 if health == "healthy" else 0}'
                )

    def emit(name, help_text, samples):
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.extend(samples)

    emit("docker_container_up", "1 if the container is running", up)
    emit("docker_container_restarting", "1 if docker is restart-looping it", restarting)
    emit("docker_container_healthy", "1 healthy, 0 unhealthy (healthcheck only)", healthy)
    emit("docker_container_oneshot", "1 if the container is meant to exit", oneshot)
    return "\n".join(out) + "\n"


def poll_forever():
    global _metrics
    while True:
        try:
            text = collect()
        except Exception as e:
            text = f"docker_health_exporter_up 0\n# error: {str(e)[:200]}\n"
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

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_forever, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
