import pytest

from cloudpanel_backup.config import NotifySettings
from cloudpanel_backup.heartbeat import BODY_LIMIT, Heartbeat
from cloudpanel_backup.http import HttpResponse
from conftest import FakeRunner
from test_cli import cli, write_config

PING_ENV = "CPB_TEST_HEARTBEAT_URL"
PING_URL = "https://hc-ping.example.com/check-uuid"


class PingTransport:
    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.sent: list[tuple[str, str, dict, bytes]] = []

    def request(self, method, url, headers, body=None, sink=None):
        self.sent.append((method, url, dict(headers), body or b""))
        if self.error is not None:
            raise self.error
        return HttpResponse(self.status)

    @property
    def urls(self) -> list[str]:
        return [url for _, url, _, _ in self.sent]


@pytest.fixture
def ping_env(monkeypatch):
    monkeypatch.setenv(PING_ENV, PING_URL + "/")


def heartbeat(transport) -> Heartbeat:
    return Heartbeat(NotifySettings(heartbeat_url_env=PING_ENV), transport)


def test_ping_urls(ping_env):
    transport = PingTransport()
    hb = heartbeat(transport)
    assert hb.enabled
    assert hb.start() and hb.finish(True, "all good") and hb.finish(False, "broken")
    assert transport.urls == [PING_URL + "/start", PING_URL, PING_URL + "/fail"]
    method, _, headers, body = transport.sent[2]
    assert method == "POST" and headers["Content-Type"].startswith("text/plain") and body == b"broken"
    with pytest.raises(ValueError):
        hb.url_for("later")


def test_body_is_truncated(ping_env):
    transport = PingTransport()
    heartbeat(transport).finish(True, "x" * (BODY_LIMIT + 50))
    assert len(transport.sent[0][3]) == BODY_LIMIT


def test_disabled_missing_or_insecure_url(monkeypatch, caplog):
    transport = PingTransport()
    assert not Heartbeat(NotifySettings(), transport).enabled
    assert Heartbeat(NotifySettings(), transport).start() is False
    monkeypatch.delenv(PING_ENV, raising=False)
    assert heartbeat(transport).start() is False
    assert "is not set" in caplog.text
    monkeypatch.setenv(PING_ENV, "http://hc-ping.example.com/check-uuid")
    assert heartbeat(transport).start() is False
    assert "https://" in caplog.text
    assert transport.sent == []


def test_ping_failures_never_raise(ping_env, caplog):
    assert heartbeat(PingTransport(status=500)).start() is False
    assert heartbeat(PingTransport(error=OSError("network down"))).finish(True) is False
    assert "HTTP 500" in caplog.text and "network down" in caplog.text
    assert PING_URL not in caplog.text  # the URL holds the check id; never log it


def test_backup_pings_start_and_success(server, ping_env):
    cfg = write_config(server, extra=f'[notify]\nheartbeat_url_env = "{PING_ENV}"\n')
    transport = PingTransport()
    code, out = cli(["--config", str(cfg), "backup"], transport=transport)
    assert code == 0, out
    assert transport.urls == [PING_URL + "/start", PING_URL]
    assert b"2/2 site(s) backed up" in transport.sent[1][3]


def test_backup_failure_pings_fail(server, ping_env):
    cfg = write_config(server, extra=f'[notify]\nheartbeat_url_env = "{PING_ENV}"\n')
    runner = FakeRunner()
    runner.fail["mariadb-dump"] = "denied"
    transport = PingTransport()
    code, _ = cli(["--config", str(cfg), "backup"], runner=runner, transport=transport)
    assert code == 1
    assert transport.urls == [PING_URL + "/start", PING_URL + "/fail"]
    assert b"FAILED" in transport.sent[1][3]


def test_failing_heartbeat_does_not_fail_backup(server, ping_env):
    cfg = write_config(server, extra=f'[notify]\nheartbeat_url_env = "{PING_ENV}"\n')
    code, out = cli(["--config", str(cfg), "backup"], transport=PingTransport(status=503))
    assert code == 0, out


def test_no_pings_for_dry_run_or_no_notify(server, ping_env):
    cfg = write_config(server, extra=f'[notify]\nheartbeat_url_env = "{PING_ENV}"\n')
    transport = PingTransport()
    assert cli(["--config", str(cfg), "backup", "--dry-run"], transport=transport)[0] == 0
    assert cli(["--config", str(cfg), "backup", "--no-notify"], transport=transport)[0] == 0
    assert transport.sent == []


def test_test_notify_sends_heartbeat(server, ping_env):
    cfg = write_config(server, extra=f'[notify]\nheartbeat_url_env = "{PING_ENV}"\n')
    transport = PingTransport()
    code, out = cli(["--config", str(cfg), "test-notify"], transport=transport)
    assert code == 0 and "heartbeat: sent" in out
    assert transport.urls == [PING_URL]
    code, out = cli(["--config", str(cfg), "test-notify"], transport=PingTransport(status=404))
    assert code == 1 and "heartbeat: FAILED" in out
