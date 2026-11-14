"""Finding backups in storage. Layout: ``<prefix>/<host>/<domain>/<timestamp>/<files>``."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from cloudpanel_backup.manifest import MANIFEST_NAME, Manifest
from cloudpanel_backup.storage.base import ObjectInfo, Storage
from cloudpanel_backup.util import is_timestamp, join_key, parse_timestamp


@dataclass
class BackupSet:
    host: str
    domain: str
    timestamp: str
    objects: list[ObjectInfo] = field(default_factory=list)
    prefix: str = ""

    @property
    def moment(self) -> datetime:
        return parse_timestamp(self.timestamp)

    @property
    def key(self) -> str:
        return join_key(self.prefix, self.host, self.domain, self.timestamp)

    @property
    def complete(self) -> bool:
        return any(o.key.endswith("/" + MANIFEST_NAME) for o in self.objects)

    @property
    def size(self) -> int:
        return sum(o.size for o in self.objects)

    def object_key(self, name: str) -> str:
        return join_key(self.key, name)

    def load_manifest(self, storage: Storage) -> Manifest:
        return Manifest.from_json(storage.get_bytes(self.object_key(MANIFEST_NAME)).decode("utf-8"))


def list_backups(storage: Storage, prefix: str, host: str | None = None, domain: str | None = None) -> list[BackupSet]:
    """All backup sets below ``prefix`` (optionally one host and/or domain), oldest first."""
    search = join_key(prefix, host or "", domain or "") if host else join_key(prefix)
    groups: dict[tuple[str, str, str], list[ObjectInfo]] = defaultdict(list)
    base_depth = len([p for p in prefix.split("/") if p])
    for obj in storage.list(search):
        parts = obj.key.split("/")
        rel = parts[base_depth:]
        if len(rel) < 4 or not is_timestamp(rel[2]):
            continue
        h, d, ts = rel[0], rel[1], rel[2]
        if (host and h != host) or (domain and d != domain):
            continue
        groups[(h, d, ts)].append(obj)
    sets = [BackupSet(h, d, ts, objs, prefix) for (h, d, ts), objs in groups.items()]
    return sorted(sets, key=lambda b: (b.host, b.domain, b.timestamp))


def find_backup(storage: Storage, prefix: str, host: str, domain: str, timestamp: str = "latest") -> BackupSet | None:
    complete = [b for b in list_backups(storage, prefix, host, domain) if b.complete]
    if not complete:
        return None
    if timestamp == "latest":
        return complete[-1]
    for backup in complete:
        if backup.timestamp == timestamp:
            return backup
    return None
