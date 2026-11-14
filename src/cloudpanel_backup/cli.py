"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

from cloudpanel_backup._version import __version__
from cloudpanel_backup.appconfig import parse_dotenv
from cloudpanel_backup.backup import BackupRunner
from cloudpanel_backup.catalog import find_backup, list_backups
from cloudpanel_backup.config import SAMPLE_CONFIG, Config, ConfigError, load_config
from cloudpanel_backup.crypto import Encryptor
from cloudpanel_backup.discovery import Site, discover_sites, is_cloudpanel
from cloudpanel_backup.http import Transport
from cloudpanel_backup.notify import Event, Notifier, SmtpFactory
from cloudpanel_backup.restore import RestoreError, RestoreOptions, Restorer
from cloudpanel_backup.runner import CommandError, CommandRunner, SubprocessRunner
from cloudpanel_backup.schedule import ScheduleOptions, cron_entry, install_systemd, systemd_units
from cloudpanel_backup.storage import StorageError, make_storage
from cloudpanel_backup.util import hostname, human_size, write_private
from cloudpanel_backup.verify import verify_backup

log = logging.getLogger("cloudpanel_backup")

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def load_env_file(path: Path) -> int:
    """Load KEY=VALUE lines into the environment without overriding variables that are already set."""
    if not path.is_file():
        return 0
    mode = path.stat().st_mode & 0o077
    if mode:
        log.warning("%s is readable by other users; chmod 600 it", path)
    count = 0
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudpanel-backup",
        description="Back up and restore CloudPanel sites (files + MariaDB) to S3-compatible storage, "
        "a local directory or rsync.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="config file (default /etc/cloudpanel-backup/config.toml)")
    parser.add_argument("--env-file", type=Path, help="load secrets from this KEY=VALUE file")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more output (-vv for debug)")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--host", help="host name used in object keys (default: [backup] host or the hostname)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("init-config", help="print or write a sample config file")
    p.add_argument("--output", type=Path, help="write to this file (mode 0600) instead of stdout")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")

    p = sub.add_parser("discover", help="list the sites and databases that would be backed up")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("backup", help="back up all (or some) sites")
    p.add_argument("--site", action="append", default=[], help="only this domain (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--no-prune", action="store_true", help="do not apply the retention policy")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list backups in storage")
    p.add_argument("--site")
    p.add_argument("--all-hosts", action="store_true", help="include backups of other servers")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("restore", help="restore a site from a backup")
    p.add_argument("site", help="domain to restore")
    p.add_argument("--timestamp", default="latest", help="backup to use (default: latest)")
    p.add_argument("--from-host", help="restore a backup made on another server")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--files-only", action="store_true")
    group.add_argument("--db-only", action="store_true")
    p.add_argument("--database", action="append", default=[], help="only this database (repeatable)")
    p.add_argument("--target", type=Path, help="restore files here instead of the original site directory")
    p.add_argument("--db-map", action="append", default=[], metavar="OLD=NEW", help="import OLD into database NEW")
    p.add_argument("--identity", type=Path, help="age identity file for encrypted backups")
    p.add_argument("--dry-run", action="store_true", help="only print the restore plan")
    p.add_argument("--force", action="store_true", help="replace a non-empty target directory")
    p.add_argument("--no-keep-previous", action="store_true", help="delete instead of moving existing files aside")
    p.add_argument("--no-chown", action="store_true", help="do not chown restored files to the site user")

    p = sub.add_parser("verify", help="check that backups are complete and intact")
    p.add_argument("--site", action="append", default=[])
    p.add_argument("--all", action="store_true", help="every backup, not just the latest per site")
    p.add_argument("--deep", action="store_true", help="download and hash every file")
    p.add_argument("--identity", type=Path, help="age identity file to test inside encrypted files")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("prune", help="apply the retention policy")
    p.add_argument("--site", action="append", default=[])
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("schedule", help="print or install a systemd timer or cron entry")
    p.add_argument("--cron", action="store_true", help="a /etc/cron.d line instead of systemd units")
    p.add_argument("--on-calendar", default="*-*-* 02:30:00", help="systemd OnCalendar value")
    p.add_argument("--cron-schedule", default="30 2 * * *", help="five cron fields")
    p.add_argument("--executable", default="/usr/local/bin/cloudpanel-backup")
    p.add_argument("--verify-after", action="store_true", help="run 'verify' after each backup")
    p.add_argument("--install", action="store_true", help="write the units to /etc/systemd/system")
    p.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))

    sub.add_parser("test-notify", help="send a test notification to every configured channel")
    return parser


class App:
    def __init__(
        self,
        args: argparse.Namespace,
        out: TextIO,
        runner: CommandRunner,
        transport: Transport | None,
        smtp_factory: SmtpFactory | None,
    ) -> None:
        self.args = args
        self.out = out
        self.runner = runner
        self.transport = transport
        self.smtp_factory = smtp_factory
        self._config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.args.config)
        return self._config

    @property
    def host(self) -> str:
        return self.args.host or self.config.backup.host or hostname()

    def print(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def storage(self):
        return make_storage(self.config, self.runner, self.transport)

    def notifier(self) -> Notifier:
        return Notifier(self.config.notify, self.transport, self.smtp_factory)

    def selected_sites(self, wanted: list[str]) -> list[Site]:
        sites = discover_sites(self.config, self.runner)
        if wanted:
            names = {w.lower() for w in wanted}
            unknown = names - {s.domain for s in sites}
            if unknown:
                raise ConfigError(f"unknown site(s): {', '.join(sorted(unknown))}")
            sites = [s for s in sites if s.domain in names]
        return sites

    # -- commands ------------------------------------------------------------------------------------------

    def cmd_init_config(self) -> int:
        if self.args.output is None:
            self.out.write(SAMPLE_CONFIG)
            return EXIT_OK
        if self.args.output.exists() and not self.args.force:
            raise ConfigError(f"{self.args.output} exists; pass --force to overwrite")
        write_private(self.args.output, SAMPLE_CONFIG)
        self.print(f"wrote {self.args.output}")
        return EXIT_OK

    def cmd_discover(self) -> int:
        sites = discover_sites(self.config, self.runner)
        if self.args.json:
            self.print(
                json.dumps({"cloudpanel": is_cloudpanel(self.runner), "sites": [s.to_dict() for s in sites]}, indent=2)
            )
            return EXIT_OK
        if not is_cloudpanel(self.runner):
            self.print("note: clpctl not found; this does not look like a CloudPanel server")
        if not sites:
            self.print("no sites found")
            return EXIT_OK
        for site in sites:
            dbs = ", ".join(site.database_names) or "-"
            self.print(f"{site.domain:<40} {site.kind:<10} user={site.user:<16} db={dbs}")
            self.print(f"    {site.site_dir}" + (f"  (vhost {site.vhost_path})" if site.vhost_path else ""))
        return EXIT_OK

    def cmd_backup(self) -> int:
        sites = self.selected_sites(self.args.site)
        if not sites:
            self.print("no sites to back up")
            return EXIT_OK
        storage = self.storage()
        runner = BackupRunner(self.config, self.runner, storage, self.host)
        if self.args.dry_run:
            for site in sites:
                self.print(site.domain)
                for step in runner.plan(site):
                    self.print(f"  - {step}")
            return EXIT_OK
        report = runner.run(sites, prune=not self.args.no_prune)
        if self.args.json:
            self.print(
                json.dumps(
                    {"ok": report.ok, "host": report.host, "results": [r.to_dict() for r in report.results]}, indent=2
                )
            )
        else:
            for r in report.results:
                status = "ok" if r.ok else "FAILED"
                extra = f", pruned {len(r.pruned)}" if r.pruned else ""
                self.print(f"{r.domain:<40} {status:<7} {r.message} ({r.seconds:.0f}s{extra})")
            self.print(report.summary())
        if not self.args.no_notify:
            event = Event("backup", report.ok, report.host, report.summary(), [r.to_dict() for r in report.results])
            self.notifier().send(event)
        return EXIT_OK if report.ok else EXIT_FAILED

    def cmd_list(self) -> int:
        host = None if self.args.all_hosts else self.host
        sets = list_backups(self.storage(), self.config.backup.prefix, host, self.args.site)
        if self.args.json:
            self.print(
                json.dumps(
                    [
                        {
                            "host": b.host,
                            "domain": b.domain,
                            "timestamp": b.timestamp,
                            "complete": b.complete,
                            "size": b.size,
                        }
                        for b in sets
                    ],
                    indent=2,
                )
            )
            return EXIT_OK
        if not sets:
            self.print("no backups found")
        for b in sets:
            state = "" if b.complete else "  INCOMPLETE"
            self.print(f"{b.host:<16} {b.domain:<40} {b.timestamp}  {human_size(b.size):>10}{state}")
        return EXIT_OK

    def cmd_restore(self) -> int:
        a = self.args
        host = a.from_host or self.host
        storage = self.storage()
        backup = find_backup(storage, self.config.backup.prefix, host, a.site.lower(), a.timestamp)
        if backup is None:
            self.print(f"no complete backup of {a.site} ({a.timestamp}) from host {host}")
            return EXIT_FAILED
        db_map = {}
        for item in a.db_map:
            old, sep, new = item.partition("=")
            if not sep or not old or not new:
                raise ConfigError(f"--db-map expects OLD=NEW, got {item!r}")
            db_map[old] = new
        options = RestoreOptions(
            files=not a.db_only,
            databases=not a.files_only,
            target_dir=a.target,
            db_map=db_map,
            only_databases=a.database,
            dry_run=a.dry_run,
            force=a.force,
            chown=not a.no_chown,
            identity_file=a.identity,
            keep_previous=not a.no_keep_previous,
        )
        steps = Restorer(self.config, self.runner, storage).restore(backup, options)
        self.print(f"{'restore plan' if a.dry_run else 'restored'}: {backup.domain} {backup.timestamp} from {host}")
        for step in steps:
            self.print(f"  - {step}")
        return EXIT_OK

    def cmd_verify(self) -> int:
        a = self.args
        storage = self.storage()
        sets = [
            b
            for b in list_backups(storage, self.config.backup.prefix, self.host)
            if not a.site or b.domain in {s.lower() for s in a.site}
        ]
        if not a.all:
            latest: dict[str, object] = {}
            for b in sets:  # sorted oldest first; keep the newest complete one, or the newest at all
                if b.complete or b.domain not in latest:
                    latest[b.domain] = b
            sets = list(latest.values())  # type: ignore[arg-type]
        encryptor = Encryptor(self.config.encryption, self.runner)
        results = [
            verify_backup(
                storage,
                b,
                a.deep,
                encryptor,
                a.identity,
                self.config.backup.staging_dir if self.config.backup.staging_dir.is_dir() else None,
            )
            for b in sets
        ]
        ok = all(r.ok for r in results)
        if a.json:
            self.print(json.dumps({"ok": ok, "results": [r.to_dict() for r in results]}, indent=2))
        else:
            if not results:
                self.print("no backups found")
            for r in results:
                self.print(f"{r.domain:<40} {r.timestamp}  {'ok' if r.ok else 'FAILED'} ({r.checked} file(s))")
                for problem in r.problems:
                    self.print(f"    {problem}")
        if not a.no_notify:
            summary = f"{sum(r.ok for r in results)}/{len(results)} backup(s) verified" + (" (deep)" if a.deep else "")
            self.notifier().send(Event("verify", ok, self.host, summary, [r.to_dict() for r in results]))
        return EXIT_OK if ok and results else EXIT_FAILED

    def cmd_prune(self) -> int:
        storage = self.storage()
        runner = BackupRunner(self.config, self.runner, storage, self.host)
        domains = sorted({b.domain for b in list_backups(storage, self.config.backup.prefix, self.host)})
        if self.args.site:
            domains = [d for d in domains if d in {s.lower() for s in self.args.site}]
        for domain in domains:
            deleted = runner.prune_site(domain, dry_run=self.args.dry_run)
            verb = "would delete" if self.args.dry_run else "deleted"
            self.print(f"{domain}: {verb} {len(deleted)} backup(s)" + (f": {', '.join(deleted)}" if deleted else ""))
        return EXIT_OK

    def cmd_schedule(self) -> int:
        a = self.args
        opts = ScheduleOptions(
            executable=a.executable,
            config=a.config or Path("/etc/cloudpanel-backup/config.toml"),
            env_file=a.env_file or Path("/etc/cloudpanel-backup/env"),
            on_calendar=a.on_calendar,
            cron=a.cron_schedule,
            verify_after=a.verify_after,
        )
        if a.cron:
            self.out.write(cron_entry(opts))
            return EXIT_OK
        if a.install:
            for path in install_systemd(opts, a.unit_dir):
                self.print(f"wrote {path}")
            self.print("now run: systemctl daemon-reload && systemctl enable --now cloudpanel-backup.timer")
            return EXIT_OK
        for name, content in systemd_units(opts).items():
            self.print(f"# --- {name}")
            self.out.write(content)
        return EXIT_OK

    def cmd_test_notify(self) -> int:
        notifier = self.notifier()
        channels = notifier.channels()
        if not channels:
            self.print("no notification channels configured")
            return EXIT_FAILED
        failed = notifier.send(Event("test", True, self.host, "This is a test notification."))
        for channel in channels:
            self.print(f"{channel}: {'FAILED' if channel in failed else 'sent'}")
        return EXIT_FAILED if failed else EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO | None = None,
    runner: CommandRunner | None = None,
    transport: Transport | None = None,
    smtp_factory: SmtpFactory | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.ERROR if args.quiet else (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    if args.env_file:
        load_env_file(args.env_file)
    app = App(args, out or sys.stdout, runner or SubprocessRunner(), transport, smtp_factory)
    handler = getattr(app, "cmd_" + args.command.replace("-", "_"))
    try:
        return handler()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (StorageError, RestoreError, CommandError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
