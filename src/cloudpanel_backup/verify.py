"""Checking that backups in storage are complete and intact."""

from __future__ import annotations

import gzip
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cloudpanel_backup.catalog import BackupSet
from cloudpanel_backup.crypto import Encryptor
from cloudpanel_backup.manifest import ManifestError
from cloudpanel_backup.storage.base import Storage, StorageError
from cloudpanel_backup.util import sha256_file


@dataclass
class VerifyResult:
    domain: str
    timestamp: str
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    checked: int = 0

    def fail(self, message: str) -> None:
        self.ok = False
        self.problems.append(message)

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "timestamp": self.timestamp,
            "ok": self.ok,
            "checked": self.checked,
            "problems": self.problems,
            "message": "; ".join(self.problems),
        }


def _test_plaintext(path: Path, kind: str) -> str | None:
    """Read the whole file to make sure gzip (and tar) are intact."""
    try:
        if kind == "files":
            with tarfile.open(path, "r:gz") as tar:
                for _member in tar:
                    pass
        elif path.name.endswith(".gz"):
            with gzip.open(path, "rb") as handle:
                while handle.read(1024 * 1024):
                    pass
    except (OSError, EOFError, tarfile.TarError) as exc:
        return f"{path.name}: archive is damaged ({exc})"
    return None


def verify_backup(
    storage: Storage,
    backup: BackupSet,
    deep: bool = False,
    encryptor: Encryptor | None = None,
    identity: Path | None = None,
    workdir: Path | None = None,
) -> VerifyResult:
    """Quick mode compares the manifest with the stored object sizes; deep mode downloads and hashes every file
    (and, when the identity is available, decrypts it and tests the gzip/tar stream)."""
    result = VerifyResult(backup.domain, backup.timestamp)
    if not backup.complete:
        result.fail("no manifest: the backup is incomplete")
        return result
    try:
        manifest = backup.load_manifest(storage)
    except (ManifestError, StorageError, UnicodeDecodeError) as exc:
        result.fail(f"manifest unreadable: {exc}")
        return result
    sizes = {o.key: o.size for o in backup.objects}
    for entry in manifest.entries:
        key = backup.object_key(entry.name)
        if key not in sizes:
            result.fail(f"{entry.name}: missing from storage")
            continue
        if sizes[key] != entry.size:
            result.fail(f"{entry.name}: size {sizes[key]} != {entry.size} in manifest")
            continue
        result.checked += 1
    if not deep or not result.ok:
        return result
    with tempfile.TemporaryDirectory(prefix="cpb-verify-", dir=workdir) as tmp:
        for entry in manifest.entries:
            local = Path(tmp) / entry.name
            try:
                storage.get_file(backup.object_key(entry.name), local)
            except StorageError as exc:
                result.fail(f"{entry.name}: download failed: {exc}")
                continue
            if sha256_file(local) != entry.sha256:
                result.fail(f"{entry.name}: checksum mismatch")
                continue
            plain = local
            if manifest.encryption != "none":
                if encryptor is None or (
                    manifest.encryption == "age" and not (identity or encryptor.settings.identity_file)
                ):
                    local.unlink()
                    continue  # stored checksum verified; cannot look inside without the key
                plain = encryptor.decrypt(local, manifest.encryption, identity)
                if sha256_file(plain) != entry.plain_sha256:
                    result.fail(f"{entry.name}: decrypted checksum mismatch")
                    continue
            problem = _test_plaintext(plain, entry.kind)
            if problem:
                result.fail(problem)
            plain.unlink(missing_ok=True)
    return result
