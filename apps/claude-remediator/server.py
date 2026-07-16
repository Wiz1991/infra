#!/usr/bin/env python3
"""claude-remediator — SigNoz webhook receiver that spawns a Claude Code session.

SigNoz alert (webhook channel "claude-remediator") -> POST /alert?token=...
-> headless `claude -p` run with apps/claude-remediator/prompt.md as the
runbook and settings.json as the permission policy. The session investigates
the firing alert (dead debrid links, crashed arr queues), applies only the
narrow fixes the runbook allows, and reports its outcome via
`logger -t claude-remediator`, which the otel syslog pipeline ships to SigNoz
as its own service — so every run, resolved or not, is queryable next to the
alert that triggered it.

Guardrails live in three layers, resist weakening any of them:
  1. prompt.md    — what the session is allowed to *want* to do
  2. settings.json — what the session is allowed to *actually* do (allowlist;
                     deny rules for hardlinks/library paths win over allows)
  3. this server  — cooldown per alert, one run at a time, daily cap, timeout

Stdlib only. Config via environment (see claude-remediator.service):
  REMEDIATOR_TOKEN      shared secret, required in the webhook URL
  REMEDIATOR_PORT       default 8484
  REMEDIATOR_MODEL      default sonnet
  REMEDIATOR_COOLDOWN   seconds per alertname between runs, default 2700
  REMEDIATOR_DAILY_CAP  max runs per UTC day, default 8
  REMEDIATOR_TIMEOUT    seconds before the claude run is killed, default 1200
  REMEDIATOR_DRY_RUN    "1" = log what would run, don't spawn claude
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LOG_DIR = Path.home() / ".local" / "state" / "claude-remediator"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get("REMEDIATOR_TOKEN", "")
PORT = int(os.environ.get("REMEDIATOR_PORT", "8484"))
MODEL = os.environ.get("REMEDIATOR_MODEL", "sonnet")
COOLDOWN = int(os.environ.get("REMEDIATOR_COOLDOWN", "2700"))
DAILY_CAP = int(os.environ.get("REMEDIATOR_DAILY_CAP", "8"))
TIMEOUT = int(os.environ.get("REMEDIATOR_TIMEOUT", "1200"))
DRY_RUN = os.environ.get("REMEDIATOR_DRY_RUN", "") == "1"

# Only these alerts get a session. Anything else (down-checks, disk, crash
# loops) needs a human or a container restart, not an LLM poking at torrents.
HANDLED_ALERTS = {
    "Sonarr queue processing is CRASHING (imports frozen)",
    "Radarr queue processing is CRASHING (imports frozen)",
    "Sonarr cannot import completed downloads (dead debrid links)",
    "Radarr cannot import completed downloads (dead debrid links)",
    "decypharr is serving DEAD debrid links",
}

_lock = threading.Lock()          # one claude run at a time
_last_run: dict[str, float] = {}  # alertname -> monotonic timestamp
_runs_today: list[str] = []       # UTC date strings, one per run


def syslog(msg: str) -> None:
    """Ship a line to SigNoz via the host syslog pipeline (and journald)."""
    subprocess.run(["logger", "-t", "claude-remediator", msg], check=False)


def spawn(alertname: str, alerts: list[dict]) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _runs_today[:] = [d for d in _runs_today if d == today]
    if len(_runs_today) >= DAILY_CAP:
        syslog(f'{{"event":"skipped","reason":"daily cap {DAILY_CAP} reached",'
               f'"alert":{json.dumps(alertname)}}}')
        return
    _runs_today.append(today)

    context = json.dumps(
        [{"labels": a.get("labels", {}), "annotations": a.get("annotations", {}),
          "startsAt": a.get("startsAt", "")} for a in alerts],
        indent=2)
    prompt = (HERE / "prompt.md").read_text().replace("{{ALERT_CONTEXT}}", context)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "-" for c in alertname)[:60]
    out_path = LOG_DIR / f"{stamp}-{slug}.log"

    # stream-json + verbose = full audit trail: every tool call the session
    # makes is one JSON line in the log file, not just its final message.
    cmd = [
        "claude", "-p", prompt,
        "--settings", str(HERE / "settings.json"),
        "--model", MODEL,
        "--max-turns", "120",
        "--verbose", "--output-format", "stream-json",
    ]
    syslog(f'{{"event":"started","alert":{json.dumps(alertname)},'
           f'"log":{json.dumps(str(out_path))},"dry_run":{str(DRY_RUN).lower()}}}')
    if DRY_RUN:
        out_path.write_text(f"DRY RUN — would exec:\n{cmd}\n\nprompt:\n{prompt}\n")
        return

    # Transient API errors (529 Overloaded, 5xx) kill a run before it does any
    # work — retry a couple of times. Remediation actions are idempotent
    # (deleting a gone torrent / repointing an already-repointed symlink are
    # no-ops), so a retry after a mid-run failure is safe.
    status = "unknown"
    for attempt in range(3):
        if attempt:
            time.sleep(90)
            syslog(f'{{"event":"retry","attempt":{attempt + 1},'
                   f'"alert":{json.dumps(alertname)}}}')
        try:
            with out_path.open("a") as out:
                proc = subprocess.run(
                    cmd, cwd=str(REPO), stdout=out, stderr=subprocess.STDOUT,
                    timeout=TIMEOUT,
                    env={**os.environ, "CLAUDE_CODE_DISABLE_AUTOUPDATE": "1"},
                )
            status = "finished" if proc.returncode == 0 else f"exited {proc.returncode}"
            if proc.returncode == 0:
                break
        except subprocess.TimeoutExpired:
            status = f"killed after {TIMEOUT}s"
            break  # a timeout burned real work-time; don't triple the cost
        except FileNotFoundError:
            status = "claude binary not found"
            break
    # Belt-and-braces: even if the session forgot its own note, ship its final
    # message to SigNoz so every run leaves something queryable.
    final = ""
    try:
        for line in reversed(out_path.read_text().splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "result":
                final = (rec.get("result") or "")[:500]
                break
    except OSError:
        pass
    syslog(f'{{"event":"run_done","alert":{json.dumps(alertname)},'
           f'"status":{json.dumps(status)},"log":{json.dumps(str(out_path))},'
           f'"final":{json.dumps(final)}}}')


def handle_payload(payload: dict) -> list[str]:
    """Group firing alerts by alertname, spawn a run per eligible name."""
    triggered = []
    by_name: dict[str, list[dict]] = {}
    for a in payload.get("alerts", []):
        if a.get("status", "firing") != "firing":
            continue
        name = a.get("labels", {}).get("alertname", "")
        by_name.setdefault(name, []).append(a)

    for name, alerts in by_name.items():
        if name not in HANDLED_ALERTS:
            syslog(f'{{"event":"ignored","reason":"not a handled alert",'
                   f'"alert":{json.dumps(name)}}}')
            continue
        now = time.monotonic()
        if now - _last_run.get(name, -COOLDOWN) < COOLDOWN:
            syslog(f'{{"event":"skipped","reason":"cooldown",'
                   f'"alert":{json.dumps(name)}}}')
            continue
        _last_run[name] = now
        triggered.append(name)
        threading.Thread(
            target=lambda n=name, al=alerts: _locked_spawn(n, al),
            daemon=True).start()
    return triggered


def _locked_spawn(name: str, alerts: list[dict]) -> None:
    with _lock:
        spawn(name, alerts)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/alert":
            self.send_error(404)
            return
        token = parse_qs(url.query).get("token", [""])[0]
        if not TOKEN or token != TOKEN:
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        triggered = handle_payload(payload)
        body = json.dumps({"triggered": triggered}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):  # journald gets it via stdout
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("REMEDIATOR_TOKEN is not set")
    print(f"claude-remediator listening on :{PORT} "
          f"(model={MODEL}, dry_run={DRY_RUN})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
