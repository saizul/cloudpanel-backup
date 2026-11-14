"""The interface every storage backend implements."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int


class Storage(Protocol):
    def describe(self) -> str: ...

    def put_file(self, local: Path, key: str, sha256: str | None = None) -> None: ...

    def get_file(self, key: str, local: Path) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def put_bytes(self, data: bytes, key: str) -> None: ...

    def list(self, prefix: str) -> list[ObjectInfo]: ...

    def delete(self, key: str) -> None: ...

    def stat(self, key: str) -> ObjectInfo | None: ...


def check_key(key: str) -> str:
    """Refuse keys that could escape a directory on local or rsync targets."""
    if not key or key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
        raise StorageError(f"invalid object key {key!r}")
    return key
