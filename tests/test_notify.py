import json
import typing

from cloudpanel_backup.config import NotifySettings
from cloudpanel_backup.http import HttpResponse
from cloudpanel_backup.notify import Event, Notifier


class RecordingTransport:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.sent: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, sink=None):
        self.sent.append((method, url, json.loads(body)))
        return HttpResponse(self.status)


class FakeSMTP:
    instances: typing.ClassVar[list["FakeSMTP"]] = []

    def __init__(self, settings):
        self.settings = settings
        self.logged_in = None
        self.messages = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.messages.append(msg)

    def quit(self):
        self.closed = True


def event(ok: bool = False) -> Event:
    return Event(
        "backup",
        ok,
        "vps1",
        "1/2 site(s) backed up",
        [{"domain": "a.example", "ok": True}, {"domain": "b.example", "ok": False, "message": "boom"}],
    )


def test_event_text():
    text = event().text()
    assert text.startswith("[cloudpanel-backup] backup FAILED on vps1")
    assert "- b.example: FAILED (boom)" in text


def test_telegram_and_webhook(monkeypatch):
    monkeypatch.setenv("TG", "123:abc")
    monkeypatch.setenv("HOOK", "https://hooks.example.com/x")
    transport = RecordingTransport()
    settings = NotifySettings(telegram_bot_token_env="TG", telegram_chat_id="42", webhook_url_env="HOOK")
    notifier = Notifier(settings, transport)
    assert notifier.channels() == ["telegram", "webhook"]
    assert notifier.send(event()) == []
    (_m1, url1, body1), (_m2, url2, body2) = transport.sent
    assert url1 == "https://api.telegram.org/bot123:abc/sendMessage"
    assert body1["chat_id"] == "42" and "FAILED" in body1["text"]
    assert url2 == "https://hooks.example.com/x"
    assert body2["ok"] is False and body2["source"] == "cloudpanel-backup"


def test_on_failure_skips_success():
    transport = RecordingTransport()
    notifier = Notifier(NotifySettings(webhook_url="https://hooks.example.com"), transport)
    notifier.send(event(ok=True))
    assert transport.sent == []
    notifier.settings.on = "always"
    notifier.send(event(ok=True))
    assert len(transport.sent) == 1
    notifier.settings.on = "never"
    notifier.send(event())
    assert len(transport.sent) == 1


def test_failures_are_reported_not_raised(monkeypatch):
    monkeypatch.delenv("TG", raising=False)
    settings = NotifySettings(telegram_bot_token_env="TG", telegram_chat_id="1", webhook_url="http://insecure")
    failed = Notifier(settings, RecordingTransport()).send(event())
    assert failed == ["telegram", "webhook"]
    failed = Notifier(NotifySettings(webhook_url="https://h.example"), RecordingTransport(500)).send(event())
    assert failed == ["webhook"]


def test_email(monkeypatch):
    monkeypatch.setenv("SMTP_PASS", "pw")
    settings = NotifySettings(
        email_to=["ops@example.com"],
        smtp_host="smtp.example.com",
        smtp_user="u",
        smtp_password_env="SMTP_PASS",
        email_from="backup@example.com",
    )
    FakeSMTP.instances.clear()
    assert Notifier(settings, RecordingTransport(), FakeSMTP).send(event()) == []
    smtp = FakeSMTP.instances[0]
    assert smtp.logged_in == ("u", "pw") and smtp.closed
    msg = smtp.messages[0]
    assert msg["To"] == "ops@example.com" and "FAILED" in msg["Subject"]


def test_long_telegram_messages_are_truncated(monkeypatch):
    monkeypatch.setenv("TG", "t")
    transport = RecordingTransport()
    big = Event("backup", False, "h", "x" * 5000)
    Notifier(NotifySettings(telegram_bot_token_env="TG", telegram_chat_id="1"), transport).send(big)
    assert len(transport.sent[0][2]["text"]) <= 4096
