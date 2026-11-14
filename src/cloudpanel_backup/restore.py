"""Restoring a backup: download, verify checksums, decrypt, extract files and import databases."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cloudpanel_backup.appconfig import DbCredentials
from cloudpanel_backup.archive import extract_archive
from cloudpanel_backup.catalog import BackupSet
from cloudpanel_backup.config import Config
from cloudpanel_backup.crypto import Encryptor
from cloudpanel_backup.database import import_database, resolve_credentials
from cloudpanel_backup.discovery import master_credentials
from cloudpanel_backup.manifest import Manifest, ManifestEntry
from cloudpanel_backup.runner import CommandRunner
from cloudpanel_backup.storage.base import Storage
from cloudpanel_backup.util import format_timestamp, sha256_file, utcnow

log = logging.getLogger(__name__)


class RestoreError(RuntimeError):
    pass


class ChecksumError(RestoreError):
    pass


@dataclass
class RestoreOptions:
    files: bool = True
    databases: bool = True
    target_dir: Path | None = None
    db_map: dict[str, str] = field(default_factory=dict)
    only_databases: list[str] = field(default_factory=list)
    dry_run: bool = False
    force: bool = False
    chown: bool = True
    identity_file: Path | None = None
    keep_previous: bool = True


@dataclass
class RestoreStep:
    action: str
    detail: str

    def __str__(self) -> str:
        return f"{self.action}: {self.detail}"


def plan_restore(manifest: Manifest, options: RestoreOptions) -> list[RestoreStep]:
    steps: list[RestoreStep] = []
    site = manifest.site
    target = options.target_dir or Path(str(site.get("site_dir", "")))
    for entry in manifest.entries:
        steps.append(RestoreStep("download", f"{entry.name} ({entry.size} bytes, sha256 {entry.sha256[:12]}...)"))
    if manifest.encryption != "none":
        steps.append(RestoreStep("decrypt", f"{manifest.encryption}"))
    if options.files and manifest.by_kind("files"):
        if target.exists():
            what = "move aside to " + f"{target.name}.before-restore-<time>" if options.keep_previous else "replace"
            steps.append(RestoreStep("files", f"extract into {target} (existing directory: {what})"))
        else:
            steps.append(RestoreStep("files", f"extract into {target}"))
        if options.chown and site.get("user"):
            steps.append(RestoreStep("chown", f"{site['user']}:{site['user']} {target}"))
    if options.databases:
        for entry in manifest.by_kind("database"):
            name = entry.database or ""
            if options.only_databases and name not in options.only_databases:
                continue
            dest = options.db_map.get(name, name)
            steps.append(RestoreStep("database", f"import {entry.name} into {dest} (existing tables are replaced)"))
    vhosts = manifest.by_kind("vhost")
    if vhosts:
        steps.append(RestoreStep("vhost", f"{vhosts[0].name} is downloaded next to the restore; review it by hand"))
    return steps


def verify_file(path: Path, expected: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise ChecksumError(f"{label}: checksum mismatch (expected {expected[:16]}..., got {actual[:16]}...)")


class Restorer:
    def __init__(self, config: Config, runner: CommandRunner, storage: Storage) -> None:
        self.config = config
        self.runner = runner
        self.storage = storage
        self.encryptor = Encryptor(config.encryption, runner)

    def fetch(
        self, backup: BackupSet, entry: ManifestEntry, workdir: Path, manifest: Manifest, identity: Path | None
    ) -> Path:
        local = workdir / entry.name
        self.storage.get_file(backup.object_key(entry.name), local)
        verify_file(local, entry.sha256, entry.name)
        plain = self.encryptor.decrypt(local, manifest.encryption, identity)
        verify_file(plain, entry.plain_sha256, f"{entry.name} (decrypted)")
        return plain

    def restore(self, backup: BackupSet, options: RestoreOptions) -> list[RestoreStep]:
        manifest = backup.load_manifest(self.storage)
        steps = plan_restore(manifest, options)
        if options.dry_run:
            return steps
        site = manifest.site
        target = options.target_dir or Path(str(site.get("site_dir", "")))
        if options.files and manifest.by_kind("files") and not str(target):
            raise RestoreError("the manifest has no site_dir; pass --target")
        staging_root = self.config.backup.staging_dir
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=staging_root) as tmp:
            workdir = Path(tmp)
            if options.files:
                for entry in manifest.by_kind("files"):
                    archive = self.fetch(backup, entry, workdir, manifest, options.identity_file)
                    self._restore_files(archive, target, options, site.get("user"))
                for entry in manifest.by_kind("vhost"):
                    plain = self.fetch(backup, entry, workdir, manifest, options.identity_file)
                    keep = target.parent / f"{target.name}.vhost-{backup.timestamp}.conf"
                    shutil.copyfile(plain, keep)
                    log.info("vhost saved as %s (not activated)", keep)
            if options.databases:
                master: DbCredentials | None = None
                for entry in manifest.by_kind("database"):
                    name = entry.database or ""
                    if options.only_databases and name not in options.only_databases:
                        continue
                    dump = self.fetch(backup, entry, workdir, manifest, options.identity_file)
                    dest = options.db_map.get(name, name)
                    if master is None and not self.config.database.user:
                        master = master_credentials(self.runner)
                    creds = self._credentials_for(dest, target)
                    creds = resolve_credentials(creds, self.config.database, master)
                    method = str(entry.details.get("method", self.config.backup.db_method))
                    import_database(self.runner, creds, dump, self.config.database, method=method)
                    log.info("imported %s into %s", entry.name, dest)
        return steps

    def _credentials_for(self, database: str, target: Path) -> DbCredentials:
        from cloudpanel_backup.appconfig import detect_credentials

        if target.is_dir():
            _, found = detect_credentials(target)
            for creds in found:
                if creds.name == database:
                    return creds
        return DbCredentials(name=database, source="restore")

    def _restore_files(self, archive: Path, target: Path, options: RestoreOptions, user: object) -> None:
        stamp = format_timestamp(utcnow())
        incoming = target.parent / f".{target.name}.restore-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        count = extract_archive(archive, incoming)
        log.info("extracted %d entries", count)
        if target.exists():
            if not options.force and any(target.iterdir()):
                shutil.rmtree(incoming, ignore_errors=True)
                raise RestoreError(f"{target} is not empty; pass --force to replace it")
            if options.keep_previous:
                previous = target.parent / f"{target.name}.before-restore-{stamp}"
                os.replace(target, previous)
                log.info("previous files moved to %s", previous)
            else:
                shutil.rmtree(target)
        os.replace(incoming, target)
        if options.chown and isinstance(user, str) and user and os.geteuid() == 0:
            self.runner.run(["chown", "-R", "--no-dereference", f"{user}:{user}", str(target)])
