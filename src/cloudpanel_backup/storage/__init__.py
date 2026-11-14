"""Storage backends: where backups are written to and read from."""

from __future__ import annotations

from cloudpanel_backup.config import Config, ConfigError
from cloudpanel_backup.http import Transport
from cloudpanel_backup.runner import CommandRunner
from cloudpanel_backup.storage.base import ObjectInfo, Storage, StorageError
from cloudpanel_backup.storage.local import LocalStorage
from cloudpanel_backup.storage.rsync import RsyncStorage
from cloudpanel_backup.storage.s3 import S3Storage

__all__ = ["LocalStorage", "ObjectInfo", "RsyncStorage", "S3Storage", "Storage", "StorageError", "make_storage"]


def make_storage(config: Config, runner: CommandRunner, transport: Transport | None = None) -> Storage:
    s = config.storage
    if s.type == "local":
        assert s.path is not None
        return LocalStorage(s.path)
    if s.type == "rsync":
        assert s.target is not None
        return RsyncStorage(s.target, runner, ssh_port=s.ssh_port, ssh_key=s.ssh_key, extra_options=s.rsync_options)
    if s.type == "s3":
        if s.backend == "boto3":
            from cloudpanel_backup.storage.boto import Boto3Storage

            return Boto3Storage.from_settings(s)
        return S3Storage.from_settings(s, transport=transport)
    raise ConfigError(f"unknown storage type {s.type!r}")
