import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cloudpanel_backup.backup import BackupRunner
from cloudpanel_backup.catalog import find_backup, list_backups
from cloudpanel_backup.discovery import discover_sites
from cloudpanel_backup.manifest import Manifest
from cloudpanel_backup.restore import ChecksumError, RestoreError, RestoreOptions, Restorer, plan_restore
from cloudpanel_backup.storage import LocalStorage, S3Storage
from conftest import ACCESS_KEY, SECRET_KEY, FakeRunner, FakeS3, make_config

NOW = datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)


def run_backup(cfg, storage, runner=None, now=NOW, prune=True):
    runner = runner or FakeRunner()
    sites = discover_sites(cfg, runner)
    report = BackupRunner(cfg, runner, storage, "vps1", now=now).run(sites, prune=prune)
    return report, runner


def test_backup_to_local_storage_writes_manifest_last(server):
    cfg = make_config(server)
    storage = LocalStorage(server["root"] / "store")
    report, runner = run_backup(cfg, storage)
    assert report.ok, [r.message for r in report.results]
    assert len(report.results) == 2
    shop = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    assert shop is not None and shop.timestamp == "20261114T023000Z"
    names = sorted(o.key.rsplit("/", 1)[1] for o in shop.objects)
    assert names == ["db-shop_wp.sql.gz", "files.tar.gz", "manifest.json", "vhost.conf"]
    manifest = shop.load_manifest(storage)
    assert manifest.site["kind"] == "wordpress"
    assert manifest.entry("files.tar.gz").details["files"] == 3
    db = manifest.by_kind("database")[0]
    assert db.database == "shop_wp"
    # credentials came from wp-config.php, not the master credentials
    assert any('user="shop_user"' in c for c in runner.cnf_contents)
    assert not runner.commands("clpctl")
    assert not (server["root"] / "staging" / "shop.example.com").exists() or not any(
        (server["root"] / "staging" / "shop.example.com").iterdir()
    )
    assert "2/2 site(s) backed up" in report.summary()


def test_backup_and_restore_through_fake_s3_with_age(server, s3_env, tmp_path):
    fake = FakeS3(page_size=3)
    storage = S3Storage(
        "backups",
        ACCESS_KEY,
        SECRET_KEY,
        endpoint="https://acct.r2.cloudflarestorage.com",
        transport=fake,
        retry_delay=0,
    )
    cfg = make_config(
        server, encryption={"method": "age", "recipients": ["age1example"], "identity_file": str(tmp_path / "id.txt")}
    )
    report, _runner = run_backup(cfg, storage)
    assert report.ok
    keys = sorted(fake.objects)
    assert all(k.startswith("cloudpanel/vps1/") for k in keys)
    assert any(k.endswith("files.tar.gz.age") for k in keys)
    assert not any(k.endswith("files.tar.gz") for k in keys)
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    assert backup.load_manifest(storage).encryption == "age"

    target = tmp_path / "restored-site"
    options = RestoreOptions(target_dir=target, chown=False)
    restore_runner = FakeRunner()
    Restorer(cfg, restore_runner, storage).restore(backup, options)
    assert (target / "index.php").read_text() == "<?php echo 'hello';\n"
    assert not (target / "node_modules").exists()
    assert restore_runner.imports["shop_wp"].startswith(b"-- dump of shop_wp")
    assert (tmp_path / f"restored-site.vhost-{backup.timestamp}.conf").is_file()


def test_restore_moves_existing_files_aside(server):
    cfg = make_config(server)
    storage = LocalStorage(server["root"] / "store")
    run_backup(cfg, storage)
    shop = server["shop"]
    (shop / "index.php").write_text("broken")
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    restorer = Restorer(cfg, FakeRunner(), storage)
    with pytest.raises(RestoreError, match="--force"):
        restorer.restore(backup, RestoreOptions(databases=False, chown=False))
    restorer.restore(backup, RestoreOptions(databases=False, force=True, chown=False))
    assert (shop / "index.php").read_text() == "<?php echo 'hello';\n"
    previous = [p for p in shop.parent.iterdir() if ".before-restore-" in p.name]
    assert len(previous) == 1 and (previous[0] / "index.php").read_text() == "broken"
    assert not [p for p in shop.parent.iterdir() if p.name.startswith(".")]


def test_restore_db_only_with_mapping(server):
    cfg = make_config(server)
    storage = LocalStorage(server["root"] / "store")
    run_backup(cfg, storage)
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    runner = FakeRunner()
    steps = Restorer(cfg, runner, storage).restore(
        backup, RestoreOptions(files=False, db_map={"shop_wp": "staging_wp"}, chown=False)
    )
    assert "staging_wp" in runner.imports
    assert any(s.action == "database" and "staging_wp" in s.detail for s in steps)
    # no wp-config for staging_wp: master credentials from clpctl are used
    assert any('user="root"' in c for c in runner.cnf_contents)


def test_restore_dry_run_changes_nothing(server):
    cfg = make_config(server)
    storage = LocalStorage(server["root"] / "store")
    run_backup(cfg, storage)
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    runner = FakeRunner()
    steps = Restorer(cfg, runner, storage).restore(backup, RestoreOptions(dry_run=True))
    actions = [s.action for s in steps]
    assert actions.count("download") == 3
    assert "files" in actions and "database" in actions and "chown" in actions
    assert runner.calls == []


def test_restore_detects_tampering(server):
    cfg = make_config(server)
    store = server["root"] / "store"
    storage = LocalStorage(store)
    run_backup(cfg, storage)
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    (store / backup.object_key("files.tar.gz")).write_bytes(b"tampered")
    with pytest.raises(ChecksumError):
        Restorer(cfg, FakeRunner(), storage).restore(backup, RestoreOptions(target_dir=server["root"] / "x"))


def test_failed_site_does_not_stop_others(server):
    cfg = make_config(server)
    storage = LocalStorage(server["root"] / "store")
    runner = FakeRunner()
    runner.fail["mariadb-dump"] = "Access denied for user"
    report, _ = run_backup(cfg, storage, runner)
    by = {r.domain: r for r in report.results}
    assert not by["shop.example.com"].ok and "Access denied" in by["shop.example.com"].message
    assert by["www.blog.example.org"].ok
    assert not report.ok
    # the failed site left no manifest, so no complete backup
    assert find_backup(storage, "cloudpanel", "vps1", "shop.example.com") is None


def test_clpctl_dump_method(server):
    cfg = make_config(server, backup={"db_method": "clpctl"})
    storage = LocalStorage(server["root"] / "store")
    report, runner = run_backup(cfg, storage)
    assert report.ok
    assert runner.commands("clpctl")[0][1] == "db:export"
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    data = storage.get_bytes(backup.object_key("db-shop_wp.sql.gz"))
    assert gzip.decompress(data).startswith(b"-- clpctl export")


def test_retention_prunes_old_backups(server):
    cfg = make_config(
        server, retention={"daily": 3, "weekly": 0, "monthly": 0, "min_keep": 1}, backup={"sites": ["shop.*"]}
    )
    storage = LocalStorage(server["root"] / "store")
    for day in range(6):
        report, _ = run_backup(cfg, storage, now=NOW + timedelta(days=day))
        assert report.ok
    sets = list_backups(storage, "cloudpanel", "vps1", "shop.example.com")
    assert [b.timestamp[:8] for b in sets] == ["20261117", "20261118", "20261119"]
    assert report.results[0].pruned == ["20261116T023000Z"]


def test_incomplete_leftovers_are_pruned(server):
    cfg = make_config(server, backup={"sites": ["shop.*"]})
    storage = LocalStorage(server["root"] / "store")
    storage.put_bytes(b"partial", "cloudpanel/vps1/shop.example.com/20261101T023000Z/files.tar.gz")
    report, _ = run_backup(cfg, storage)
    assert "20261101T023000Z" in report.results[0].pruned


def test_plan_restore_lists_steps():
    manifest = Manifest.from_json(
        json.dumps(
            {
                "tool": "cloudpanel-backup",
                "format_version": 1,
                "domain": "a.example",
                "host": "h",
                "timestamp": "20261114T000000Z",
                "tool_version": "0.1.0",
                "encryption": "gpg",
                "site": {"site_dir": "/nonexistent/a.example", "user": "a"},
                "entries": [
                    {
                        "name": "files.tar.gz.gpg",
                        "kind": "files",
                        "size": 1,
                        "sha256": "a" * 64,
                        "plain_sha256": "b" * 64,
                        "plain_size": 1,
                    },
                    {
                        "name": "db-a.sql.gz.gpg",
                        "kind": "database",
                        "size": 1,
                        "sha256": "a" * 64,
                        "plain_sha256": "b" * 64,
                        "plain_size": 1,
                        "database": "a",
                    },
                ],
            }
        )
    )
    steps = [str(s) for s in plan_restore(manifest, RestoreOptions(only_databases=["other"]))]
    assert "decrypt: gpg" in steps
    assert not any(s.startswith("database") for s in steps)
    assert any(s.startswith("files: extract into /nonexistent/a.example") for s in steps)


def test_dry_run_plan_from_backup_runner(server):
    cfg = make_config(server, encryption={"method": "gpg", "gpg_recipient": "x@example.com"})
    runner = FakeRunner()
    sites = discover_sites(cfg, runner)
    steps = BackupRunner(cfg, runner, LocalStorage(Path("/nonexistent")), "vps1").plan(sites[0])
    assert any("dump database shop_wp" in s for s in steps)
    assert any("encrypt with gpg" in s for s in steps)
