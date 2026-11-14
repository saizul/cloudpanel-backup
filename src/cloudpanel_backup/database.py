"""Dump and import MariaDB/MySQL databases with ``mariadb-dump`` or ``clpctl db:export``."""

from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path

from cloudpanel_backup.appconfig import DbCredentials
from cloudpanel_backup.config import DatabaseSettings
from cloudpanel_backup.runner import CommandRunner, RunOptions
from cloudpanel_backup.util import write_private

log = logging.getLogger(__name__)

DEFAULT_DUMP_OPTIONS = (
    "--single-transaction",
    "--quick",
    "--routines",
    "--triggers",
    "--hex-blob",
    "--no-tablespaces",
    "--default-character-set=utf8mb4",
)


def _option_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def client_options(creds: DbCredentials) -> str:
    """The content of a ``--defaults-extra-file`` so the password never shows up in ``ps``."""
    lines = ["[client]"]
    if creds.user:
        lines.append(f"user={_option_value(creds.user)}")
    if creds.password is not None:
        lines.append(f"password={_option_value(creds.password)}")
    if creds.socket:
        lines.append(f"socket={_option_value(creds.socket)}")
    else:
        lines.append(f"host={_option_value(creds.host)}")
        lines.append(f"port={creds.port}")
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def defaults_file(creds: DbCredentials, directory: Path | None = None) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cpb-", dir=directory) as tmp:
        path = Path(tmp) / "client.cnf"
        write_private(path, client_options(creds))
        yield path


def resolve_credentials(db: DbCredentials, settings: DatabaseSettings, master: DbCredentials | None) -> DbCredentials:
    """Pick the credentials for one database.

    Order: an explicit ``[database] user`` from the config, then the site's own credentials (wp-config.php, .env),
    then the CloudPanel master credentials.
    """
    if settings.user:
        return DbCredentials(
            name=db.name,
            user=settings.user,
            password=settings.password(),
            host=settings.host,
            port=settings.port,
            source="config",
        )
    if db.user:
        return db
    if master is not None and settings.use_master_credentials:
        return DbCredentials(
            name=db.name,
            user=master.user,
            password=master.password,
            host=master.host,
            port=master.port,
            source="clpctl",
        )
    return DbCredentials(name=db.name, host=settings.host, port=settings.port, source="defaults")


def dump_program(runner: CommandRunner, settings: DatabaseSettings) -> str:
    if runner.which(settings.dump_program):
        return settings.dump_program
    if settings.dump_program == "mariadb-dump" and runner.which("mysqldump"):
        return "mysqldump"
    return settings.dump_program


def client_program(runner: CommandRunner, settings: DatabaseSettings) -> str:
    if runner.which(settings.client_program):
        return settings.client_program
    if settings.client_program == "mariadb" and runner.which("mysql"):
        return "mysql"
    return settings.client_program


def dump_database(
    runner: CommandRunner,
    creds: DbCredentials,
    output: Path,
    settings: DatabaseSettings,
    method: str = "mariadb-dump",
    compression_level: int = 6,
) -> None:
    """Write a gzipped SQL dump of ``creds.name`` to ``output`` (``.sql.gz``)."""
    if not str(output).endswith(".sql.gz"):
        raise ValueError("dump output must end with .sql.gz")
    if method == "clpctl":
        runner.run(["clpctl", "db:export", f"--databaseName={creds.name}", f"--file={output}"])
        return
    program = dump_program(runner, settings)
    with defaults_file(creds) as cnf:
        args = [program, f"--defaults-extra-file={cnf}", *DEFAULT_DUMP_OPTIONS, *settings.extra_dump_options]
        args.append(creds.name)
        runner.run(args, RunOptions(stdout_path=output, gzip_stdout=max(1, compression_level)))


def import_database(
    runner: CommandRunner,
    creds: DbCredentials,
    dump: Path,
    settings: DatabaseSettings,
    method: str = "mariadb-dump",
) -> None:
    """Load a gzipped SQL dump into an existing database."""
    if method == "clpctl":
        # clpctl db:import is documented to run from the directory that holds the dump.
        runner.run(
            ["clpctl", "db:import", f"--databaseName={creds.name}", f"--file={dump.name}"],
            RunOptions(cwd=dump.parent),
        )
        return
    program = client_program(runner, settings)
    with defaults_file(creds) as cnf:
        args = [program, f"--defaults-extra-file={cnf}", "--default-character-set=utf8mb4", creds.name]
        runner.run(args, RunOptions(stdin_path=dump, gunzip_stdin=True))
