from pathlib import Path

import pytest

from cloudpanel_backup.config import ConfigError, EncryptionSettings
from cloudpanel_backup.crypto import Encryptor
from conftest import FakeRunner


def test_age_roundtrip(tmp_path: Path):
    runner = FakeRunner()
    enc = Encryptor(
        EncryptionSettings(method="age", recipients=["age1aaa", "age1bbb"], identity_file=tmp_path / "id.txt"), runner
    )
    plain = tmp_path / "files.tar.gz"
    plain.write_bytes(b"secret data")
    stored = enc.encrypt(plain)
    assert stored.name == "files.tar.gz.age" and not plain.exists()
    args = runner.calls[0][0]
    assert args[:2] == ["age", "--encrypt"]
    assert args.count("--recipient") == 2
    back = enc.decrypt(stored, "age")
    assert back.read_bytes() == b"secret data"
    assert "--identity" in runner.calls[1][0]


def test_age_restore_needs_identity(tmp_path: Path):
    enc = Encryptor(EncryptionSettings(method="age", recipients=["age1"]), FakeRunner())
    f = tmp_path / "x.age"
    f.write_bytes(b"FAKEAGE\n")
    with pytest.raises(ConfigError):
        enc.decrypt(f, "age")


def test_gpg_roundtrip_with_passphrase(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CPB_GPG_PASS", "hunter2")
    runner = FakeRunner()
    enc = Encryptor(
        EncryptionSettings(
            method="gpg",
            gpg_recipient="backup@example.com",
            gpg_homedir=tmp_path / "gnupg",
            gpg_passphrase_env="CPB_GPG_PASS",
        ),
        runner,
    )
    plain = tmp_path / "db.sql.gz"
    plain.write_bytes(b"dump")
    stored = enc.encrypt(plain)
    assert stored.suffix == ".gpg"
    enc_args = runner.calls[0][0]
    assert enc_args[enc_args.index("--recipient") + 1] == "backup@example.com"
    assert enc.decrypt(stored, "gpg").read_bytes() == b"dump"
    dec_args = runner.calls[1][0]
    assert "--passphrase-file" in dec_args and "hunter2" not in dec_args
    assert "--homedir" in dec_args


def test_disabled_and_missing_program(tmp_path: Path):
    enc = Encryptor(EncryptionSettings(), FakeRunner())
    f = tmp_path / "a"
    f.write_text("x")
    assert enc.encrypt(f) == f and not enc.enabled
    assert enc.decrypt(f, "none") == f
    with pytest.raises(ConfigError):
        Encryptor(EncryptionSettings(method="age", recipients=["a"]), FakeRunner(programs=[])).check_available()
    with pytest.raises(ValueError):
        Encryptor(EncryptionSettings(method="gpg", gpg_recipient="x"), FakeRunner()).decrypt(f, "gpg")
