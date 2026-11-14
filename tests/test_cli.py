import io
import json
import os
from pathlib import Path

import pytest

from cloudpanel_backup.cli import load_env_file, main
from conftest import ACCESS_KEY, SECRET_KEY, FakeRunner, FakeS3
from test_notify import RecordingTransport


def write_config(server, extra: str = "", storage: str | None = None) -> Path:
    path = server["root"] / "config.toml"
    storage = storage or f'[storage]\ntype = "local"\npath = "{server["root"] / "store"}"\n'
    path.write_text(
        f"""
[backup]
staging_dir = "{server["root"] / "staging"}"
home_root = "{server["home"]}"
nginx_dir = "{server["nginx"]}"
host = "vps1"

{storage}
{extra}
"""
    )
    return path


def cli(args, runner=None, transport=None) -> tuple[int, str]:
    out = io.StringIO()
    code = main(args, out=out, runner=runner or FakeRunner(), transport=transport)
    return code, out.getvalue()


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "cloudpanel-backup 0.1.0" in capsys.readouterr().out


def test_init_config(tmp_path: Path):
    code, out = cli(["init-config"])
    assert code == 0 and "[storage]" in out
    target = tmp_path / "c.toml"
    assert cli(["init-config", "--output", str(target)])[0] == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert cli(["init-config", "--output", str(target)])[0] == 2


def test_discover(server):
    cfg = write_config(server)
    code, out = cli(["--config", str(cfg), "discover"])
    assert code == 0
    assert "shop.example.com" in out and "db=shop_wp" in out
    code, out = cli(["--config", str(cfg), "discover", "--json"])
    data = json.loads(out)
    assert data["cloudpanel"] is True
    assert {s["domain"] for s in data["sites"]} == {"shop.example.com", "www.blog.example.org"}
    code, out = cli(["--config", str(cfg), "discover"], runner=FakeRunner(programs=[]))
    assert "does not look like a CloudPanel server" in out


def test_backup_list_verify_restore_prune(server, tmp_path):
    cfg = write_config(server)
    base = ["--config", str(cfg)]
    code, out = cli([*base, "backup", "--dry-run"])
    assert code == 0 and "dump database shop_wp" in out
    assert not (server["root"] / "store").exists()

    code, out = cli([*base, "backup", "--json"])
    assert code == 0, out
    result = json.loads(out)
    assert result["ok"] and len(result["results"]) == 2

    code, out = cli([*base, "list"])
    assert code == 0 and "shop.example.com" in out
    code, out = cli([*base, "list", "--json", "--site", "shop.example.com"])
    assert len(json.loads(out)) == 1

    code, out = cli([*base, "verify", "--deep"])
    assert code == 0 and out.count(" ok ") == 2

    code, out = cli([*base, "restore", "shop.example.com", "--dry-run"])
    assert code == 0 and out.startswith("restore plan")

    target = tmp_path / "restored"
    runner = FakeRunner()
    code, out = cli(
        [*base, "restore", "shop.example.com", "--target", str(target), "--db-map", "shop_wp=new_db", "--no-chown"],
        runner=runner,
    )
    assert code == 0, out
    assert (target / "index.php").exists() and "new_db" in runner.imports

    code, out = cli([*base, "restore", "nope.example"])
    assert code == 1 and "no complete backup" in out
    assert cli([*base, "restore", "shop.example.com", "--db-map", "bad"])[0] == 2

    code, out = cli([*base, "prune", "--dry-run"])
    assert code == 0 and "would delete 0 backup(s)" in out


def test_backup_unknown_site_and_failure_notifies(server):
    hook = RecordingTransport()
    cfg = write_config(server, extra='[notify]\nwebhook_url = "https://hooks.example.com/b"\n')
    assert cli(["--config", str(cfg), "backup", "--site", "missing.example"])[0] == 2
    runner = FakeRunner()
    runner.fail["mariadb-dump"] = "denied"
    code, out = cli(["--config", str(cfg), "backup"], runner=runner, transport=hook)
    assert code == 1 and "FAILED" in out
    assert hook.sent and hook.sent[0][2]["ok"] is False


def test_backup_to_s3_via_cli(server, s3_env):
    fake = FakeS3()
    storage = '[storage]\ntype = "s3"\nbucket = "backups"\nendpoint = "https://acct.r2.cloudflarestorage.com"\n'
    cfg = write_config(server, storage=storage)
    code, out = cli(["--config", str(cfg), "backup", "--site", "shop.example.com"], transport=fake)
    assert code == 0, out
    assert any(k.endswith("manifest.json") for k in fake.objects)
    code, out = cli(["--config", str(cfg), "verify"], transport=fake)
    assert code == 0


def test_verify_with_no_backups_fails(server):
    cfg = write_config(server)
    code, out = cli(["--config", str(cfg), "verify"])
    assert code == 1 and "no backups found" in out


def test_schedule_outputs(server, tmp_path):
    code, out = cli(["schedule"])
    assert code == 0 and "# --- cloudpanel-backup.timer" in out
    code, out = cli(["schedule", "--cron", "--cron-schedule", "0 4 * * *"])
    assert out.splitlines()[-1].startswith("0 4 * * * root ")
    code, out = cli(["schedule", "--install", "--unit-dir", str(tmp_path)])
    assert (tmp_path / "cloudpanel-backup.timer").exists() and "systemctl daemon-reload" in out


def test_test_notify(server):
    cfg = write_config(server)
    assert cli(["--config", str(cfg), "test-notify"])[0] == 1
    cfg = write_config(server, extra='[notify]\nwebhook_url = "https://hooks.example.com/t"\n')
    hook = RecordingTransport()
    code, out = cli(["--config", str(cfg), "test-notify"], transport=hook)
    assert code == 0 and "webhook: sent" in out and hook.sent


def test_missing_config_is_usage_error(tmp_path):
    assert cli(["--config", str(tmp_path / "none.toml"), "discover"])[0] == 2


def test_env_file(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text(f"CPB_S3_ACCESS_KEY_ID={ACCESS_KEY}\nCPB_S3_SECRET_ACCESS_KEY={SECRET_KEY}\nCPB_KEEP=new\n")
    os.chmod(env, 0o600)
    monkeypatch.setenv("CPB_KEEP", "old")
    for name in ("CPB_S3_ACCESS_KEY_ID", "CPB_S3_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "placeholder")  # registers the variable so it is removed after the test
        monkeypatch.delenv(name)
    assert load_env_file(env) == 2
    assert os.environ["CPB_KEEP"] == "old"
    assert os.environ["CPB_S3_ACCESS_KEY_ID"] == ACCESS_KEY
    assert load_env_file(tmp_path / "missing") == 0
