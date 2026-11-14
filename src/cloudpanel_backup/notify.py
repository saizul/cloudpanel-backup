"""Notifications after a run: Telegram, a JSON webhook and email (SMTP)."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from cloudpanel_backup.config import NotifySettings, require_env
from cloudpanel_backup.http import Transport, UrllibTransport

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4096


@dataclass
class Event:
    action: str  # "backup", "verify", "prune", "test"
    ok: bool
    host: str
    summary: str
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        status = "OK" if self.ok else "FAILED"
        return f"[cloudpanel-backup] {self.action} {status} on {self.host}"

    def text(self) -> str:
        lines = [self.title, "", self.summary]
        for item in self.details:
            mark = "ok" if item.get("ok") else "FAILED"
            line = f"- {item.get('domain', '?')}: {mark}"
            if item.get("message"):
                line += f" ({item['message']})"
            lines.append(line)
        return "\n".join(lines)


SmtpFactory = Callable[[NotifySettings], Any]


def _default_smtp(settings: NotifySettings) -> Any:
    assert settings.smtp_host is not None
    context = ssl.create_default_context()
    if settings.smtp_ssl:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30)
    client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    if settings.smtp_starttls:
        client.starttls(context=context)
    return client


class Notifier:
    def __init__(
        self,
        settings: NotifySettings,
        transport: Transport | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or UrllibTransport(timeout=20)
        self.smtp_factory = smtp_factory or _default_smtp

    def channels(self) -> list[str]:
        s = self.settings
        out = []
        if s.telegram_bot_token_env and s.telegram_chat_id:
            out.append("telegram")
        if s.webhook_url or s.webhook_url_env:
            out.append("webhook")
        if s.email_to and s.smtp_host:
            out.append("email")
        return out

    def should_send(self, event: Event) -> bool:
        if self.settings.on == "never":
            return False
        return self.settings.on == "always" or not event.ok or event.action == "test"

    def send(self, event: Event) -> list[str]:
        """Send to every configured channel. Returns the channels that failed; a failure never raises."""
        failed: list[str] = []
        if not self.should_send(event):
            return failed
        for channel in self.channels():
            try:
                getattr(self, f"_send_{channel}")(event)
            except Exception as exc:
                log.error("notification via %s failed: %s", channel, exc)
                failed.append(channel)
        return failed

    def _send_telegram(self, event: Event) -> None:
        s = self.settings
        assert s.telegram_bot_token_env is not None
        token = require_env(s.telegram_bot_token_env, "Telegram bot token")
        text = event.text()
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 20] + "\n... (truncated)"
        body = json.dumps({"chat_id": s.telegram_chat_id, "text": text, "disable_web_page_preview": True})
        resp = self.transport.request(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"Content-Type": "application/json"},
            body.encode("utf-8"),
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram answered HTTP {resp.status}")

    def _send_webhook(self, event: Event) -> None:
        s = self.settings
        url = s.webhook_url
        if s.webhook_url_env:
            url = os.environ.get(s.webhook_url_env) or url
        if not url:
            raise RuntimeError("webhook URL is not set")
        if not url.startswith("https://"):
            raise RuntimeError("webhook URL must use https://")
        payload = {
            "source": "cloudpanel-backup",
            "action": event.action,
            "ok": event.ok,
            "host": event.host,
            "title": event.title,
            "summary": event.summary,
            "text": event.text(),
            "details": event.details,
        }
        resp = self.transport.request(
            "POST", url, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")
        )
        if not resp.ok:
            raise RuntimeError(f"webhook answered HTTP {resp.status}")

    def _send_email(self, event: Event) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = event.title
        msg["From"] = s.email_from or f"cloudpanel-backup@{event.host}"
        msg["To"] = ", ".join(s.email_to)
        msg.set_content(event.text())
        client = self.smtp_factory(s)
        try:
            if s.smtp_user:
                password = require_env(s.smtp_password_env, "SMTP password") if s.smtp_password_env else ""
                client.login(s.smtp_user, password)
            client.send_message(msg)
        finally:
            client.quit()
