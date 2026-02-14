<p align="center">
  <h1 align="center">Argus</h1>
  <p align="center"><strong>The Hundred-Eyed Monitor</strong></p>
  <p align="center">
    A lightweight health-monitoring daemon that watches one or more Loki and Grafana instances<br>
    and sends Discord alerts when things go wrong.
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/platform-Ubuntu%20%7C%20Debian-orange" alt="Platform">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/status-production--ready-brightgreen" alt="Status">
  </p>
</p>

---

Named after **Argus Panoptes**, the hundred-eyed giant of Greek mythology who never sleeps -- just like this daemon.

Argus polls your Loki and Grafana health endpoints every 2 minutes, tracks consecutive failures to avoid false alarms, and sends rich Discord notifications only when a service actually goes down or comes back up. You can monitor a single instance of each, or scale to multiple instances using comma-separated URLs.

## Features

- **Loki monitoring** -- polls `/ready`, expects HTTP 200 with `ready` in the response body
- **Grafana monitoring** -- polls `/api/health`, expects HTTP 200 with `"database": "ok"` in JSON
- **Multi-instance support** -- monitor one or many Loki/Grafana instances via comma-separated URLs
- **Stateful alerting** -- notifies only on state transitions, never spams on repeated failures
- **Failure threshold** -- configurable consecutive failure count before declaring a service unhealthy (default: 2)
- **Recovery notifications** -- alerts when a service comes back online
- **Discord embeds** -- color-coded messages (red for alerts, green for recovery, blue for startup)
- **Retry with backoff** -- 3 attempts with exponential backoff on webhook failures
- **Rate-limit aware** -- handles Discord 429 responses using `Retry-After`
- **Graceful shutdown** -- handles `SIGTERM` and `SIGINT` for clean systemd stops
- **Rotating log files** -- 10 MB max, 5 backups at `/var/log/argus/argus.log`
- **Systemd integration** -- auto-start on boot, auto-restart on crash
- **Security hardened** -- runs as a dedicated non-root user with systemd sandboxing

## Project Structure

```
argus/
├── argus.py           # Main daemon script
├── argus.service      # systemd unit file
├── install.sh         # Automated setup script
├── .env.example       # Configuration template
├── requirements.txt   # Pinned Python dependencies
├── LICENSE            # MIT License
└── README.md
```

## Prerequisites

- **Ubuntu / Debian** server (or any systemd-based Linux distribution)
- **Python 3.10+**
- A **Discord webhook URL** ([how to create one](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks))
- Running **Loki** and **Grafana** instance(s) with accessible health endpoints

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/siyamsarker/argus.git
cd argus
cp .env.example .env
```

Edit `.env` with your actual values:

```bash
# Single instance
LOKI_URL=http://192.168.1.50:3100
GRAFANA_URL=http://192.168.1.50:3000
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN

# Or multiple instances (comma-separated)
# LOKI_URL=http://192.168.1.50:3100,http://192.168.1.51:3100
# GRAFANA_URL=http://192.168.1.50:3000,http://192.168.1.51:3000
```

### 2. Install

```bash
sudo bash install.sh
```

The install script will:

- Create an `argus` system user (no login shell)
- Set up `/opt/argus/` with a Python virtual environment
- Install pinned dependencies (`requests`, `python-dotenv`)
- Copy the systemd unit file, enable, and start the service
- Create `/var/log/argus/` with proper ownership

### 3. Verify

```bash
systemctl status argus
journalctl -u argus -f
```

On successful startup, Argus sends a blue "Argus is now watching" notification to your Discord channel.

## Configuration

All settings are loaded from `/opt/argus/.env` (or environment variables). The `.env` file is set to `chmod 600` for security.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOKI_URL` | Yes | -- | Loki URL(s), comma-separated for multiple (e.g. `http://host:3100,http://host2:3100`) |
| `GRAFANA_URL` | Yes | -- | Grafana URL(s), comma-separated for multiple (e.g. `http://host:3000,http://host2:3000`) |
| `DISCORD_WEBHOOK_URL` | Yes | -- | Discord webhook URL for notifications |
| `CHECK_INTERVAL_SECONDS` | No | `120` | Polling interval in seconds |
| `FAILURE_THRESHOLD` | No | `2` | Consecutive failures before alerting |
| `REQUEST_TIMEOUT_SECONDS` | No | `10` | HTTP timeout per health check |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

When monitoring multiple instances of the same service, each instance is tracked independently with its own failure counter and state. Discord alerts identify the specific instance by its `host:port`.

After changing configuration, restart the service:

```bash
sudo systemctl restart argus
```

## Discord Notifications

Argus sends three types of Discord embeds:

| Type | Color | When |
|---|---|---|
| **Startup** | Blue | Argus starts successfully |
| **Alert** | Red | A service transitions from healthy to unhealthy |
| **Recovery** | Green | A service transitions from unhealthy back to healthy |

Every embed includes: service name, status, timestamp (UTC), hostname, and failure reason (for alerts).

## Managing the Service

```bash
# Check status
systemctl status argus

# View live logs
journalctl -u argus -f

# Restart
sudo systemctl restart argus

# Stop
sudo systemctl stop argus

# Disable auto-start on boot
sudo systemctl disable argus
```

## Logs

| Destination | Location |
|---|---|
| journald | `journalctl -u argus` |
| Log file | `/var/log/argus/argus.log` (10 MB, 5 rotated backups) |

Set `LOG_LEVEL=DEBUG` in `.env` to see every individual health check result.

## Testing

### Trigger an alert with a wrong URL

Set an unreachable Loki URL to simulate a failure:

```bash
# Edit /opt/argus/.env
LOKI_URL=http://127.0.0.1:9999
```

```bash
sudo systemctl restart argus
```

After 2 consecutive failures (default threshold), a red alert embed appears in Discord. Restore the correct URL and restart to see a green recovery notification.

### Verify logs

```bash
# Live journal output
journalctl -u argus -f

# Log file
tail -f /var/log/argus/argus.log
```

### Test your Discord webhook independently

```bash
curl -H "Content-Type: application/json" \
     -d '{"embeds":[{"title":"Test","description":"Argus webhook test","color":3447003}]}' \
     "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
```

### Run manually (without systemd)

```bash
cd /opt/argus
source venv/bin/activate
python argus.py
```

Press `Ctrl+C` for graceful shutdown.

## Architecture

Argus is a single Python script running in a `while True` loop -- no async frameworks, no threads, no external schedulers.

```
                    ┌──────────────┐
                    │   Startup    │
                    │  Validation  │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  Discord:    │
                    │  "Watching"  │
                    └──────┬───────┘
                           │
              ┌────────────▼────────────┐
              │      Main Loop          │
              │  ┌───────────────────┐  │
              │  │ For each Loki     │  │
              │  │ instance: /ready  │  │
              │  └────────┬──────────┘  │
              │  ┌────────▼──────────┐  │
              │  │ For each Grafana  │  │
              │  │ instance:         │  │
              │  │ /api/health       │  │
              │  └────────┬──────────┘  │
              │  ┌────────▼──────────┐  │
              │  │ State transition? │  │
              │  │ → Discord alert   │  │
              │  └────────┬──────────┘  │
              │  ┌────────▼──────────┐  │
              │  │ Sleep (interval)  │  │
              │  └────────┬──────────┘  │
              │           │             │
              └───────────┘─────────────┘
                           │
                    ┌──────▼───────┐
                    │  SIGTERM /   │
                    │  SIGINT      │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Shutdown   │
                    └──────────────┘
```

Key implementation details:

- **Connection pooling** via `requests.Session` with `HTTPAdapter`, pool sized dynamically to match instance count
- **1-second sleep granularity** inside the interval loop for prompt signal response
- **Top-level exception handler** wraps the main loop so the daemon never crashes
- **systemd** handles process supervision (`Restart=on-failure`, `RestartSec=30`)
- **Security hardening** in the unit file: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`

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

| Symptom | Solution |
|---|---|
| Service won't start | Run `journalctl -u argus -e` and look for `FATAL:` config errors |
| No Discord notifications | Test the webhook URL with the `curl` command above |
| Too many alerts | Increase `FAILURE_THRESHOLD` to require more consecutive failures |
| Alerts are delayed | Decrease `CHECK_INTERVAL_SECONDS` for faster detection |
| Permission denied on log file | Run `sudo chown argus:argus /var/log/argus` |
| Python version error | Argus requires Python 3.10+. Check with `python3 --version` |
| Config not taking effect | Restart the service after editing `.env`: `sudo systemctl restart argus` |

## Contributing

Contributions are welcome. Please open an issue to discuss proposed changes before submitting a pull request.

## License

This project is licensed under the [MIT License](LICENSE).
