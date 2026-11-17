"""Heartbeat pings for "dead man's switch" monitors such as Healthchecks.io or Uptime Kuma push monitors.

Notifications only fire when a run *finishes*. A heartbeat monitor also alerts when a run never starts (timer
disabled, server down, lock held for days), because it expects a ping on a schedule. The run pings
``<url>/start`` when it begins, ``<url>`` on success and ``<url>/fail`` on failure; the request body is the plain-text
run summary. The URL usually contains a secret check id, so it is read from an environment variable.

A failed ping is logged as a warning and never fails the backup itself.
"""

from __future__ import annotations

import logging
import os

from cloudpanel_backup.config import NotifySettings
from cloudpanel_backup.http import Transport, UrllibTransport

log = logging.getLogger(__name__)

BODY_LIMIT = 10_000  # Healthchecks.io keeps the first 10 kB of a ping body
STATES = ("start", "success", "fail")


class Heartbeat:
    def __init__(self, settings: NotifySettings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or UrllibTransport(timeout=10)

    def base_url(self) -> str | None:
        name = self.settings.heartbeat_url_env
        if not name:
            return None
        url = (os.environ.get(name) or "").strip()
        if not url:
            log.warning("heartbeat: environment variable %s is not set; skipping pings", name)
            return None
        if not url.startswith("https://"):
            log.warning("heartbeat: %s must hold an https:// URL; skipping pings", name)
            return None
        return url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.settings.heartbeat_url_env)

    def url_for(self, state: str) -> str | None:
        if state not in STATES:
            raise ValueError(f"unknown heartbeat state {state!r}")
        base = self.base_url()
        if base is None:
            return None
        return base if state == "success" else f"{base}/{state}"

    def ping(self, state: str, message: str = "") -> bool:
        """Send one ping. Returns True when it was delivered, False when skipped or failed."""
        url = self.url_for(state)
        if url is None:
            return False
        body = message.encode("utf-8")[:BODY_LIMIT]
        try:
            resp = self.transport.request("POST", url, {"Content-Type": "text/plain; charset=utf-8"}, body)
        except Exception as exc:
            log.warning("heartbeat %s ping failed: %s", state, exc)
            return False
        if not resp.ok:
            log.warning("heartbeat %s ping answered HTTP %s", state, resp.status)
            return False
        log.debug("heartbeat %s ping sent", state)
        return True

    def start(self) -> bool:
        return self.ping("start")

    def finish(self, ok: bool, message: str = "") -> bool:
        return self.ping("success" if ok else "fail", message)
