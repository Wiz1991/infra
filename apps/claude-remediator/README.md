# claude-remediator

SigNoz alert → webhook → this receiver → headless Claude Code session that
investigates and (narrowly) fixes arr/debrid pipeline failures.

```
SigNoz alertmanager ──POST /alert?token=…──▶ server.py (host, :8484)
                                               │ cooldown / daily cap / 1-at-a-time
                                               ▼
                                    claude -p prompt.md --settings settings.json
                                               │
                          fixes dead torrents / repoints symlinks / kicks arrs
                                               ▼
                       logger -t claude-remediator '{"event":"remediation",…}'
                            (host syslog → otel → SigNoz service "claude-remediator")
```

## What it will do

Only the failure modes encoded in `prompt.md` (with read-only SigNoz MCP
access for log/metric history during investigation):

- delete nameless/errored torrents in decypharr that crash Sonarr/Radarr's
  QueueService (imports frozen)
- verify dead debrid links by *reading* them, repoint symlinks to a healthy
  provider copy, or delete the single dead torrent and trigger a re-grab
- otherwise: investigate read-only and leave a note

## What it will never do

Enforced by `settings.json` deny rules (which beat allows), not just prose:
no writes under `/mnt/media/hardlinks`, no container lifecycle (`docker
rm/stop/restart/compose/exec`), no package/image updates, no git commits or
pushes, no repo file edits, no sudo. It can delete at most 5 torrents per run
(prompt rule) and runs at most `REMEDIATOR_DAILY_CAP` (8) sessions/day with a
45-min per-alert cooldown (server enforced).

## Outcome notes in SigNoz

Every run ends with a JSON line under service **`claude-remediator`** in the
SigNoz logs — `{"event":"remediation","resolved":…,"actions":…,"unresolved":…}`.
Query it next to the alert that fired. Full transcripts land in
`~/.local/state/claude-remediator/`.

## Wiring

- Alerts: the five arr/debrid rules in `apps/signoz/alerts/build.py` carry
  `preferredChannels: ["default", "claude-remediator"]`.
- Channel: SigNoz webhook channel `claude-remediator` →
  `http://10.0.1.1:8484/alert?token=$REMEDIATOR_TOKEN` (docker gateway → host).
- Secret: `REMEDIATOR_TOKEN` in `/opt/gitops/.env`.
- Service: see header of `claude-remediator.service` for install commands.

## Testing

```
# receiver only (no claude spawn):
systemctl --user stop claude-remediator
REMEDIATOR_DRY_RUN=1 REMEDIATOR_TOKEN=test python3 server.py &
curl -s -X POST 'localhost:8484/alert?token=test' -d '{"alerts":[{"status":"firing","labels":{"alertname":"decypharr is serving DEAD debrid links"}}]}'
```
