"""The backup manifest: what a backup contains, with sizes and SHA-256 checksums.

The manifest is uploaded last, so a backup without one is incomplete (the run failed or is still going).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


class ManifestError(ValueError):
    pass


@dataclass
class ManifestEntry:
    name: str
    kind: str  # "files", "database" or "vhost"
    size: int
    sha256: str
    plain_sha256: str
    plain_size: int
    database: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    domain: str
    host: str
    timestamp: str
    tool_version: str
    encryption: str = "none"
    site: dict[str, Any] = field(default_factory=dict)
    entries: list[ManifestEntry] = field(default_factory=list)
    format_version: int = FORMAT_VERSION

    def entry(self, name: str) -> ManifestEntry:
        for e in self.entries:
            if e.name == name:
                return e
        raise KeyError(name)

    def by_kind(self, kind: str) -> list[ManifestEntry]:
        return [e for e in self.entries if e.kind == kind]

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)

    def to_json(self) -> str:
        data = asdict(self)
        data["tool"] = "cloudpanel-backup"
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("tool") != "cloudpanel-backup":
            raise ManifestError("not a cloudpanel-backup manifest")
        version = data.get("format_version")
        if version != FORMAT_VERSION:
            raise ManifestError(f"unsupported manifest format version {version!r}")
        try:
            entries = [ManifestEntry(**e) for e in data.get("entries", [])]
            for e in entries:
                if len(e.sha256) != 64 or len(e.plain_sha256) != 64:
                    raise ManifestError(f"bad checksum for {e.name}")
                if "/" in e.name or e.name.startswith("."):
                    raise ManifestError(f"bad entry name {e.name!r}")
            return cls(
                domain=data["domain"],
                host=data["host"],
                timestamp=data["timestamp"],
                tool_version=data["tool_version"],
                encryption=data.get("encryption", "none"),
                site=data.get("site", {}),
                entries=entries,
            )
        except (KeyError, TypeError) as exc:
            raise ManifestError(f"incomplete manifest: {exc}") from exc
