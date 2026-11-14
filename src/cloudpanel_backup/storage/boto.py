"""S3 storage through boto3 (``pip install "cloudpanel-backup[s3]"``), for setups that need its credential chain."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cloudpanel_backup.config import ConfigError, StorageSettings
from cloudpanel_backup.storage.base import ObjectInfo, StorageError


class Boto3Storage:
    def __init__(self, client: Any, bucket: str, storage_class: str | None = None) -> None:
        self.client = client
        self.bucket = bucket
        self.storage_class = storage_class

    @classmethod
    def from_settings(cls, s: StorageSettings) -> Boto3Storage:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ConfigError('backend = "boto3" needs boto3: pip install "cloudpanel-backup[s3]"') from exc
        kwargs: dict[str, Any] = {}
        if s.endpoint:
            kwargs["endpoint_url"] = s.endpoint
        if s.region and s.region != "auto":
            kwargs["region_name"] = s.region
        elif s.endpoint:
            kwargs["region_name"] = "auto"
        # Fall back to boto3's own credential chain (instance roles, ~/.aws) when the variables are not set.
        if os.environ.get(s.access_key_env) and os.environ.get(s.secret_key_env):
            kwargs["aws_access_key_id"] = os.environ[s.access_key_env]
            kwargs["aws_secret_access_key"] = os.environ[s.secret_key_env]
        assert s.bucket is not None
        return cls(boto3.client("s3", **kwargs), s.bucket, s.storage_class)

    def describe(self) -> str:
        return f"s3://{self.bucket} (boto3)"

    def _extra(self) -> dict[str, Any]:
        return {"StorageClass": self.storage_class} if self.storage_class else {}

    def put_file(self, local: Path, key: str, sha256: str | None = None) -> None:
        self.client.upload_file(str(local), self.bucket, key, ExtraArgs=self._extra() or None)

    def put_bytes(self, data: bytes, key: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **self._extra())

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, str(local))
        except Exception as exc:
            raise StorageError(f"could not fetch {key}: {exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list(self, prefix: str) -> list[ObjectInfo]:
        prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                out.append(ObjectInfo(item["Key"], int(item["Size"])))
        return sorted(out, key=lambda o: o.key)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def stat(self, key: str) -> ObjectInfo | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        return ObjectInfo(key, int(head["ContentLength"]))
