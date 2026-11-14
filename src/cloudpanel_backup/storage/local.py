"""A directory on this server or a mounted volume (NFS, a second disk, a Storage Box mounted with sshfs)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from cloudpanel_backup.storage.base import ObjectInfo, StorageError, check_key


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> str:
        return f"local:{self.root}"

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def put_file(self, local: Path, key: str, sha256: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        os.close(fd)
        try:
            shutil.copyfile(local, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put_bytes(self, data: bytes, key: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_name(f".upload-{target.name}")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)

    def get_file(self, key: str, local: Path) -> None:
        source = self._path(key)
        if not source.is_file():
            raise StorageError(f"{key} not found in {self.describe()}")
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, local)

    def get_bytes(self, key: str) -> bytes:
        source = self._path(key)
        if not source.is_file():
            raise StorageError(f"{key} not found in {self.describe()}")
        return source.read_bytes()

    def list(self, prefix: str) -> list[ObjectInfo]:
        base = self.root / prefix.strip("/") if prefix.strip("/") else self.root
        if not base.exists():
            return []
        out = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith(".upload-"):
                out.append(ObjectInfo(path.relative_to(self.root).as_posix(), path.stat().st_size))
        return out

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        # remove empty parent directories up to the root
        parent = path.parent
        while parent != self.root and parent.is_relative_to(self.root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def stat(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        return ObjectInfo(key, path.stat().st_size) if path.is_file() else None
