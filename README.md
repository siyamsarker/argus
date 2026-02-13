# Argus — The Hundred-Eyed Monitor

A lightweight health-monitoring daemon that watches Loki and Grafana instances and sends Discord alerts on state transitions. Named after Argus Panoptes, the hundred-eyed giant of Greek mythology who never sleeps.

## Features

- Polls Loki `/ready` and Grafana `/api/health` endpoints on a configurable interval
- Stateful alerting — notifies only on state transitions (healthy → unhealthy, unhealthy → healthy)
- Consecutive failure threshold to avoid false alarms from transient blips
- Discord webhook integration with embeds, retry logic, and rate-limit handling
- Runs as a systemd service with auto-restart on failure
- Structured logging to stdout (journald) and rotating log files

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/siyamsarker/argus.git /tmp/argus
cd /tmp/argus
cp .env.example .env
```

Edit `.env` with your actual values:

```
LOKI_URL=http://192.168.1.50:3100
GRAFANA_URL=http://192.168.1.50:3000
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### 2. Install

```bash
sudo bash install.sh
```

This will:

- Create a `argus` system user (no login shell)
- Copy files to `/opt/argus/`
- Create a Python virtual environment and install dependencies
- Install and start the systemd service

### 3. Verify

```bash
systemctl status argus
journalctl -u argus -f
```

You should see a startup notification in your Discord channel.

## Configuration Reference

All settings are loaded from `/opt/argus/.env` (or environment variables).

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOKI_URL` | Yes | — | Base URL of the Loki instance (e.g. `http://host:3100`) |
| `GRAFANA_URL` | Yes | — | Base URL of the Grafana instance (e.g. `http://host:3000`) |
| `DISCORD_WEBHOOK_URL` | Yes | — | Discord webhook URL for notifications |
| `CHECK_INTERVAL_SECONDS` | No | `120` | Polling interval in seconds |
| `FAILURE_THRESHOLD` | No | `2` | Consecutive failures before alerting |
| `REQUEST_TIMEOUT_SECONDS` | No | `10` | HTTP timeout for health checks |
| `LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

## Managing the Service

```bash
# Check status
systemctl status argus

# View live logs
journalctl -u argus -f

# Restart after config change
sudo systemctl restart argus

# Stop
sudo systemctl stop argus

# Disable (prevent start on boot)
sudo systemctl disable argus
```

## Log Files

- **journald**: `journalctl -u argus`
- **File**: `/var/log/argus/argus.log` (10 MB max, 5 rotated backups)

## Testing

### Trigger an alert with an intentionally wrong URL

Edit `/opt/argus/.env` and set an invalid Loki URL:

```
LOKI_URL=http://127.0.0.1:9999
```

Restart the service:

```bash
sudo systemctl restart argus
```

After `FAILURE_THRESHOLD` consecutive failures (default: 2 checks), you should receive a red alert embed in Discord. Restore the correct URL and restart to see a green recovery notification.

### Verify logs

```bash
# Live journal output
journalctl -u argus -f

# Log file
tail -f /var/log/argus/argus.log

# Debug-level output (shows every check result)
# Set LOG_LEVEL=DEBUG in .env, then restart
```

### Manually send a test Discord notification

You can test your webhook independently with curl:

```bash
curl -H "Content-Type: application/json" \
     -d '{"embeds":[{"title":"Test","description":"Argus webhook test","color":3447003}]}' \
     "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
```

### Run directly (without systemd)

For development or quick testing:

```bash
cd /opt/argus
source venv/bin/activate
python argus.py
```

Press `Ctrl+C` for graceful shutdown.

## Uninstall

```bash
sudo systemctl stop argus
sudo systemctl disable argus
sudo rm /etc/systemd/system/argus.service
sudo systemctl daemon-reload
sudo userdel argus
sudo rm -rf /opt/argus /var/log/argus
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `journalctl -u argus -e` — look for `FATAL:` config errors |
| No Discord notifications | Verify `DISCORD_WEBHOOK_URL` with the curl test above |
| Too many alerts | Increase `FAILURE_THRESHOLD` to require more consecutive failures |
| Alerts are delayed | Decrease `CHECK_INTERVAL_SECONDS` for faster detection |
| Permission denied on log file | Ensure `/var/log/argus/` is owned by the `argus` user |
| Python version error | Argus requires Python 3.10+; check with `python3 --version` |

## Architecture

Argus is intentionally simple — a single Python script in a `while True` loop:

1. Poll Loki `/ready` and Grafana `/api/health`
2. Track consecutive failures per service
3. On state transition → send Discord embed (red alert or green recovery)
4. Sleep for `CHECK_INTERVAL_SECONDS`
5. Repeat

systemd handles process supervision (auto-restart on crash). Signal handlers (`SIGTERM`, `SIGINT`) enable graceful shutdown.

## Contributing

Contributions are welcome. Please open an issue to discuss proposed changes before submitting a pull request.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
