"""The backup run: archive files, dump databases, encrypt, upload, write the manifest, apply retention."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cloudpanel_backup._version import __version__
from cloudpanel_backup.appconfig import DbCredentials
from cloudpanel_backup.archive import create_archive
from cloudpanel_backup.catalog import list_backups
from cloudpanel_backup.config import Config
from cloudpanel_backup.crypto import Encryptor
from cloudpanel_backup.database import dump_database, resolve_credentials
from cloudpanel_backup.discovery import Site, master_credentials
from cloudpanel_backup.manifest import MANIFEST_NAME, Manifest, ManifestEntry
from cloudpanel_backup.retention import plan_retention, stale_incomplete
from cloudpanel_backup.runner import CommandRunner
from cloudpanel_backup.storage.base import Storage
from cloudpanel_backup.util import format_timestamp, human_size, join_key, safe_name, sha256_file, utcnow

log = logging.getLogger(__name__)


@dataclass
class SiteResult:
    domain: str
    ok: bool
    timestamp: str
    size: int = 0
    seconds: float = 0.0
    entries: list[str] = field(default_factory=list)
    message: str = ""
    pruned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "ok": self.ok,
            "timestamp": self.timestamp,
            "size": self.size,
            "seconds": round(self.seconds, 1),
            "entries": self.entries,
            "message": self.message,
            "pruned": self.pruned,
        }


@dataclass
class RunReport:
    host: str
    results: list[SiteResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def summary(self) -> str:
        good = sum(1 for r in self.results if r.ok)
        size = sum(r.size for r in self.results if r.ok)
        return f"{good}/{len(self.results)} site(s) backed up, {human_size(size)} uploaded"


class BackupRunner:
    def __init__(
        self,
        config: Config,
        runner: CommandRunner,
        storage: Storage,
        host: str,
        encryptor: Encryptor | None = None,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.storage = storage
        self.host = host
        self.encryptor = encryptor or Encryptor(config.encryption, runner)
        self.now = now
        self._master: DbCredentials | None = None
        self._master_loaded = False

    def _master_credentials(self) -> DbCredentials | None:
        if not self._master_loaded:
            self._master_loaded = True
            if self.config.database.use_master_credentials and not self.config.database.user:
                self._master = master_credentials(self.runner)
        return self._master

    def plan(self, site: Site) -> list[str]:
        steps = [f"archive {site.site_dir} (excluding {len(self.config.backup.all_excludes(site.excludes))} patterns)"]
        if site.vhost_path:
            steps.append(f"copy vhost {site.vhost_path}")
        for db in site.databases:
            steps.append(f"dump database {db.name} with {self.config.backup.db_method}")
        if self.encryptor.enabled:
            steps.append(f"encrypt with {self.encryptor.method}")
        steps.append(f"upload to {self.storage.describe()}")
        return steps

    def run(self, sites: list[Site], prune: bool = True) -> RunReport:
        report = RunReport(host=self.host)
        if self.encryptor.enabled:
            self.encryptor.check_available()
        for site in sites:
            report.results.append(self.backup_site(site, prune=prune))
        return report

    def _finish_entry(self, path: Path, kind: str, database: str | None = None, **details: object) -> ManifestEntry:
        plain_sha = sha256_file(path)
        plain_size = path.stat().st_size
        stored = self.encryptor.encrypt(path)
        return ManifestEntry(
            name=stored.name,
            kind=kind,
            size=stored.stat().st_size,
            sha256=sha256_file(stored) if stored != path else plain_sha,
            plain_sha256=plain_sha,
            plain_size=plain_size,
            database=database,
            details=dict(details),
        )

    def backup_site(self, site: Site, prune: bool = True) -> SiteResult:
        started = time.monotonic()
        stamp = format_timestamp(self.now or utcnow())
        result = SiteResult(domain=site.domain, ok=False, timestamp=stamp)
        settings = self.config.backup
        staging = settings.staging_dir / safe_name(site.domain) / stamp
        prefix = join_key(settings.prefix, self.host, site.domain, stamp)
        log.info("%s: starting backup %s", site.domain, stamp)
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True, mode=0o700)
            manifest = Manifest(
                domain=site.domain,
                host=self.host,
                timestamp=stamp,
                tool_version=__version__,
                encryption=self.config.encryption.method,
                site=site.to_dict(),
            )
            files_path = staging / "files.tar.gz"
            stats = create_archive(
                site.site_dir,
                files_path,
                settings.all_excludes(site.excludes),
                compression_level=settings.compression_level,
            )
            manifest.entries.append(
                self._finish_entry(
                    files_path, "files", files=stats.files, excluded=stats.excluded, bytes_in=stats.bytes_in
                )
            )
            log.info(
                "%s: archived %d files (%s), %d excluded",
                site.domain,
                stats.files,
                human_size(stats.bytes_in),
                stats.excluded,
            )
            if site.vhost_path and site.vhost_path.is_file():
                vhost_copy = staging / "vhost.conf"
                shutil.copyfile(site.vhost_path, vhost_copy)
                manifest.entries.append(self._finish_entry(vhost_copy, "vhost", source=str(site.vhost_path)))
            for db in site.databases:
                master = None if (db.user or self.config.database.user) else self._master_credentials()
                creds = resolve_credentials(db, self.config.database, master)
                dump_path = staging / f"db-{safe_name(db.name)}.sql.gz"
                dump_database(
                    self.runner,
                    creds,
                    dump_path,
                    self.config.database,
                    method=settings.db_method,
                    compression_level=settings.compression_level,
                )
                manifest.entries.append(
                    self._finish_entry(dump_path, "database", database=db.name, method=settings.db_method)
                )
                log.info("%s: dumped database %s", site.domain, db.name)
            for entry in manifest.entries:
                self.storage.put_file(staging / entry.name, join_key(prefix, entry.name), entry.sha256)
            # The manifest goes last: its presence marks the backup as complete.
            self.storage.put_bytes(manifest.to_json().encode("utf-8"), join_key(prefix, MANIFEST_NAME))
            result.ok = True
            result.size = manifest.total_size
            result.entries = [e.name for e in manifest.entries]
            result.message = human_size(result.size)
            if prune:
                result.pruned = self.prune_site(site.domain)
        except Exception as exc:
            log.error("%s: backup failed: %s", site.domain, exc)
            result.message = str(exc)
        finally:
            if not settings.keep_staging and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            result.seconds = time.monotonic() - started
        return result

    def prune_site(self, domain: str, dry_run: bool = False) -> list[str]:
        """Apply the retention policy to one site's backups. Returns the deleted timestamps."""
        sets = list_backups(self.storage, self.config.backup.prefix, self.host, domain)
        complete = [b for b in sets if b.complete]
        plan = plan_retention([b.moment for b in complete], self.config.retention)
        doomed = [b for b in complete if b.moment in plan.delete]
        stale = set(stale_incomplete([b.moment for b in sets if not b.complete], self.now or utcnow()))
        doomed += [b for b in sets if not b.complete and b.moment in stale]
        deleted = []
        for backup in sorted(doomed, key=lambda b: b.timestamp):
            log.info("%s: pruning %s%s", domain, backup.timestamp, " (dry run)" if dry_run else "")
            if not dry_run:
                # delete the manifest first so a half-deleted backup is seen as incomplete
                for obj in sorted(backup.objects, key=lambda o: not o.key.endswith(MANIFEST_NAME)):
                    self.storage.delete(obj.key)
            deleted.append(backup.timestamp)
        return deleted
