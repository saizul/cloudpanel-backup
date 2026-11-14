import gzip
from datetime import datetime, timezone

from cloudpanel_backup.backup import BackupRunner
from cloudpanel_backup.catalog import find_backup
from cloudpanel_backup.crypto import Encryptor
from cloudpanel_backup.discovery import discover_sites
from cloudpanel_backup.storage import LocalStorage
from cloudpanel_backup.verify import verify_backup
from conftest import FakeRunner, make_config

NOW = datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)


def setup(server, **sections):
    cfg = make_config(server, backup={"sites": ["shop.*"]}, **sections)
    store = server["root"] / "store"
    storage = LocalStorage(store)
    runner = FakeRunner()
    BackupRunner(cfg, runner, storage, "vps1", now=NOW).run(discover_sites(cfg, runner))
    return cfg, store, storage, find_backup(storage, "cloudpanel", "vps1", "shop.example.com")


def test_quick_and_deep_verify_pass(server):
    _, _, storage, backup = setup(server)
    quick = verify_backup(storage, backup)
    assert quick.ok and quick.checked == 3
    deep = verify_backup(storage, backup, deep=True)
    assert deep.ok, deep.problems


def test_missing_object_and_size_mismatch(server):
    _, store, storage, backup = setup(server)
    (store / backup.object_key("vhost.conf")).unlink()
    (store / backup.object_key("files.tar.gz")).write_bytes(b"short")
    backup = find_backup(storage, "cloudpanel", "vps1", "shop.example.com")
    result = verify_backup(storage, backup)
    assert not result.ok
    assert any("missing" in p for p in result.problems)
    assert any("size" in p for p in result.problems)


def test_deep_detects_corruption_with_same_size(server):
    _, store, storage, backup = setup(server)
    path = store / backup.object_key("db-shop_wp.sql.gz")
    data = bytearray(path.read_bytes())
    data[-5] ^= 0xFF
    path.write_bytes(bytes(data))
    assert verify_backup(storage, backup).ok  # sizes still match
    deep = verify_backup(storage, backup, deep=True)
    assert not deep.ok and "checksum mismatch" in deep.problems[0]


def test_deep_detects_damaged_gzip_even_with_matching_checksum(server, tmp_path):
    from cloudpanel_backup.verify import _test_plaintext

    bad = tmp_path / "x.sql.gz"
    bad.write_bytes(gzip.compress(b"data")[:-6])
    assert "damaged" in _test_plaintext(bad, "database")


def test_encrypted_backup_deep_verify(server, tmp_path):
    cfg, _, storage, backup = setup(
        server, encryption={"method": "age", "recipients": ["age1x"], "identity_file": str(tmp_path / "id")}
    )
    result = verify_backup(storage, backup, deep=True, encryptor=Encryptor(cfg.encryption, FakeRunner()))
    assert result.ok, result.problems
    # without an identity the stored checksums are still verified
    cfg.encryption.identity_file = None
    result = verify_backup(storage, backup, deep=True, encryptor=Encryptor(cfg.encryption, FakeRunner()))
    assert result.ok


def test_incomplete_and_bad_manifest(server):
    _, store, storage, backup = setup(server)
    (store / backup.object_key("manifest.json")).write_text("{not json")
    result = verify_backup(storage, backup)
    assert not result.ok and "manifest unreadable" in result.problems[0]
    (store / backup.object_key("manifest.json")).unlink()
    from cloudpanel_backup.catalog import list_backups

    incomplete = list_backups(storage, "cloudpanel", "vps1", "shop.example.com")[0]
    assert not verify_backup(storage, incomplete).ok
