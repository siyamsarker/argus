#!/usr/bin/env python3
"""
Argus v1.1.0 — The Hundred-Eyed Monitor

A lightweight health-monitoring daemon that watches one or more Loki and Grafana
instances and sends Discord alerts on state transitions. Named after Argus
Panoptes, the hundred-eyed giant of Greek mythology who never sleeps.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.1.0"
BANNER = f"Argus v{VERSION} — The Hundred-Eyed Monitor"
LOG_DIR = "/var/log/argus"
LOG_FILE = os.path.join(LOG_DIR, "argus.log")
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

# Discord embed colours (passed as integers to the Discord API)
COLOR_RED = 0xFF0000    # Alert: service went down
COLOR_GREEN = 0x00FF00  # Recovery: service came back up
COLOR_BLUE = 0x3498DB   # Startup: Argus is now watching

HOSTNAME = socket.gethostname()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    """Validated runtime configuration loaded from environment / .env.

    Supports multiple Loki and Grafana instances via comma-separated URLs
    in LOKI_URL and GRAFANA_URL (e.g. ``http://host1:3100,http://host2:3100``).
    A single URL works as before for backward compatibility.
    """

    def __init__(self) -> None:
        load_dotenv()

        self.loki_urls: list[str] = self._require_urls("LOKI_URL")
        self.grafana_urls: list[str] = self._require_urls("GRAFANA_URL")
        self.discord_webhook_url: str = self._require("DISCORD_WEBHOOK_URL")

        self.check_interval: int = self._require_int("CHECK_INTERVAL_SECONDS", 120)
        self.failure_threshold: int = self._require_int("FAILURE_THRESHOLD", 2)
        self.request_timeout: int = self._require_int("REQUEST_TIMEOUT_SECONDS", 10)
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

        self._validate()

    # ---- private helpers ----------------------------------------------------

    @staticmethod
    def _require(key: str) -> str:
        """Return the value of a required env var or exit with a clear error."""
        value = os.getenv(key)
        if not value:
            print(f"FATAL: Required environment variable '{key}' is not set.", file=sys.stderr)
            sys.exit(1)
        return value

    @staticmethod
    def _require_urls(key: str) -> list[str]:
        """Return a list of URLs from a comma-separated env var, or exit.

        Supports both single URLs (backward compatible) and comma-separated
        lists for multi-instance monitoring.
        """
        value = os.getenv(key)
        if not value:
            print(f"FATAL: Required environment variable '{key}' is not set.", file=sys.stderr)
            sys.exit(1)
        urls = [u.strip() for u in value.split(",") if u.strip()]
        if not urls:
            print(f"FATAL: '{key}' contains no valid URLs.", file=sys.stderr)
            sys.exit(1)
        return urls

    @staticmethod
    def _require_int(key: str, default: int) -> int:
        """Return an env var as int, or exit with a clear error if not numeric."""
        raw = os.getenv(key, str(default))
        try:
            return int(raw)
        except ValueError:
            print(f"FATAL: '{key}' must be an integer, got '{raw}'.", file=sys.stderr)
            sys.exit(1)

    def _validate(self) -> None:
        """Validate URLs and numeric ranges; exit on invalid config."""
        for name, urls in [("LOKI_URL", self.loki_urls), ("GRAFANA_URL", self.grafana_urls)]:
            for url in urls:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    print(f"FATAL: '{name}' contains an invalid URL: {url}", file=sys.stderr)
                    sys.exit(1)

        parsed = urlparse(self.discord_webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            print(
                f"FATAL: 'DISCORD_WEBHOOK_URL' is not a valid URL: {self.discord_webhook_url}",
                file=sys.stderr,
            )
            sys.exit(1)

        if self.check_interval < 1:
            print("FATAL: CHECK_INTERVAL_SECONDS must be >= 1.", file=sys.stderr)
            sys.exit(1)

        if self.failure_threshold < 1:
            print("FATAL: FAILURE_THRESHOLD must be >= 1.", file=sys.stderr)
            sys.exit(1)

        if self.request_timeout < 1:
            print("FATAL: REQUEST_TIMEOUT_SECONDS must be >= 1.", file=sys.stderr)
            sys.exit(1)

        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            print(f"FATAL: Invalid LOG_LEVEL '{self.log_level}'.", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(level_name: str) -> logging.Logger:
    """Configure logging to stdout and a rotating file."""
    logger = logging.getLogger("argus")
    logger.setLevel(getattr(logging, level_name))

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Stdout handler (captured by journald when running under systemd)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    # Rotating file handler — creates the directory if needed, skips
    # gracefully if permissions prevent it (e.g. running outside systemd).
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not set up file logging at %s: %s", LOG_FILE, exc)

    return logger


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def send_discord_embed(
    session: requests.Session,
    webhook_url: str,
    title: str,
    description: str,
    color: int,
    fields: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Send a Discord embed via webhook with retry & rate-limit handling.

    Returns True on success, False after all retries exhausted.
    """
    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": f"Argus v{VERSION} • {HOSTNAME}"},
            }
        ]
    }

    max_attempts = 3
    max_rate_limit_retries = 5
    backoff = 2  # seconds, doubles each retry

    attempt = 0
    rate_limit_hits = 0
    while attempt < max_attempts:
        try:
            resp = session.post(webhook_url, json=payload, timeout=15)

            # Discord rate-limit handling — retries without consuming an attempt,
            # but capped to prevent an infinite loop if 429s persist.
            if resp.status_code == 429:
                rate_limit_hits += 1
                if rate_limit_hits > max_rate_limit_retries:
                    if logger:
                        logger.error(
                            "Discord rate-limit retries exhausted (%d hits).",
                            rate_limit_hits,
                        )
                    return False
                try:
                    retry_after = float(resp.json().get("retry_after", backoff))
                except (ValueError, KeyError):
                    retry_after = float(backoff)
                retry_after = min(retry_after, 60.0)  # cap to avoid excessively long waits
                if logger:
                    logger.warning(
                        "Discord rate-limited; retrying after %.1f s (%d/%d)",
                        retry_after, rate_limit_hits, max_rate_limit_retries,
                    )
                time.sleep(retry_after)
                continue

            if resp.status_code in (200, 204):
                return True

            if logger:
                logger.error(
                    "Discord webhook returned %d (attempt %d/%d): %s",
                    resp.status_code, attempt + 1, max_attempts, resp.text[:200],
                )

        except requests.RequestException as exc:
            if logger:
                logger.error(
                    "Discord webhook request failed (attempt %d/%d): %s",
                    attempt + 1, max_attempts, exc,
                )

        attempt += 1
        time.sleep(backoff)
        backoff *= 2

    return False


# ---------------------------------------------------------------------------
# Health check implementations
# ---------------------------------------------------------------------------

def check_loki(session: requests.Session, url: str, timeout: int) -> tuple[bool, str]:
    """Check Loki readiness. Returns (healthy, reason)."""
    endpoint = url.rstrip("/") + "/ready"
    try:
        resp = session.get(endpoint, timeout=timeout)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code} from {endpoint}"
        if "ready" not in resp.text.lower():
            return False, f"Response body does not contain 'ready': {resp.text[:120]}"
        return True, "Loki is ready"
    except requests.ConnectionError as exc:
        return False, f"Connection error: {exc}"
    except requests.Timeout:
        return False, f"Request timed out after {timeout}s"
    except requests.RequestException as exc:
        return False, f"Request failed: {exc}"


def check_grafana(session: requests.Session, url: str, timeout: int) -> tuple[bool, str]:
    """Check Grafana health. Returns (healthy, reason)."""
    endpoint = url.rstrip("/") + "/api/health"
    try:
        resp = session.get(endpoint, timeout=timeout)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code} from {endpoint}"
        try:
            data = resp.json()
        except ValueError:
            return False, f"Invalid JSON response: {resp.text[:120]}"
        if data.get("database") != "ok":
            return False, f"Database field is '{data.get('database')}', expected 'ok'"
        return True, "Grafana is healthy"
    except requests.ConnectionError as exc:
        return False, f"Connection error: {exc}"
    except requests.Timeout:
        return False, f"Request timed out after {timeout}s"
    except requests.RequestException as exc:
        return False, f"Request failed: {exc}"


# ---------------------------------------------------------------------------
# Service state tracker
# ---------------------------------------------------------------------------

def _instance_label(service_type: str, url: str, total: int) -> str:
    """Generate a display label for a service instance.

    Uses just the service type for single instances (e.g. ``Loki``), and
    appends the host for multiple instances (e.g. ``Loki (host:3100)``).
    """
    if total == 1:
        return service_type
    parsed = urlparse(url)
    return f"{service_type} ({parsed.netloc})"


class ServiceState:
    """Track consecutive failures and healthy/unhealthy state for a service."""

    def __init__(self, name: str, threshold: int) -> None:
        self.name = name
        self.threshold = threshold
        self.consecutive_failures: int = 0
        self.is_healthy: bool = True  # assume healthy at start to avoid false alerts on first run
        self.last_reason: str = ""

    def record_success(self) -> bool:
        """Record a successful check. Returns True if state transitioned to healthy."""
        self.consecutive_failures = 0
        if not self.is_healthy:
            self.is_healthy = True
            self.last_reason = ""
            return True  # recovered
        return False

    def record_failure(self, reason: str) -> bool:
        """Record a failed check. Returns True if state transitioned to unhealthy."""
        self.consecutive_failures += 1
        self.last_reason = reason
        if self.is_healthy and self.consecutive_failures >= self.threshold:
            self.is_healthy = False
            return True  # newly unhealthy
        return False


# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------

def send_alert(
    session: requests.Session,
    webhook_url: str,
    service: ServiceState,
    logger: logging.Logger,
) -> None:
    """Send an unhealthy alert embed to Discord."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fields = [
        {"name": "Service", "value": service.name, "inline": True},
        {"name": "Status", "value": "UNHEALTHY", "inline": True},
        {"name": "Timestamp", "value": now, "inline": False},
        {"name": "Reason", "value": service.last_reason[:1024], "inline": False},
        {"name": "Host", "value": HOSTNAME, "inline": True},
    ]
    ok = send_discord_embed(
        session, webhook_url,
        title=f"\u26a0\ufe0f {service.name} is DOWN",
        description=f"{service.name} has failed {service.consecutive_failures} consecutive health checks.",
        color=COLOR_RED,
        fields=fields,
        logger=logger,
    )
    if ok:
        logger.warning("Alert sent to Discord: %s is UNHEALTHY — %s", service.name, service.last_reason)
    else:
        logger.error("Failed to send alert to Discord for %s", service.name)


def send_recovery(
    session: requests.Session,
    webhook_url: str,
    service: ServiceState,
    logger: logging.Logger,
) -> None:
    """Send a recovery embed to Discord."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fields = [
        {"name": "Service", "value": service.name, "inline": True},
        {"name": "Status", "value": "HEALTHY", "inline": True},
        {"name": "Timestamp", "value": now, "inline": False},
        {"name": "Host", "value": HOSTNAME, "inline": True},
    ]
    ok = send_discord_embed(
        session, webhook_url,
        title=f"\u2705 {service.name} has RECOVERED",
        description=f"{service.name} is back online and healthy.",
        color=COLOR_GREEN,
        fields=fields,
        logger=logger,
    )
    if ok:
        logger.info("Recovery notification sent to Discord: %s is HEALTHY", service.name)
    else:
        logger.error("Failed to send recovery notification to Discord for %s", service.name)


def send_startup_notification(
    session: requests.Session,
    webhook_url: str,
    config: Config,
    logger: logging.Logger,
) -> None:
    """Send a one-time startup notification to Discord."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fields = [
        {"name": "Host", "value": HOSTNAME, "inline": True},
        {"name": "Interval", "value": f"{config.check_interval}s", "inline": True},
        {"name": "Failure Threshold", "value": str(config.failure_threshold), "inline": True},
        {"name": "Loki", "value": ", ".join(config.loki_urls), "inline": False},
        {"name": "Grafana", "value": ", ".join(config.grafana_urls), "inline": False},
        {"name": "Started At", "value": now, "inline": False},
    ]
    ok = send_discord_embed(
        session, webhook_url,
        title=f"\U0001f441\ufe0f Argus is now watching",
        description=BANNER,
        color=COLOR_BLUE,
        fields=fields,
        logger=logger,
    )
    if ok:
        logger.info("Startup notification sent to Discord.")
    else:
        logger.error("Failed to send startup notification to Discord.")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _signal_handler(signum: int, _frame: Any) -> None:
    """Set the shutdown flag on SIGTERM/SIGINT."""
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    logging.getLogger("argus").info("Received %s — initiating graceful shutdown.", sig_name)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point: load config, set up logging, run the health-check loop."""
    # Load and validate configuration
    config = Config()

    # Set up logging
    logger = setup_logging(config.log_level)
    logger.info(BANNER)

    # Register signal handlers for graceful shutdown (SIGTERM from systemd, SIGINT from Ctrl+C)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # HTTP session with connection pooling (sized for all monitored instances)
    session = requests.Session()
    pool_size = len(config.loki_urls) + len(config.grafana_urls) + 2
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,  # we handle retries ourselves
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Startup notification
    send_startup_notification(session, config.discord_webhook_url, config, logger)

    # Service states — one per monitored instance for independent tracking
    loki_instances: list[tuple[str, ServiceState]] = [
        (url, ServiceState(
            _instance_label("Loki", url, len(config.loki_urls)),
            config.failure_threshold,
        ))
        for url in config.loki_urls
    ]
    grafana_instances: list[tuple[str, ServiceState]] = [
        (url, ServiceState(
            _instance_label("Grafana", url, len(config.grafana_urls)),
            config.failure_threshold,
        ))
        for url in config.grafana_urls
    ]

    loki_summary = ", ".join(config.loki_urls)
    grafana_summary = ", ".join(config.grafana_urls)
    logger.info(
        "Monitoring started — Loki: [%s] | Grafana: [%s] | Interval: %ds | Threshold: %d",
        loki_summary, grafana_summary, config.check_interval, config.failure_threshold,
    )

    # ----- main loop -------------------------------------------------------
    while not _shutdown_requested:
        try:
            # -- Loki checks -------------------------------------------------
            for url, state in loki_instances:
                healthy, reason = check_loki(session, url, config.request_timeout)
                logger.debug("%s check: healthy=%s reason=%s", state.name, healthy, reason)

                if healthy:
                    transitioned = state.record_success()
                    if transitioned:
                        logger.info("%s recovered.", state.name)
                        send_recovery(session, config.discord_webhook_url, state, logger)
                else:
                    transitioned = state.record_failure(reason)
                    if transitioned:
                        logger.warning("%s is UNHEALTHY: %s", state.name, reason)
                        send_alert(session, config.discord_webhook_url, state, logger)
                    else:
                        logger.debug(
                            "%s failure %d/%d: %s",
                            state.name, state.consecutive_failures, config.failure_threshold, reason,
                        )

            # -- Grafana checks ----------------------------------------------
            for url, state in grafana_instances:
                healthy, reason = check_grafana(session, url, config.request_timeout)
                logger.debug("%s check: healthy=%s reason=%s", state.name, healthy, reason)

                if healthy:
                    transitioned = state.record_success()
                    if transitioned:
                        logger.info("%s recovered.", state.name)
                        send_recovery(session, config.discord_webhook_url, state, logger)
                else:
                    transitioned = state.record_failure(reason)
                    if transitioned:
                        logger.warning("%s is UNHEALTHY: %s", state.name, reason)
                        send_alert(session, config.discord_webhook_url, state, logger)
                    else:
                        logger.debug(
                            "%s failure %d/%d: %s",
                            state.name, state.consecutive_failures, config.failure_threshold, reason,
                        )

        # Catch-all: log the error and keep the daemon alive.
        # systemd Restart=on-failure handles truly fatal crashes.
        except Exception:
            logger.exception("Unexpected error in health-check loop — continuing.")

        # Sleep in 1-second increments instead of one long sleep so we can
        # respond to SIGTERM/SIGINT promptly without waiting the full interval.
        for _ in range(config.check_interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    # ----- clean shutdown ---------------------------------------------------
    session.close()
    logger.info("Argus shutting down. Goodbye.")


if __name__ == "__main__":
    main()
