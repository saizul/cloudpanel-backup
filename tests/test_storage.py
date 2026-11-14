from pathlib import Path

import pytest

from cloudpanel_backup.config import StorageSettings, parse_config
from cloudpanel_backup.http import HttpResponse
from cloudpanel_backup.storage import LocalStorage, RsyncStorage, S3Storage, StorageError, make_storage
from cloudpanel_backup.storage.base import check_key
from cloudpanel_backup.storage.rsync import parse_target
from conftest import ACCESS_KEY, SECRET_KEY, FakeRunner, FakeS3


def s3(fake: FakeS3, **kwargs) -> S3Storage:
    return S3Storage(
        "backups",
        ACCESS_KEY,
        SECRET_KEY,
        endpoint="https://acct.r2.cloudflarestorage.com",
        transport=fake,
        retry_delay=0,
        **kwargs,
    )


def exercise(storage, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    storage.put_file(src, "p/h/d/20261114T000000Z/files.tar.gz")
    storage.put_bytes(b'{"a": 1}', "p/h/d/20261114T000000Z/manifest.json")
    storage.put_bytes(b"x", "p/h/other/20261114T000000Z/manifest.json")
    listed = storage.list("p/h/d")
    assert [(o.key, o.size) for o in listed] == [
        ("p/h/d/20261114T000000Z/files.tar.gz", 11),
        ("p/h/d/20261114T000000Z/manifest.json", 8),
    ]
    assert len(storage.list("p")) == 3
    out = tmp_path / "out" / "f"
    storage.get_file("p/h/d/20261114T000000Z/files.tar.gz", out)
    assert out.read_bytes() == b"hello world"
    assert storage.get_bytes("p/h/d/20261114T000000Z/manifest.json") == b'{"a": 1}'
    assert storage.stat("p/h/d/20261114T000000Z/files.tar.gz").size == 11
    storage.delete("p/h/d/20261114T000000Z/files.tar.gz")
    assert storage.stat("p/h/d/20261114T000000Z/files.tar.gz") is None
    assert storage.list("nothing-here") == []


def test_local_storage(tmp_path: Path):
    storage = LocalStorage(tmp_path / "store")
    exercise(storage, tmp_path)
    assert (tmp_path / "store" / "p/h/d/20261114T000000Z/manifest.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(StorageError):
        storage.get_file("missing", tmp_path / "x")


def test_s3_storage(tmp_path: Path):
    fake = FakeS3(page_size=2)
    exercise(s3(fake), tmp_path)
    # the 3-object listing needed two pages
    assert any("continuation-token" in url for _, url in fake.requests)


def test_s3_multipart_upload(tmp_path: Path):
    fake = FakeS3()
    storage = s3(fake)
    storage.multipart_threshold = 5 * 1024 * 1024
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * (5 * 1024 * 1024) + b"b" * 1000)
    storage.put_file(big, "k/big.bin")
    assert fake.objects["k/big.bin"] == big.read_bytes()
    methods = [m for m, _ in fake.requests]
    assert methods.count("PUT") == 2 and methods.count("POST") == 2


def test_s3_multipart_aborts_on_error(tmp_path: Path):
    fake = FakeS3()
    storage = s3(fake, retries=0)
    storage.multipart_threshold = 5 * 1024 * 1024
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    original = fake.request

    def failing(method, url, headers, body=None, sink=None):
        if method == "PUT" and "partNumber=2" in url:
            original(method, url, headers, body, sink)
            return HttpResponse(400, {}, b"<Error><Code>EntityTooSmall</Code><Message>no</Message></Error>")
        return original(method, url, headers, body, sink)

    fake.request = failing  # type: ignore[method-assign]
    with pytest.raises(StorageError, match="EntityTooSmall"):
        storage.put_file(big, "k/big.bin")
    assert fake.uploads == {}
    assert fake.requests[-1][0] == "DELETE"


def test_s3_retries_server_errors(tmp_path: Path):
    fake = FakeS3()
    fake.fail_next = [503, 500]
    storage = s3(fake)
    storage.put_bytes(b"data", "a/b")
    assert fake.objects["a/b"] == b"data"


def test_s3_error_message_and_no_retry_on_4xx():
    fake = FakeS3()
    fake.fail_next = [403]
    with pytest.raises(StorageError, match="HTTP 403 InternalError"):
        s3(fake).put_bytes(b"x", "a")
    assert len(fake.requests) == 1


def test_s3_urls_and_endpoint_rules():
    vhost = S3Storage("bkt", "a", "b", region="eu-central-1")
    assert vhost._url("x/y z") == "https://bkt.s3.eu-central-1.amazonaws.com/x/y%20z"
    path = S3Storage("bkt", "a", "b", endpoint="https://s3.us-west-004.backblazeb2.com/", region="us-west-004")
    assert path._url("k") == "https://s3.us-west-004.backblazeb2.com/bkt/k"
    with pytest.raises(StorageError):
        S3Storage("bkt", "a", "b", endpoint="http://s3.example.com")
    assert S3Storage("bkt", "a", "b", endpoint="http://127.0.0.1:9000").endpoint == "http://127.0.0.1:9000"


def test_rsync_storage_remote(tmp_path: Path):
    runner = FakeRunner(remote_root=tmp_path / "remote")
    storage = RsyncStorage("backup@203.0.113.10:/srv/backups", runner, ssh_port=2222, ssh_key=Path("/root/.ssh/k"))
    exercise(storage, tmp_path)
    put = runner.commands("rsync")[0]
    assert "--mkpath" in put
    assert put[put.index("-e") + 1] == "ssh -o BatchMode=yes -p 2222 -i /root/.ssh/k"
    assert put[-1] == "backup@203.0.113.10:/srv/backups/p/h/d/20261114T000000Z/files.tar.gz"
    rm = runner.commands("ssh")[0]
    assert rm[-4:] == ["rm", "-f", "--", "/srv/backups/p/h/d/20261114T000000Z/files.tar.gz"]


def test_rsync_list_missing_directory(tmp_path: Path):
    storage = RsyncStorage("backup@203.0.113.10:/srv/backups", FakeRunner(remote_root=tmp_path))
    assert storage.list("p/h") == []


def test_parse_target():
    assert parse_target("u@h:/a/b/") == ("u@h", "/a/b")
    assert parse_target("/mnt/backup/") == (None, "/mnt/backup")
    assert parse_target("host:") == ("host", ".")


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "./a"])
def test_check_key_rejects_traversal(key):
    with pytest.raises(StorageError):
        check_key(key)


def test_make_storage(tmp_path: Path, s3_env):
    cfg = parse_config({"storage": {"type": "s3", "bucket": "backups", "endpoint": "https://x.example"}})
    assert isinstance(make_storage(cfg, FakeRunner()), S3Storage)
    cfg = parse_config({"storage": {"type": "rsync", "target": "h:/x"}})
    assert isinstance(make_storage(cfg, FakeRunner()), RsyncStorage)
    cfg = parse_config({"storage": {"type": "local", "path": str(tmp_path)}})
    assert isinstance(make_storage(cfg, FakeRunner()), LocalStorage)


def test_s3_needs_credentials(monkeypatch):
    monkeypatch.delenv("CPB_S3_ACCESS_KEY_ID", raising=False)
    from cloudpanel_backup.config import ConfigError

    with pytest.raises(ConfigError, match="CPB_S3_ACCESS_KEY_ID"):
        S3Storage.from_settings(StorageSettings(type="s3", bucket="b"))
