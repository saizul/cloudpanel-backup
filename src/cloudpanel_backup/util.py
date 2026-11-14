"""Small helpers shared by the other modules."""

from __future__ import annotations

import hashlib
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    if not _TIMESTAMP_RE.match(value):
        raise ValueError(f"not a backup timestamp: {value!r}")
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def is_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP_RE.match(value))


def safe_name(value: str) -> str:
    """Turn a domain or database name into something safe for a file name or object key segment."""
    cleaned = _SAFE_NAME_RE.sub("_", value.strip()).strip("._")
    if not cleaned:
        raise ValueError(f"cannot build a safe name from {value!r}")
    return cleaned


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover


def hostname() -> str:
    return safe_name(socket.gethostname().split(".")[0] or "server")


def write_private(path: Path, content: str) -> None:
    """Write a file that only the current user can read (mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def join_key(*parts: str) -> str:
    """Join object key segments with single slashes."""
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))
